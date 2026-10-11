#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
主动工作（P6）：例行巡检 + 自排跟进任务

这是"数字员工"与"工作流代理"的分水岭：员工不等指令，自己发现待办、自己安排跟进。

两条主动能力：
    1) **例行巡检**：扫描共享盘收件目录（Mock OA），发现新合同就主动发起合规审查，
       结果经渠道通知。幂等键 = 合同内容哈希，避免重复审查（Mock 的 duplicate 故障专门验证这点）。
    2) **自排跟进**：审查中发现"需补充材料"，员工自己创建 Cron 跟进任务，到期提醒。

与框架的接入方式（实读结论，零改动）：
    框架的 Cron 是"到点把提示词发给主 Agent 执行"（agent/tools/cron_tool.py:125），
    因此定时任务只需要一段提示词；真正的扫描逻辑由本项目提供为 MCP 工具/服务方法，
    两者通过 `.lemonclaw/.env` 与 mcp.json 连接，框架源码不动。

容错（对应容错矩阵 mock OA 超时/报错）：
    外部系统故障时**不阻塞批次**：单条失败记录告警继续处理其余；整批不可达时返回明确降级结果。
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from service.connectors import (build_contract_source, build_mail_gateway,
                                content_hash, describe as describe_connectors)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 【审查修正 P1】Mock / 能力服务地址改为可配置：
#   原来硬编码 127.0.0.1:8780，而 docker-compose 以服务名（mock / capability）组网，
#   容器内该地址必然不可达（配置里的 MOCK_URL/CAPABILITY_URL 又没人读）。
MOCK_OA = os.environ.get("MOCK_URL", "http://127.0.0.1:8780").rstrip("/")
# 扫描状态文件同样可覆盖（自检脚本可指向隔离路径，避免污染真实"只审一次"依据）
SCAN_STATE = Path(os.environ.get("SCAN_STATE_PATH",
                                 str(PROJECT_ROOT / ".runtime" / "scan-state.json")))
DEFAULT_CRON = "0 9 * * *"          # 每天 9:00
FOLLOWUP_CRON = "0 9 * * *"

