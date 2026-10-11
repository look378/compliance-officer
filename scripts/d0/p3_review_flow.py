#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P3 端到端：合规审查主流程（workflows/compliance_review.json）

覆盖的路径（对应任务书 §4.2 与验收清单）：
    A 低风险：自动办结（无人工节点）
    B 中风险：专员确认 → 主管复核 → 归档
    C 高风险：专员 modify（重新标注）→ 循环收敛 → 确认 → 主管复核 → 归档
    D 驳回：专员 reject → 生成规则提案（不直接改规则表）→ 归档

实现方式（P7 后统一）：
    走**服务层编排器**（service/review_orchestrator.py）而不是直接驱动图。
    原因：P4/P7 的实测发现，框架把 resume 的值写入"当前等待的 human 节点"的输出字段，
    因此路由必须由服务层显式写入 detail_decision 字段；直接驱动图会绕过这条纪律。
    本脚本因此同时验证"图编排"与"服务层决策入口"两条链路。

产出：docs/evidence/p3-review-flow.json
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, paths  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SPEC = PROJECT_ROOT / "workflows" / "compliance_review.json"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p3-review-flow.json"

CONTRACTS = {
    "low": (
        "技术服务合同\n"
        "第一条 甲方委托乙方提供系统运维服务，服务期限一年。\n"
        "第二条 甲方应在验收合格后 30 日内支付服务费。\n"
        "第三条 双方应按约定时间完成交付，验收标准以附件一为准。\n"
        "第四条 乙方按约定开具增值税专用发票。\n"
    ),
    "mid": (
        "技术服务合同（中等风险示例）\n"
        "第一条 甲方委托乙方提供系统运维服务，服务期限一年。\n"
        "第二条 乙方违反保密义务的，违约金为合同总额的 50%。\n"
        "第三条 甲方应在验收合格后 30 日内支付服务费。\n"
    ),
    "high": (
        "劳动合同（高风险示例）\n"
        "第一条 本合同为固定期限劳动合同，期限三年。\n"
        "第二条 乙方月工作时长不超过 280 小时，甲方可安排加班。\n"
        "第三条 甲方有权无条件解除本合同，最终解释权归甲方所有。\n"
        "第四条 本产品保本，年化收益不低于 5%。\n"
        "第五条 甲方收集乙方个人信息无需取得同意，亦无需另行授权。\n"
    ),
}


