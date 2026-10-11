#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
合规审查编排服务（本项目自有，P4 核心）

职责：把 P1–P3 验证过的机制固化成**服务端正式逻辑**，供网关、渠道与定时任务调用：
    1) 两段式执行：第一段能力服务出判定（带规则表指纹与命中规则），第二段工作流做编排
    2) 人工节点暂停 → 统一待办 → 按角色取件 → **原子认领** → 决策 → 恢复
    3) 并发安全：同一 run 的审批只有一个入口能生效（原子抢占，G6 已验证）
    4) 启动补偿：扫描暂停中的 run，为缺失的角色补建待办并重新通知
    5) 恢复一律走**同步路径**（框架线程池恢复路径在跨进程场景下记录不可靠，见 D0-G3 报告）

为什么这些必须由本项目做（D0 实读结论）：
    框架的人工节点只发一条文本通知，run_id 靠用户手动回填；没有待办表、没有自动恢复、
    没有投递确认，也没有并发互斥。合规审批必须有"谁该做什么、谁做过了、只做一次"。

本模块不做的事：
    - 不启动 HTTP 服务（那是 gateway 的职责）
    - 不直接改运行时框架源码（只调用其公开 API：WorkflowManager / WorkflowRunner）
"""

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from mcp_server import queue_store, runtime_store, tools_registry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_NAME = "compliance_review"

# 角色 -> 该角色在人工节点上可用的决策
#
# 【审查修正 P1】supervisor 的可用决策里补上 `reject_supervisor`：
# 主管"驳回"会被归一化为该决策名（与既有 DECISION_ROUTE 的键一致），
# 而原实现只在报错文案里把它列出来、校验却用的是不含它的元组 ——
# 结果主管点"驳回"100% 被拒（reject_supervisor 分支因此是死代码）。
ROLE_DECISIONS = {
    "specialist": ("confirm", "modify", "reject"),
    "supervisor": ("approve", "reject", "reject_supervisor"),
}

# 决策 -> (resume 值, 路由字段 detail_decision 的取值)
#
# 【P4/P7 实测，本项目最关键的一条框架纪律，务必按此实现】
#   框架会把 resume 的值写入**当前等待的那个 human 节点**的输出字段，且同一值可能被写进多个字段。
#   若让 human 节点的输出字段兼任条件边路由键，就会出现：
#     专员 confirm → 主管节点输出字段被写成 confirm → 主管 mapping 首键 approve 兜底
#     → 主管复核被静默跳过、流程直接办结。
#   因此本服务层规定：
#     - **路由只依赖 detail_decision**（非人工输出字段），人工输出字段只作记录；
#     - detail_decision 在 resume 之前写入（若在 resume 之后写，图已按旧值走完）。
#   resume 值本身只用于让被等待的节点继续执行，不承担路由职责。
#
# 【审查修正 P2】元组里原来的第一个元素（"写哪个记录字段"）从未被使用（死代码 + 误导），已删除；
# 真正写人工输出字段的是框架自己（把 resume 值写进当前等待节点的 output_field）。
DECISION_ROUTE = {
    "confirm": ("confirm", "pending"),         # 专员确认 → 待主管复核
    "modify": ("modify", "modify"),            # 专员要求重标注 → 回 entry_probe
    "reject": ("reject", "reject"),            # 专员驳回 → 终态
    "approve": ("approve", "confirm"),         # 主管通过 → 归档
    "reject_supervisor": ("reject", "reject"), # 主管驳回（审计口径区分）
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def capability_url() -> str:
    """岗位能力服务地址（唯一出处）。

    【第二次审查修正·P2】原来这个默认值**只写在 `capability_health` 里**，
    网关若想自己探活就必然复制一份 —— 两处地址一旦漂移就会重现
    "容器内永远 unreachable" 那类问题。现在收敛成本函数。
    """
    return (os.environ.get("CAPABILITY_URL") or "http://127.0.0.1:8770").rstrip("/")


def probe_capability(timeout_s: float = 3.0) -> dict:
    """直接探测能力服务（**不构造编排器、不编译工作流**）。

    【第二次审查修正·P2】网关的 `/health` 既要"无副作用、不阻塞"（不能被未授权调用
    拖成 DoS 面），又要有意义地报出能力服务是否在线。`ReviewOrchestrator.ensure_compiled`
    是重活，所以探活必须能脱离编排器单独跑 —— 本函数就是那个最小入口，
    由后台看门狗线程按低频调用，`/health` 只读快照。
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"{capability_url()}/health", timeout=timeout_s) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict):
            return {"status": "unreachable", "error": "响应结构异常"}
        return data
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)[:120]}