# 【审查修正 P2】巡检状态是"同一合同只主动审一次"的唯一依据，
# 读改写必须有互斥（且必须是 RLock：scan_once 持锁期间 _save_state 还要再取一次）。
_STATE_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ProactiveWorker:
    """例行巡检与自排跟进的执行体（服务层，可被定时任务/网关/手动调用）。

    外部系统通过**连接器**接入（`service/connectors.py`）：
    默认仍是本项目的 Mock（开箱即用），企业环境只需改环境变量即可切到
    共享盘目录 / OA HTTP 接口 / SMTP 邮件网关，本类代码不变。
    """

    def __init__(self, orchestrator=None, mock_base: str = MOCK_OA,
                 contract_source=None, mail_gateway=None, fault: str = ""):
        self.fault = fault
        self._source = contract_source or build_contract_source(mock_base=mock_base)
        self._mail = mail_gateway or build_mail_gateway(mock_base=mock_base)
        self._orch = orchestrator

    @property
    def connectors(self) -> dict:
        return describe_connectors()

    @property
    def orch(self):
        if self._orch is None:
            from service.review_orchestrator import ReviewOrchestrator
            self._orch = ReviewOrchestrator()
        return self._orch

    # ---------------------------------------------------------------- 状态

    def _load_state(self) -> dict:
        if SCAN_STATE.exists():
            try:
                return json.loads(SCAN_STATE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_state(self, state: dict) -> None:
        """原子写巡检状态（临时文件 + os.replace），并加进程内互斥。

        【审查修正 P2】原来直接 `write_text`：并发（cron + 手动扫描）下两个
        读改写序列会互相覆盖 processed_hashes，导致同一合同被重复发起审查与重复发信。
        """
        SCAN_STATE.parent.mkdir(parents=True, exist_ok=True)
        with _STATE_LOCK:
            fd, tmp = tempfile.mkstemp(dir=str(SCAN_STATE.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, SCAN_STATE)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # ---------------------------------------------------------------- 巡检

    def list_inbox(self, fault: str = "") -> dict:
        """读取收件目录（经连接器）。

        故障注入：仅在 Mock 连接器上生效（`X-Mock-Mode`），
        用于验证"外部系统会怎么坏"的降级路径；真实连接器不注入故障。
        """
        src = self._source
        f = fault or self.fault
        if hasattr(src, "fault"):
            # 【务必每次显式赋值】不能只在非空时设置：前一轮用过 fault="empty" 后，
            # 故障会**粘在连接器上**，导致后续正常调用一直拿到空清单（实测踩过）。
            try:
                src.fault = f
            except Exception:
                pass
        res = src.list_contracts()
        out = res.as_dict()
        if not res.ok:
            out["error"] = res.error or "收件目录不可用"
        return out

    def download(self, contract_id: str) -> bytes:
        res = self._source.fetch_contract(contract_id)
        if not res.ok:
            raise IOError(res.error or f"下载失败：{contract_id}")
        return res.content

    def scan_once(self, fault: str = "", notify_to: str = "compliance-lead@example.com",
                  max_items: int = 20) -> dict:
        """扫描一次收件目录：新合同 → 主动发起审查 → 通知（幂等）。

        【第二次审查修正·P1】"读状态 → 处理 → 写状态"整段放进同一把锁：
        原实现只给 `_save_state` 加了锁，而 `scan_once` 的 load→…→save 不是原子的，
        定时巡检与手动扫描并发时会出现"后写覆盖先写" → `processed_hashes` 丢失
        → 同一份合同被重复发起审查、重复发信（幂等承诺失效）。
        （`_STATE_LOCK` 是 RLock，`_save_state` 内部再取一次不会死锁。）
        """
        with _STATE_LOCK:
            return self._scan_once_locked(fault=fault, notify_to=notify_to, max_items=max_items)

    def _scan_once_locked(self, fault: str, notify_to: str, max_items: int) -> dict:
        started = _now()
        inbox = self.list_inbox(fault=fault)
        state = self._load_state()
        processed = set(state.get("processed_hashes", []))
        result = {"started_at": started, "inbox_ok": inbox["ok"], "degraded": inbox["degraded"],
                  "scanned": 0, "new": 0, "skipped_duplicate": 0, "failed": [], "submitted": [],
                  "skipped": [], "notifications": []}
        if not inbox["ok"]:
            result["error"] = inbox["error"]
            result["finished_at"] = _now()
            return result

        items = inbox.get("items", [])[:max_items]
        result["scanned"] = len(items)
        try:
            for item in items:
                self._scan_item(item, processed, result, notify_to)
        finally:
            # 【审查修正 P2】原实现把状态落盘放在循环之后：中途抛异常则**本轮已成功的
            # 合同哈希全部丢失** → 下一轮重复发起审查、重复发信。现在用 finally 保证落盘。
            state["processed_hashes"] = sorted(processed)
            state["last_scan_at"] = _now()
            state["last_scan_summary"] = {k: result[k] for k in
                                          ("scanned", "new", "skipped_duplicate", "degraded")}
            self._save_state(state)
        result["finished_at"] = _now()
        return result

    def _scan_item(self, item: dict, processed: set, result: dict, notify_to: str) -> None:
        """处理收件目录中的单条合同（单条失败不阻塞批次）。"""
        cid = item.get("contract_id", "")
        # 【连接器落地】不是所有外部系统都会给内容哈希（共享盘目录、部分 OA 接口都不给），
        # 因此"幂等键"必须能**由原文算出来**，否则同一份合同每轮都会被重复审查。
        upstream_hash = str(item.get("content_hash") or "")
        if not cid:
            result["failed"].append({"contract_id": cid, "reason": "缺少 contract_id"})
            return
        try:
            body = self.download(cid)
            # 【第二次审查修正·P2】不再用"上游给的哈希"直接跳过：上游哈希字段配错/恒定值时，
            # 除第一份以外全部被静默判重 → **漏审**（本项目最不能接受的失败模式）。
            # 现在一律以**下载到的原文内容哈希**为准（代价是一次下载，换来不漏审），
            # 并把被跳过的合同编号记进 `skipped`，让"为什么没审"可追溯。
            chash = content_hash(body) if body else upstream_hash
            if chash and chash in processed:
                result["skipped_duplicate"] += 1
                result["skipped"].append({"contract_id": cid, "content_hash": chash,
                                          "by": "content_hash"})
                return
            text = body.decode("utf-8", errors="replace")
            submitted = self.orch.submit(text, submitted_by="proactive-scan")
            # 【审查修正 P2】原实现不判断 submitted["ok"]：判定/编排失败（例如解析失败）
            # 也被计入 new、写入 submitted，并把内容哈希记入 processed_hashes 且不写 failed
            # —— 合同**静默丢失且永不重试**。现在只有成功才记账+记哈希。
            if not submitted.get("ok"):
                result["failed"].append({
                    "contract_id": cid,
                    "reason": "发起审查失败：" + "；".join(submitted.get("errors") or ["未知原因"])})
                return
            result["new"] += 1
            result["submitted"].append({"contract_id": cid, "run_id": submitted.get("run_id"),
                                        "status": submitted.get("status"),
                                        "risk_level": submitted.get("risk_level"),
                                        "content_hash": chash,
                                        "paused_role": (submitted.get("paused") or {}).get("role")})
            if chash:
                processed.add(chash)
            # 主动推送结果（Mock 邮箱）
            if notify_to:
                try:
                    self.send_mail(
                        to=notify_to,
                        subject=f"[主动审查] 合同 {cid} 已完成自动审查（{submitted.get('risk_level')}）",
                        body=(f"run_id={submitted.get('run_id')}\n"
                              f"风险等级={submitted.get('risk_level')}\n"
                              f"状态={submitted.get('status')}\n"
                              f"待人工环节={(submitted.get('paused') or {}).get('role') or '无'}\n"
                              f"摘要={submitted.get('state', {}).get('overall_risk_level')}"),
                        idempotency_key=f"review-done:{cid}:{chash}")
                    result["notifications"].append(cid)
                except Exception as exc:
                    result["failed"].append({"contract_id": cid,
                                             "reason": f"通知失败：{exc}"})
        except Exception as exc:
            # 单条失败不阻塞批次（容错矩阵要求）
            result["failed"].append({"contract_id": cid,
                                     "reason": f"{type(exc).__name__}: {exc}"})

    # ---------------------------------------------------------------- 通知与跟进

    def send_mail(self, to: str, subject: str, body: str,
                  idempotency_key: str = "") -> dict:
        """发通知（经邮件网关连接器）。

        【连接器落地】原来直接打 Mock 的 /mail/send；现在按 `MAIL_GATEWAY` 选择
        mock / http / smtp / none。`idempotency_key` 交给网关去重（重复投递不重复发信）。
        """
        res = self._mail.send_mail(to=to, subject=subject, body=body,
                                   idempotency_key=idempotency_key)
        out = res.as_dict()
        if not res.ok:
            # 保持历史调用方的语义：失败时抛异常让上层计入 failed（单条失败不阻塞批次）
            raise IOError(res.error or "邮件发送失败")
        return out

    def schedule_scan(self, cron: str = DEFAULT_CRON, prompt: str = "") -> dict:
        """把"例行巡检"注册成框架的定时任务（到期由主 Agent 执行该提示词）。

        【实读结论】框架 cron 的语义是"把提示词发给 Agent 执行"
        （agent/tools/cron_tool.py:125），所以这里注册的是**提示词**，
        真正的扫描能力由本项目以 MCP 工具提供。
        """
        from agent.tools.cron_tool import create_cron_tools
        from channels.device.crontab import get_cron_manager
        tools = {t.name: t for t in create_cron_tools(get_cron_manager())}
        tool = tools.get("create_cron_task")
        if tool is None:
            return {"ok": False, "errors": ["框架未注册 create_cron_task 工具"]}
        text = prompt or (
            "执行一次合规收件目录例行巡检：调用 MCP 工具 proactive_scan（或等价服务）扫描共享盘收件目录，"
            "对发现的新合同主动发起合规审查，并把结果与待人工环节汇总后推送给合规负责人。")
        raw = tool.invoke({"prompt": text, "cron_expression": cron, "enabled": True})
        return {"ok": raw.startswith("✅"), "raw": raw, "cron": cron, "prompt": text}

    def schedule_followup(self, case_ref: str, missing: str, cron: str = FOLLOWUP_CRON,
                          due_days: int = 3) -> dict:
        """自排跟进任务：审查中发现材料缺失，员工自己安排到点提醒。

        due_days 只用于生成可读的到期说明；实际触发时间由 cron 表达式决定。
        """
        due = (datetime.now() + timedelta(days=due_days)).strftime("%Y-%m-%d")
        prompt = (f"跟进提醒：案件 {case_ref} 仍缺少材料「{missing}」（应于 {due} 前补齐）。"
                  f"请检查该案件当前状态；若仍未补齐，向合规专员发送提醒并登记待办。")
        res = self.schedule_scan(cron=cron, prompt=prompt)
        res.update({"case_ref": case_ref, "missing": missing, "due": due, "kind": "followup"})
        return res

    def list_scheduled(self) -> list[dict]:
        """列出已注册的定时任务（走 manager.list_tasks，直查数据库）。"""
        from channels.device.crontab import get_cron_manager
        mgr = get_cron_manager()
        out = []
        for t in mgr.list_tasks(include_disabled=True):
            out.append({
                "task_id": getattr(t, "task_id", ""),
                "cron": getattr(t, "cron_expression", ""),
                "enabled": bool(getattr(t, "enabled", False)),
                "prompt": (getattr(t, "prompt", "") or "")[:90],
            })
        return out