def ensure_mcp_server() -> tuple[subprocess.Popen | None, dict]:
    """确保岗位能力服务在线（工作流在编译期从主 Agent 工具表取工具，顺序不能颠倒）。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8770/health", timeout=2) as r:
            return None, json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    log = PROJECT_ROOT / ".runtime" / "logs" / "p3-mcp-server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", encoding="utf-8")
    fh.write(f"\n===== P3 启动能力服务 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    fh.flush()
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py"),
         "--port", "8770"],
        stdout=fh, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
             "PYTHONPATH": str(PROJECT_ROOT)})
    t0 = time.time()
    while time.time() - t0 < 25:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8770/health", timeout=2) as r:
                return proc, json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("岗位能力服务启动失败，见 .runtime/logs/p3-mcp-server.log")


def snapshot(orch, run_id: str) -> dict:
    st = orch.graph.get_state({"configurable": {"thread_id": run_id}})
    values = st.values or {}
    ann = values.get("annotation")
    ann = ann if isinstance(ann, dict) else {}
    tasks = [getattr(t, "name", "") for t in (st.tasks or [])]
    return {
        "next": list(st.next), "tasks": tasks,
        "risk": values.get("overall_risk_level"),
        "detail_decision": values.get("detail_decision"),
        "specialist_note": values.get("specialist_note"),
        "supervisor_note": values.get("supervisor_note"),
        "hit_rule_ids": sorted({h.get("rule_id") for a in (ann.get("annotations") or [])
                                for h in (a.get("risk_hits") or []) if h.get("rule_id")}),
        "risky_clause_count": ann.get("risky_clause_count"),
        "clause_count": ann.get("clause_count"),
        "decision_summary": values.get("decision_summary"),
    }


def main() -> int:
    ev = {"flow": "P3-compliance-review", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "spec": str(SPEC), "cases": {}}
    checks: dict[str, dict] = {}
    problems: list[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    proc, health = ensure_mcp_server()
    ev["capability_service"] = {"started_by_this_run": proc is not None, "health": health}
    print(f"[P3] 岗位能力服务: {health.get('status')} v{health.get('version')}")

    from service.review_orchestrator import ReviewOrchestrator
    orch = ReviewOrchestrator()
    compiled = orch.ensure_compiled()
    ev["compiled"] = compiled
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    ev["spec_nodes"] = [n.get("name") for n in spec.get("nodes", [])]
    ev["spec_conditionals"] = [{"src": c.get("src"), "field": (c.get("router") or {}).get("field"),
                                "mapping": list((c.get("mapping") or {}).keys())}
                               for c in spec.get("conditionals", [])]
    print(f"[P3] 工作流 {compiled['workflow_id']} v{compiled['version']}；"
          f"节点 {ev['spec_nodes']}")

    # ---------------- A 低风险自动办结 ----------------
    ra = orch.submit(CONTRACTS["low"], submitted_by="oa-bot")
    sa = snapshot(orch, ra["run_id"])
    st_a = orch.status(ra["run_id"])
    check("A_low_risk_auto_completes",
          ra["status"] == "completed" and ra["risk_level"] == "low" and not ra["paused"]
          and sa["risk"] == "low"
          and any(e["event"] == "review_archived" for e in st_a["audit"]),
          {"status": ra["status"], "risk": ra["risk_level"], "paused": ra["paused"],
           "audit": [e["event"] for e in st_a["audit"]]})
    print(f"[P3][A] 低风险: {ra['status']} 风险={ra['risk_level']} 人工节点={bool(ra['paused'])}")
    ev["cases"]["A_low_risk"] = {"submit": ra, "state": sa,
                                 "audit": [e["event"] for e in st_a["audit"]]}

    # ---------------- B 中风险：专员确认 → 主管复核 ----------------
    rb = orch.submit(CONTRACTS["mid"], submitted_by="oa-bot")
    sb1 = snapshot(orch, rb["run_id"])
    d1 = orch.decide(rb["run_id"], "confirm", "specialist-li", "specialist", note="中风险确认")
    sb2 = snapshot(orch, rb["run_id"])
    d2 = orch.decide(rb["run_id"], "approve", "supervisor-zhang", "supervisor", note="同意归档")
    st_b = orch.status(rb["run_id"])
    events_b = [e["event"] for e in st_b["audit"]]
    check("B_mid_two_human_nodes",
          rb["status"] == "paused" and sb1["tasks"] == ["specialist_review"]
          and d1["ok"] and d1["status"] == "paused"
          and sb2["tasks"] == ["supervisor_verify"]
          and d2["ok"] and d2["status"] == "completed"
          and sb2["risk"] == "mid",
          {"pause1_tasks": sb1["tasks"], "pause2_tasks": sb2["tasks"],
           "d1": {k: d1.get(k) for k in ("ok", "status")},
           "d2": {k: d2.get(k) for k in ("ok", "status")}, "risk": sb2["risk"]})
    check("B_audit_chain",
          all(e in events_b for e in ("review_submitted", "human_decision", "review_archived")),
          {"events": events_b})
    print(f"[P3][B] 中风险: 暂停节点 {sb1['tasks']} → {sb2['tasks']} → {d2['status']}；"
          f"审计 {events_b}")
    ev["cases"]["B_mid_risk"] = {"submit": rb, "pause1": sb1, "decide1": d1,
                                 "pause2": sb2, "decide2": d2,
                                 "audit": events_b}

    # ---------------- C 高风险：modify 重标注循环 ----------------
    rc = orch.submit(CONTRACTS["high"], submitted_by="oa-bot")
    sc1 = snapshot(orch, rc["run_id"])
    dm = orch.decide(rc["run_id"], "modify", "specialist-li", "specialist",
                     note="要求重新标注收益条款")
    sc2 = snapshot(orch, rc["run_id"])
    dc = orch.decide(rc["run_id"], "confirm", "specialist-li", "specialist", note="复核后确认")
    sc3 = snapshot(orch, rc["run_id"])
    ds = orch.decide(rc["run_id"], "approve", "supervisor-zhang", "supervisor", note="高风险签署通过")
    st_c = orch.status(rc["run_id"])
    events_c = [e["event"] for e in st_c["audit"]]
    check("C_high_cycle_converges",
          sc1["risk"] == "high" and sc1["tasks"] == ["specialist_review"]
          and dm["ok"] and dm["status"] == "paused" and sc2["tasks"] == ["specialist_review"]
          and dc["ok"] and sc3["tasks"] == ["supervisor_verify"]
          and ds["ok"] and ds["status"] == "completed",
          {"pause1": sc1["tasks"], "after_modify": sc2["tasks"],
           "after_confirm": sc3["tasks"], "final": ds.get("status")})
    check("C_reannotation_produced_summary",
          "reannotated" in events_c and bool(sc2["decision_summary"]) and bool(sc2["hit_rule_ids"]),
          {"events": events_c, "rules": sc2["hit_rule_ids"],
           "summary": sc2["decision_summary"]})
    print(f"[P3][C] 高风险: {sc1['tasks']} → modify → {sc2['tasks']} → {sc3['tasks']} → "
          f"{ds['status']}；重标注命中 {sc2['hit_rule_ids']}")
    ev["cases"]["C_high_risk_cycle"] = {"submit": rc, "pause1": sc1, "modify": dm,
                                        "after_modify": sc2, "confirm": dc,
                                        "after_confirm": sc3, "approve": ds,
                                        "audit": events_c}

    # ---------------- D 驳回 -> 规则提案 ----------------
    from mcp_server import rules_store
    rules_before = rules_store._digest_of(rules_store.load_all())
    rd = orch.submit(CONTRACTS["mid"], submitted_by="oa-bot")
    dr = orch.decide(rd["run_id"], "reject", "specialist-li", "specialist",
                     note="该条款未约定续约通知义务，属重大遗漏")
    prop = orch.propose_rule(reject_reason="合同约定自动续约但未约定通知义务，存在续约争议与费用风险",
                             pattern="自动续约.{0,6}(无需|不需).{0,4}(通知|告知)", severity="mid",
                             regulation_ref="《民法典》第 470 条",
                             suggestion="明确续约通知期限与拒绝续约方式",
                             contract_id="CT-P3-D", proposed_by="specialist-li",
                             run_id=rd["run_id"])
    st_d = orch.status(rd["run_id"])
    rules_after = rules_store._digest_of(rules_store.load_all())
    check("D_reject_generates_proposal_no_rule_write",
          dr["ok"] and dr["status"] == "completed" and prop.get("ok") and prop.get("proposal_id")
          and rules_before == rules_after,
          {"status": dr.get("status"), "proposal": prop.get("proposal_id"),
           "kind": prop.get("kind"), "rules_unchanged": rules_before == rules_after})
    check("D_proposal_queued_to_supervisor",
          any("规则提案待审" in (t.get("title") or "")
              for t in st_d["todos"]),
          {"todos": [t.get("title") for t in st_d["todos"]]})
    # 清理该提案，避免累积（自检卫生）
    pf = rules_store.PROPOSALS_DIR / f"{prop.get('proposal_id')}.json"
    if pf.exists():
        pf.unlink()
    print(f"[P3][D] 驳回: {dr.get('status')} 提案={prop.get('proposal_id')} "
          f"（{prop.get('kind')}）规则表未变={rules_before == rules_after}")
    ev["cases"]["D_reject_path"] = {"submit": rd, "reject": dr, "proposal": prop,
                                    "rules_unchanged": rules_before == rules_after,
                                    "audit": [e["event"] for e in st_d["audit"]]}

    # ---------------- E 证据链 ----------------
    from mcp_server import queue_store
    total_events = 0
    for case in (ra, rb, rc, rd):
        total_events += len(queue_store.list_audit(run_id=case["run_id"], limit=100))
    check("E_evidence_chain_complete", total_events >= 12,
          {"audit_events_total": total_events})
    check("E_rule_table_intact",
          rules_store._digest_of(rules_store.load_all()) == rules_before,
          {"digest": rules_before})
    print(f"[P3][E] 证据链事件 {total_events} 条；规则表指纹一致="
          f"{rules_store._digest_of(rules_store.load_all()) == rules_before}")

    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ev["paths"] = paths()

    print(f"\n[P3] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[P3] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