class ReviewOrchestrator:
    """合规审查编排器（两段式 + 待办 + 原子认领 + 启动补偿）。"""

    def __init__(self, workflow_name: str = WORKFLOW_NAME):
        self.workflow_name = workflow_name
        self._manager = None
        self._graph = None
        self._spec = None
        self._spec_digest = ""          # 上次定义时用的 spec 摘要（避免无谓重定义）
        self._definition = None         # 已定义的 workflow 对象
        self._cap_health: tuple | None = None   # 能力服务健康探测缓存 (ts, data)

    # ---------------------------------------------------------------- 内部

    @property
    def manager(self):
        if self._manager is None:
            from agent.workflow import get_workflow_manager
            self._manager = get_workflow_manager()
            if self._manager is None:
                raise RuntimeError("工作流管理器未初始化（ENABLE_WORKFLOW 是否为 true？）")
        return self._manager

    def spec(self) -> dict:
        if self._spec is None:
            path = PROJECT_ROOT / "workflows" / f"{self.workflow_name}.json"
            spec = json.loads(path.read_text(encoding="utf-8"))
            spec.pop("_comment", None)     # 注释字段参与编译会污染 spec
            # 预计算：每个路由字段可取的映射值（键为字段名，值为映射表）
            route_values: dict[str, dict] = {}
            for cond in spec.get("conditionals", []) or []:
                router = cond.get("router") or {}
                if router.get("kind") == "state_field" and router.get("field"):
                    route_values[router["field"]] = cond.get("mapping") or {}
            spec["_route_values"] = route_values
            self._spec = spec
        return self._spec

    def ensure_compiled(self, force: bool = False):
        """确保工作流已定义且编译。

        顺序纪律（P3 实测）：图上工具表的来源是主 Agent 的工具注册表，
        而 MCP 工具只有在能力服务在线时才会注册。因此这里先做健康检查（带缓存），
        再编译，避免得到一张"没有岗位工具"的图。

        【第二次审查修正·P2】
          ① 原先每次调用都 `manager.define_workflow(...)`：框架对已存在的工作流会
             **无条件 version+1 并清空图缓存**，于是每次 submit/decide/诊断都会全量重编译，
             且 run 记录里的 workflow_version 与续跑时实际用的图版本持续漂移。
             现在先比对 spec 摘要，未变则跳过重定义（可用 force=True 强制）。
          ② 健康检查带 TTL 缓存：原来 diagnostics 一次请求会触发 2~3 次 3 秒阻塞探测，
             整个端点要 10 秒以上。
        """
        health = self.capability_health()
        if force or self._spec_digest != self._digest_of_spec():
            wf = self.manager.define_workflow(self.workflow_name, "合规审查主流程", self.spec())
            if wf.last_error:
                raise RuntimeError(f"工作流编译失败：{wf.last_error}")
            self._spec_digest = self._digest_of_spec()
            self._definition = wf
        wf = self._definition
        if wf is None:
            wf = self.manager.get_workflow(self.workflow_name)
        if wf is None:
            raise RuntimeError("工作流未定义（ensure_compiled 未能完成定义）")
        if self._graph is None or self._graph[0] != wf.version:
            self._graph = (wf.version, self.manager._get_graph(wf))
        return {"workflow_id": wf.workflow_id, "version": wf.version,
                "capability_service": health}

    def _digest_of_spec(self) -> str:
        """spec 摘要（用于判断是否需要重新定义工作流）。"""
        import hashlib
        payload = json.dumps(self.spec(), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def graph(self):
        if self._graph is None:
            # 未编译时不要抛异常：网关在启动阶段会先查状态（例如健康检查、列表接口），
            # 此时还没有工作流图。调用方应自行判断是否需要先 ensure_compiled()。
            return None
        ver, g = self._graph
        return g

    @property
    def graph_ready(self) -> bool:
        return self._graph is not None

    def capability_health(self, ttl_s: float = 15.0) -> dict:
        """探测岗位能力服务（带 TTL 缓存，避免诊断/编译路径反复阻塞 3 秒）。

        【第二次审查修正·P2】地址改为读 `CAPABILITY_URL`（默认回环 8770）：
        原来硬编码 `127.0.0.1:8770`，而 docker-compose 以服务名 `capability` 组网，
        容器内该地址必然不可达（`/health` 的能力服务状态因此永远是 unreachable）。

        【第二次审查修正·P2b】探活实现下沉到模块级 `probe_capability()`：
        网关的后台看门狗需要"不构造编排器也能探活"，否则只能绕过本类复制地址。
        """
        base = capability_url()
        now = time.time()
        cached = getattr(self, "_cap_health", None)
        if cached and (now - cached[0]) < ttl_s:
            return cached[1]
        data = probe_capability()
        if data.get("status") == "unreachable":
            data = {**data, "url": base}
        self._cap_health = (now, data)
        return data

    def _run_row(self, run_id: str):
        return self.manager.dao.get_run(run_id)

    def _state(self, run_id: str) -> dict:
        if self.graph is None:
            return {}
        st = self.graph.get_state({"configurable": {"thread_id": run_id}})
        return (st.values or {}) if st else {}

    @staticmethod
    def _as_obj(value):
        """框架 tool 节点会把结果转字符串；这里统一反序列化。"""
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                obj = json.loads(value)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}

    def _sync_run_row(self, run_id: str, result: dict, workflow_id: str) -> None:
        """把 runner 的结果写回 run 记录（编排器自己维护状态，不依赖框架线程池路径）。"""
        self.manager._update_run_from_result(run_id, result, workflow_id)

    def _pause_todo(self, run_id: str, result: dict, role: str) -> dict | None:
        """人工节点暂停时登记待办（幂等：同一 run+节点+角色只建一条）。"""
        payload = result.get("payload") or {}
        question = payload.get("question") or "工作流已暂停，等待人工处理"
        node = payload.get("kind") or "human"
        return queue_store.create_todo(
            run_id=run_id, node_name=str(node), role_required=role,
            title=f"待审：{str(question)[:48]}", summary=str(question),
            payload=payload, workflow_id=self.workflow_name,
            trace_id=f"trace-{run_id}",
            idempotency_key=f"{run_id}:{node}:{role}",
        )

    def _role_for_question(self, question: str) -> str:
        """按人工节点提问判定受理角色（主管节点问题里含"主管"）。"""
        return "supervisor" if "主管" in (question or "") else "specialist"

    # ---------------------------------------------------------------- 第一段：受理与分析

    def intake(self, doc_text: str = "", path: str = "", doc_id: str = "",
               submitted_by: str = "") -> dict:
        """受理并完成第一段判定（能力服务）。返回判定结果 + 受理记录。"""
        ann = tools_registry._annotate_document(path=path, text=doc_text, doc_id=doc_id)
        if not ann.get("ok"):
            return {"ok": False, "errors": ann.get("errors") or ["解析或标注失败"]}
        ann["submitted_by"] = submitted_by
        ann["intake_at"] = _now()
        return {"ok": True, "analysis": ann,
                "summary": ann.get("decision_summary"),
                "risk_level": ann.get("overall_risk_level"),
                "rules": ann.get("hit_rule_ids", []),
                "rule_table_digest": ann.get("rule_table_digest")}

    # ---------------------------------------------------------------- 第二段：编排启动

    def submit(self, doc_text: str, run_id: str = "", submitted_by: str = "",
               analysis: dict | None = None) -> dict:
        """受理 + 启动编排。低风险会在本步直接办结；中/高风险会停在人工节点并登记待办。

        【审查修正 P2】原实现有四个问题，这里一并修正：
          1) `runner.invoke` 抛异常时直接冒到网关 500，run 行永久停在 running、无审计；
          2) `create_run` 返回 False（run_id 撞唯一键）被忽略，会静默复用既有 thread 的状态；
          3) `result["status"] == "error"` 时仍记 `review_archived`，审计口径与事实不符；
          4) `analysis` 传进来时未校验其 ok 字段（调用方传半成品会得到不可解释的失败）。
        """
        if analysis is not None and not analysis.get("ok"):
            return {"ok": False, "errors": ["判定失败，未启动编排"]}
        ann = analysis or (self.intake(doc_text=doc_text, submitted_by=submitted_by).get("analysis"))
        if not ann or not ann.get("ok"):
            return {"ok": False, "errors": ["判定失败，未启动编排"]}

        self.ensure_compiled()
        wf = self.manager.get_workflow(self.workflow_name)
        run_id = run_id or f"wf_run_{uuid.uuid4().hex[:12]}"
        risk = ann.get("overall_risk_level", "")

        # 先建 run 记录（框架的 DAO 要求先有行；状态由编排器维护）
        from dao.workflow import WorkflowRun
        created = self.manager.dao.create_run(WorkflowRun(
            run_id=run_id, workflow_id=wf.workflow_id, workflow_version=wf.version,
            status="running", input={"doc_id": ann.get("doc_id", "")},
            origin_context={"submitted_by": submitted_by or "unknown"}))
        if not created:
            return {"ok": False, "errors": [f"run_id 已存在，拒绝复用：{run_id}"]}
        queue_store.record_audit(run_id, "review_submitted", actor=submitted_by or "unknown",
                                 detail={"doc_id": ann.get("doc_id"),
                                         "risk_level": risk,
                                         "rules": ann.get("hit_rule_ids", []),
                                         "rule_table_digest": ann.get("rule_table_digest")},
                                 trace_id=f"trace-{run_id}")

        init = {
            "doc_text": doc_text,
            "doc_id": ann.get("doc_id", ""),
            "annotation": ann,
            "overall_risk_level": risk,
            "decision_summary": ann.get("decision_summary", ""),
        }
        try:
            result = self.manager.runner.invoke(self.graph, run_id, wf.workflow_id, init, {},
                                                self.manager.dao)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            queue_store.record_audit(run_id, "submit_failed", actor=submitted_by or "unknown",
                                     detail={"error": err}, trace_id=f"trace-{run_id}")
            self._sync_run_row(run_id, {"status": "error", "payload": {"error": err}},
                               wf.workflow_id)
            return {"ok": False, "run_id": run_id, "errors": [f"启动编排失败：{err}"]}

        self._sync_run_row(run_id, result, wf.workflow_id)

        paused_info = None
        if result["status"] == "paused":
            question = (result.get("payload") or {}).get("question", "")
            role = self._role_for_question(question)
            todo = self._pause_todo(run_id, result, role)
            paused_info = {"role": role, "question": question,
                           "todo_id": (todo or {}).get("todo", {}).get("todo_id"),
                           "todo_created": (todo or {}).get("created")}
        else:
            queue_store.record_audit(
                run_id,
                "review_archived" if result["status"] == "completed" else "review_failed",
                actor="system",
                detail={"status": result["status"], "risk_level": risk},
                trace_id=f"trace-{run_id}")

        return {"ok": True, "run_id": run_id, "status": result["status"],
                "risk_level": risk, "doc_id": ann.get("doc_id"),
                "paused": paused_info,
                "state": {k: self._state(run_id).get(k)
                          for k in ("overall_risk_level", "specialist_note", "supervisor_note")}}

    # ---------------------------------------------------------------- 待办与审批

    def pending(self, role: str = "", status: str = "open", limit: int = 50) -> dict:
        return tools_registry._list_pending(role=role, status=status, limit=limit)

    # 角色 -> 该角色可用的决策（类属性引用模块级常量，供外部 `ReviewOrchestrator.ROLE_DECISIONS` 读取）
    ROLE_DECISIONS = ROLE_DECISIONS

    @staticmethod
    def _norm_decision(decision: str, role: str = "", pending_role: str = "") -> str:
        """归一化决策名：主管用 approve 表示通过；专员用 confirm 表示确认。

        【审查修正 P1】原实现是 `"reject_supervisor" if role == "supervisor" else "reject"`，
        而 `ROLE_DECISIONS["supervisor"]` 里只有 `("approve", "reject")`，
        于是 decide() 的校验对 `reject_supervisor` 恒为真 → **主管"驳回"100% 被拒**，
        DECISION_ROUTE["reject_supervisor"] 与前端"驳回"按钮全是死代码。
        现在：`reject` 保留原样，主管驳回在 decide() 里按"实际行权角色"归一化为
        `reject_supervisor`（两者语义相同、都终止流程，区别只在审计口径）。
        """
        d = (decision or "").strip().lower()
        if d in ("approve", "通过", "同意"):
            return "approve"
        if d in ("confirm", "确认"):
            return "confirm"
        if d in ("modify", "修改", "重标注"):
            return "modify"
        if d in ("reject", "驳回"):
            return "reject"
        return d

    # 决策 -> 该决策【专属于】哪个角色（None 表示两个人工角色都能用，按等待节点归属）
    #
    # 【审查修正 P1】`reject` 原来被硬编码为 specialist 专属，导致主管在
    # supervisor_verify 节点上点"驳回"被节点绑定校验拦下（与旧 bug 是同一处使用者的痛点）。
    _DECISION_ROLE = {"confirm": "specialist", "modify": "specialist",
                      "reject": None, "reject_supervisor": "supervisor", "approve": "supervisor"}

    # 等待中的节点 -> 该节点的人工角色
    _NODE_ROLE = {"specialist_review": "specialist", "supervisor_verify": "supervisor"}

    def pending_node(self, run_id: str) -> str:
        """当前等待中的人工节点名（空串表示没有等待中的人工节点）。"""
        st = self.graph.get_state({"configurable": {"thread_id": run_id}})
        names = [getattr(t, "name", "") for t in (st.tasks or [])]
        for node in ("supervisor_verify", "specialist_review"):
            if node in names:
                return node
        return ""

    def pending_role(self, run_id: str) -> str:
        """当前等待中的人工节点对应哪个角色（网关与调用方据此选择决策入口）。"""
        return self._NODE_ROLE.get(self.pending_node(run_id), "")

    def decide(self, run_id: str, decision: str, actor: str, role: str, note: str = "",
               reannotate_text: str = "") -> dict:
        """服务端决策入口：节点-角色绑定校验 → 原子认领 → 记录审计 → 写回路由字段 → 同步恢复 → 完结待办。

        并发安全：认领使用 `runtime_store.claim_run`（单条 UPDATE 的原子抢占，含崩溃租约）。
        两个入口同时审批同一 run 时，只有一个能拿到执行权，另一个得到明确的"已被占用"。

        【审查修正 P1】决策必须与"当前等待的人工节点"绑定（原实现从不调用 pending_role）：
          原缺陷 1：run 停在 supervisor_verify 时，用 specialist 提交 confirm 会被放行，
                    框架把 "confirm" 写进**主管节点**的输出字段 supervisor_note —— 伪造主管签核痕迹。
          原缺陷 2：run 停在 specialist_review 时，supervisor/admin 提交 approve 会走到
                    detail_decision=confirm → 单人一步办结，**主管复核被整段跳过**。
        现在：角色与等待节点不一致 → 409；admin 只以"当前等待节点"的角色语义行权。
        """
        decision = self._norm_decision(decision, role)
        evidence = {"run_id": run_id, "decision": decision, "actor": actor, "role": role,
                    "note": note[:200]}
        known = sum(ROLE_DECISIONS.values(), ()) + ("reject_supervisor",)
        if decision not in known:
            return {"ok": False, "errors": [f"未知决策：{decision}"]}
        if role not in ROLE_DECISIONS and role != "admin":
            return {"ok": False, "errors": [f"未知角色：{role}"]}

        row = self._run_row(run_id)
        if row is None:
            return {"ok": False, "errors": [f"run 不存在：{run_id}"]}
        if row.status != "paused":
            return {"ok": False, "errors": [f"run 当前状态为 {row.status}，无需人工决策"]}

        # 节点-角色绑定（在认领之前判定，避免占坑后立刻拒绝）
        try:
            self.ensure_compiled()
            waiting_node = self.pending_node(run_id)
        except Exception as exc:
            return {"ok": False, "errors": [f"无法读取工作流状态：{type(exc).__name__}: {exc}"]}
        expected = self._NODE_ROLE.get(waiting_node, "")
        if not expected:
            return {"ok": False, "errors": [
                f"run {run_id} 当前没有等待中的人工节点（可能已被其他人处理或流程已推进）"]}
        # admin 代操作：以"当前等待节点"的角色语义执行，不能跨节点行权
        eff_role = expected if role == "admin" else role
        # 主管的"驳回"归一化为 reject_supervisor（同一语义、审计口径区分；路由取值相同）
        if decision == "reject" and eff_role == "supervisor":
            decision = "reject_supervisor"
        want_role = self._DECISION_ROLE.get(decision, eff_role) or eff_role
        if eff_role != expected or want_role != expected:
            return {"ok": False, "conflict": True, "errors": [
                f"决策与当前等待节点不匹配：run 正等待 {waiting_node}（角色 {expected}），"
                f"而 {decision} 属于角色 {want_role}。请联系对应的审批人处理。"],
                "pending_node": waiting_node, "pending_role": expected}
        if decision not in ROLE_DECISIONS[eff_role]:
            allowed = tuple(ROLE_DECISIONS[eff_role]) + \
                (("reject_supervisor",) if eff_role == "supervisor" else ())
            return {"ok": False,
                    "errors": [f"角色 {eff_role} 不允许执行 {decision}（可用：{allowed}）"]}
        role = eff_role                       # 审计口径统一为节点角色
        evidence["role"] = role

        owner = f"{actor or 'anonymous'}@{_now()}"
        if not runtime_store.claim_run(run_id, owner):
            holder = runtime_store.get_claim(run_id) or {}
            return {"ok": False, "claimed": False,
                    "errors": [f"该审批已被 {holder.get('claimed_by') or '他人'} 处理中，请勿重复提交"],
                    "holder": holder.get("claimed_by")}

        try:
            wf = self.manager.get_workflow(self.workflow_name)
            # 1) 审计 + 待办完结（待办按角色匹配）
            todos = queue_store.list_todos(role=role, status="open", run_id=run_id, limit=5)
            todo_id = todos[0]["todo_id"] if todos else ""
            queue_store.record_audit(run_id, "human_decision", actor=actor, role=role,
                                     detail=evidence, trace_id=f"trace-{run_id}")
            if todo_id:
                queue_store.complete_todo(todo_id, actor, decision, note)
            decision_seq = queue_store.count_audit(run_id, "human_decision")

            # 2) modify：服务层重新标注（可重复、可审计），再回到人工确认
            if decision == "modify":
                body = reannotate_text or self._state(run_id).get("doc_text", "")
                ann = self.intake(doc_text=body, submitted_by=actor)
                if not ann.get("ok"):
                    return {"ok": False, "errors": ["重新标注失败"]}
                new_ann = ann["analysis"]
                # 注意：这里**不**写 specialist_note，避免把图提前推进到下一个节点。
                # 只更新判定结果；路由由随后的 resume("modify") 生效。
                self.graph.update_state({"configurable": {"thread_id": run_id}}, {
                    "annotation": new_ann,
                    "overall_risk_level": new_ann.get("overall_risk_level", ""),
                    "decision_summary": new_ann.get("decision_summary", "")})
                queue_store.record_audit(run_id, "reannotated", actor=actor,
                                         detail={"risk_level": new_ann.get("overall_risk_level"),
                                                 "rules": new_ann.get("hit_rule_ids", [])},
                                         trace_id=f"trace-{run_id}")

            # 3) 写回路由字段（detail_decision 是唯一的路由依据）
            #
            #    【P4/P7 实测，关键】必须在 resume **之前**写入：若在 resume 之后写，
            #    图已经按旧值走完了整个 super-step，写入不会改变本次分流。
            #    同时只写 detail_decision，不碰人工输出字段（避免污染下游节点）。
            resume_value, detail_flag = DECISION_ROUTE[decision]
            self.graph.update_state({"configurable": {"thread_id": run_id}},
                                    {"detail_decision": detail_flag})

            # 4) 同步恢复（不用框架线程池路径）
            result = self.manager.runner.resume(self.graph, run_id, wf.workflow_id,
                                                resume_value, {})
            self._sync_run_row(run_id, result, wf.workflow_id)

            paused_info = None
            if result["status"] == "paused":
                question = (result.get("payload") or {}).get("question", "")
                next_role = self._role_for_question(question)
                # 【审查修正 P2】幂等键原来含 `_now()`（秒级时间戳）：
                # 客户端重试/同秒重复提交会生成新键 → 绕过幂等 → 重复待办 + 重复重标注。
                # 现在用"该 run 的第 N 次人工决策"作稳定序号。
                todo = queue_store.create_todo(
                    run_id=run_id, node_name="human", role_required=next_role,
                    title=f"待审：{question[:48]}", summary=question,
                    payload=result.get("payload") or {}, workflow_id=self.workflow_name,
                    trace_id=f"trace-{run_id}",
                    idempotency_key=f"{run_id}:human:{next_role}:{decision_seq}")
                paused_info = {"role": next_role, "question": question,
                               "todo_id": (todo or {}).get("todo", {}).get("todo_id"),
                               "todo_created": (todo or {}).get("created")}
            else:
                # 【审查修正 P2】原来无论 result["status"] 是什么都记 review_archived，
                # 连 error 也记成"已归档"，审计口径与事实不符。
                queue_store.record_audit(
                    run_id,
                    "review_archived" if result["status"] == "completed" else "review_failed",
                    actor=actor, detail={"status": result["status"]}, trace_id=f"trace-{run_id}")

            return {"ok": True, "claimed": True, "run_id": run_id, "decision": decision,
                    "status": result["status"], "paused": paused_info,
                    "final_state": {k: self._state(run_id).get(k)
                                    for k in ("overall_risk_level", "specialist_note",
                                              "supervisor_note")}}
        except Exception as exc:
            # 【审查修正 P2】原实现让异常直接冒到网关 500，且 run 行永久停在 running、
            # 不留审计。现在：落审计 + 标 error + 释放占用（占用已在 finally 释放）。
            obs_err = f"{type(exc).__name__}: {exc}"
            queue_store.record_audit(run_id, "decide_failed", actor=actor, role=role,
                                     detail={"error": obs_err, "decision": decision},
                                     trace_id=f"trace-{run_id}")
            self._sync_run_row(run_id,
                               {"status": "error", "payload": {"error": obs_err}},
                               self.manager.get_workflow(self.workflow_name).workflow_id
                               if self._graph else "")
            return {"ok": False, "errors": [f"恢复工作流失败：{obs_err}"]}
        finally:
            runtime_store.release_run(run_id, owner)

    # ---------------------------------------------------------------- 启动补偿与可观测

    def recover_on_startup(self) -> dict:
        """启动时扫描暂停中的 run，为缺失待办补建并记录通知事件。

        框架本身没有这个能力（D0 实读：`list_pending_runs` 无生产调用点），
        因此"重启后审批人还能收到提醒"必须由本项目保证。

        【审查修正 P1】先释放**过期占用**：认领本身有租约，但进程被 kill 后
        占用行会残留到下一次启动；不清理就会出现"该审批永远提示已被他人处理中"。
        """
        released = runtime_store.release_stale()
        pending = runtime_store.list_pending()
        created, skipped = [], []
        for row in pending:
            run_id = row["run_id"]
            info = self._as_obj(row.get("interrupt_info"))
            question = info.get("question") or "工作流已暂停，等待人工处理"
            role = self._role_for_question(question)
            existing = queue_store.list_todos(role=role, status="open", run_id=run_id, limit=1)
            if existing:
                skipped.append({"run_id": run_id, "reason": "已有待办"})
                continue
            todo = queue_store.create_todo(
                run_id=run_id, node_name=str(info.get("kind") or "human"), role_required=role,
                title=f"待审（重启补发）：{question[:40]}", summary=question,
                payload=info, workflow_id=row.get("workflow_id", ""),
                trace_id=f"trace-{run_id}",
                idempotency_key=f"{run_id}:recover:{role}:{info.get('kind')}")
            queue_store.record_audit(run_id, "notified_on_startup", actor="system",
                                     detail={"role": role, "question": question[:120]},
                                     trace_id=f"trace-{run_id}")
            created.append({"run_id": run_id, "role": role,
                            "todo_id": (todo or {}).get("todo", {}).get("todo_id")})
        return {"pending_runs": len(pending), "notified": created, "skipped": skipped,
                "released_stale_claims": released, "at": _now()}

    def status(self, run_id: str) -> dict:
        row = self._run_row(run_id)
        if row is None:
            return {"ok": False, "errors": [f"run 不存在：{run_id}"]}
        st = self._state(run_id)
        ann = self._as_obj(st.get("annotation"))
        return {
            "ok": True, "run_id": run_id, "status": row.status, "error": row.error,
            "risk_level": st.get("overall_risk_level"),
            "doc_id": st.get("doc_id"),
            "specialist_note": st.get("specialist_note"),
            "supervisor_note": st.get("supervisor_note"),
            "decision_summary": st.get("decision_summary"),
            "hit_rule_ids": sorted({h.get("rule_id") for a in (ann.get("annotations") or [])
                                    for h in (a.get("risk_hits") or []) if h.get("rule_id")}),
            "claim": runtime_store.get_claim(run_id),
            "todos": queue_store.list_todos(run_id=run_id, status="all", limit=20),
            "audit": queue_store.list_audit(run_id=run_id, limit=50),
        }

    def metrics(self) -> dict:
        m = tools_registry._metrics()
        m["pending_runs"] = len(runtime_store.list_pending())
        return m

    # ================================================================ P5：规则提案闭环

    def proposals(self, status: str = "") -> dict:
        """列出规则提案（可按状态过滤）。"""
        return tools_registry._list_rule_proposals(status=status)

    def proposal_status(self, proposal_id: str) -> dict:
        """单条提案详情 + 是否与当前规则表冲突（供主管审批前看清影响面）。"""
        for p in self.proposals()["proposals"]:
            if p.get("proposal_id") == proposal_id:
                from mcp_server import rules_store
                target = rules_store.get(p.get("target_rule_id") or "")
                return {"ok": True, "proposal": p,
                        "current_rule": target,
                        "current_version": (target or {}).get("version"),
                        "proposed_version": p.get("version"),
                        "would_change_severity": bool(target) and
                        target.get("severity") != p.get("severity"),
                        "rules_digest": rules_store.digest()}
        return {"ok": False, "errors": [f"提案不存在：{proposal_id}"]}

    def review_proposal(self, proposal_id: str, decision: str, actor: str,
                        note: str = "") -> dict:
        """主管审批规则提案（P5 核心）。

        与人工节点审批共享同一套互斥原语：提案文件里的 `review_claim` 是认领标记，
        认领采用"写临时文件 + 原子替换"，因此并发/跨进程只有一个请求能生效。

        业务纪律（不可绕过）：
            - 驳回原因**绝不直接改规则库**：只有主管批准才写规则表
            - 同模式异等级冲突必须先人工裁决，禁止直接写入
            - active 规则必须带法条引用（写入关口校验）
        """
        from mcp_server import rules_store, queue_store
        import json as _json
        import os as _os
        import tempfile as _tempfile
        import shutil as _shutil

        if decision not in ("approve", "reject"):
            return {"ok": False, "errors": [f"未知审批决策：{decision}（可用 approve/reject）"]}
        if not (actor or "").strip():
            return {"ok": False, "errors": ["审批人不能为空（审计要求）"]}

        # 【安全修正】提案编号必须做格式白名单 + 路径围栏校验，
        # 否则 `../../docs/evidence/xxx` 这类编号可读写任意 JSON 文件
        # （HTTP 与 MCP 入口都只做透传，不校验 schema）。
        try:
            path = rules_store.proposal_path(proposal_id)
        except ValueError as exc:
            return {"ok": False, "errors": [str(exc)]}
        if not path.exists():
            return {"ok": False, "errors": [f"提案不存在：{proposal_id}"]}

        def _read_proposal(retries: int = 12) -> dict:
            """带重试的读取。

            【P5 实测】并发审批时，一个线程正在 os.replace 替换提案文件，
            另一个线程可能刚好撞上替换瞬间而拿到 PermissionError（Windows 上
            文件被占用）。这不是业务错误，重试即可稳定。
            """
            last = None
            for _ in range(retries):
                try:
                    return _json.loads(path.read_text(encoding="utf-8"))
                except (PermissionError, FileNotFoundError) as exc:
                    last = exc
                    time.sleep(0.05)
            raise RuntimeError(f"读取提案失败（并发占用）：{last}")

        proposal = _read_proposal()
        if proposal.get("status") in ("approved", "rejected"):
            return {"ok": False, "errors": [f"提案已处理过（{proposal['status']}），拒绝重复审批"]}

        # 原子认领：写临时文件 + os.replace（同一文件系统上的原子操作）
        owner = f"{actor}@{_now()}"
        proposal["review_claim"] = owner
        fd, tmp = _tempfile.mkstemp(dir=str(rules_store.PROPOSALS_DIR), suffix=".tmp")
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as fh:
                _json.dump(proposal, fh, ensure_ascii=False, indent=2, default=str)
            _shutil.move(tmp, path)
        finally:
            if _os.path.exists(tmp):
                _os.unlink(tmp)

        claimed = _read_proposal().get("review_claim")
        if claimed != owner:
            return {"ok": False, "claimed": False,
                    "errors": [f"提案已被 {claimed} 认领处理中，请勿重复提交"]}

        try:
            if decision == "reject":
                res = rules_store.reject(proposal_id, actor, note)
                if res.get("ok"):
                    queue_store.record_audit("", "rule_proposal_rejected", actor=actor,
                                             role="supervisor",
                                             detail={"proposal_id": proposal_id, "reason": note[:200]})
                    self._close_proposal_todos(proposal_id, actor, "reject", note)
                return {**res, "claimed": True, "decision": decision}

            res = rules_store.approve(proposal_id, actor)
            if not res.get("ok"):
                # 批准失败要把认领标记释放，允许修正后重试
                proposal["review_claim"] = None
                path.write_text(_json.dumps(proposal, ensure_ascii=False, indent=2, default=str),
                                encoding="utf-8")
                return {**res, "claimed": True, "decision": decision}
            queue_store.record_audit("", "rule_proposal_approved", actor=actor, role="supervisor",
                                     detail={"proposal_id": proposal_id,
                                             "rule_id": res.get("rule_id"),
                                             "version": res.get("version"),
                                             "new_digest": res.get("new_digest")})
            self._close_proposal_todos(proposal_id, actor, "approve", note)
            return {**res, "claimed": True, "decision": decision}
        finally:
            # 认领成功即视为该提案已处理：清掉认领标记，避免残留
            try:
                cur = _read_proposal()
                cur["review_claim"] = None
                path.write_text(_json.dumps(cur, ensure_ascii=False, indent=2, default=str),
                                encoding="utf-8")
            except Exception:
                pass

    def _close_proposal_todos(self, proposal_id: str, actor: str, decision: str,
                              note: str) -> None:
        """完结该提案对应的主管待办（便于待办视图与指标口径一致）。"""
        from mcp_server import queue_store
        for t in queue_store.list_todos(role="supervisor", status="open", limit=200):
            if proposal_id in (t.get("payload_json") or "") or proposal_id in (t.get("title") or ""):
                queue_store.complete_todo(t["todo_id"], actor, decision, note)

    def propose_rule(self, reject_reason: str, pattern: str, severity: str,
                     regulation_ref: str = "", suggestion: str = "",
                     contract_id: str = "", proposed_by: str = "", run_id: str = "") -> dict:
        """服务层提案入口（把待办与审计一并登记，便于主管在其队列里看到）。"""
        return tools_registry._propose_rule(
            reject_reason=reject_reason, pattern=pattern, severity=severity,
            regulation_ref=regulation_ref, suggestion=suggestion, contract_id=contract_id,
            proposed_by=proposed_by, run_id=run_id, trace_id=f"trace-{run_id or 'proposal'}")

