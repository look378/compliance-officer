#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P4+/审查回归：决策与等待节点绑定 + update_state 语义 + SSE 增量游标

背景（审查发现）：
  1) decide() 原先不校验"决策属于当前等待的人工节点"，导致
     - run 停在 supervisor_verify 时，专员 confirm 会被写进主管节点输出字段（伪造签核）
     - run 停在 specialist_review 时，主管 approve 会单人一步办结（跳过主管复核）
  2) 服务层在 resume 之前调用 graph.update_state({"detail_decision": ...}），
     代码注释声称这样"不会提前推进图"。本脚本用真实框架行为做实测判定。
  3) 网关 run 事件流原先用定长切片做增量，超过 200 条后新事件永不推送。

产出：docs/evidence/p4-decision-binding.json
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p4-decision-binding.json"

CAP = PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py"

HIGH_RISK = ("劳动合同\n"
             "第一条 乙方月工作时长不超过 280 小时。\n"
             "第二条 甲方有权无条件解除本合同，最终解释权归甲方所有。\n")


def ensure_capability() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8770/health", timeout=2) as r:
            json.loads(r.read().decode("utf-8"))
            return "already-running"
    except Exception:
        pass
    log = PROJECT_ROOT / ".runtime" / "logs" / "decision-binding-mcp.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", encoding="utf-8")
    subprocess.Popen([sys.executable, str(CAP)], stdout=fh, stderr=subprocess.STDOUT,
                     cwd=str(PROJECT_ROOT),
                     env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
                          "PYTHONPATH": str(PROJECT_ROOT)})
    t0 = time.time()
    while time.time() - t0 < 30:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8770/health", timeout=2):
                return "started"
        except Exception:
            time.sleep(0.4)
    return "failed"


def main() -> int:
    ev = {"check": "P4+-decision-binding", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    checks: dict = {}
    problems: list[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    ev["capability_service"] = ensure_capability()
    if ev["capability_service"] == "failed":
        ev.update({"verdict": "FAIL", "checks": checks, "problems": ["能力服务启动失败"]})
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        return 1

    from service.review_orchestrator import ReviewOrchestrator
    orch = ReviewOrchestrator()
    orch.ensure_compiled()
    from mcp_server import queue_store

    sub = orch.submit(HIGH_RISK, submitted_by="binding-selfcheck")
    run_id = sub["run_id"]
    ev["run_id"] = run_id
    ev["submit"] = {k: sub.get(k) for k in ("ok", "status", "risk_level")}
    check("submit_paused_at_specialist",
          sub["ok"] and sub["status"] == "paused" and sub["risk_level"] == "high",
          {"status": sub["status"], "risk": sub["risk_level"]})

    # ---------- 1) 绑定：主管不能对停在专员节点的 run 直接 approve ----------
    wrong = orch.decide(run_id, "approve", actor="supervisor-zhang", role="supervisor")
    ev["cross_node_decision"] = wrong
    check("cross_node_decision_rejected",
          wrong.get("ok") is False and "不匹配" in " ".join(wrong.get("errors") or []),
          {"errors": wrong.get("errors"), "pending_node": wrong.get("pending_node")})
    st_after = orch.status(run_id)
    check("cross_node_decision_did_not_advance",
          st_after["status"] == "paused" and not (st_after.get("supervisor_note") or ""),
          {"status": st_after["status"], "supervisor_note": st_after.get("supervisor_note")})

    # ---------- 2) update_state 语义实测：写入 detail_decision 后图是否被推进 ----------
    cfg = {"configurable": {"thread_id": run_id}}
    before = orch.graph.get_state(cfg)
    orch.graph.update_state(cfg, {"detail_decision": "pending"})
    after = orch.graph.get_state(cfg)
    ev["update_state_semantics"] = {
        "next_before": list(before.next) if before else [],
        "next_after": list(after.next) if after else [],
        "decision_visible": (after.values or {}).get("detail_decision") if after else None,
        "note": "仅写 detail_decision（非人工节点输出字段）",
    }
    # 只要求：写入后仍停在同一个等待节点（即没有"提前推进到下一节点"）
    check("update_state_keeps_waiting_node",
          list(after.next) == list(before.next),
          ev["update_state_semantics"])

    # ---------- 3) 正常专员 confirm → 应停在主管节点，且 specialist_note 记录 confirm ----------
    d1 = orch.decide(run_id, "confirm", actor="specialist-li", role="specialist",
                     note="初审通过")
    ev["specialist_confirm"] = {k: d1.get(k) for k in ("ok", "status", "decision", "paused")}
    check("specialist_confirm_ok", d1.get("ok") and d1.get("status") == "paused",
          ev["specialist_confirm"])
    check("specialist_confirm_moves_to_supervisor",
          (d1.get("paused") or {}).get("role") == "supervisor",
          {"paused": d1.get("paused")})
    st = orch.status(run_id)
    ev["state_after_confirm"] = {"specialist_note": st.get("specialist_note"),
                                 "supervisor_note": st.get("supervisor_note"),
                                 "status": st.get("status")}
    check("specialist_note_records_confirm",
          (st.get("specialist_note") or "").strip().lower() == "confirm",
          ev["state_after_confirm"])
    check("supervisor_note_not_forged",
          not (st.get("supervisor_note") or "").strip(),
          {"supervisor_note": st.get("supervisor_note")})

    # ---------- 4) 专员不能对等待主管的 run 再下决策 ----------
    wrong2 = orch.decide(run_id, "confirm", actor="specialist-li", role="specialist")
    ev["reverse_cross_node"] = wrong2
    check("reverse_cross_node_rejected", wrong2.get("ok") is False,
          {"errors": wrong2.get("errors"), "pending_node": wrong2.get("pending_node")})

    # ---------- 5) 主管驳回（原先恒被拒绝的路径）----------
    rej = orch.decide(run_id, "reject", actor="supervisor-zhang", role="supervisor",
                      note="高风险条款未整改")
    ev["supervisor_reject"] = {k: rej.get(k) for k in ("ok", "status", "decision", "errors")}
    check("supervisor_reject_now_works",
          rej.get("ok") and rej.get("status") in ("completed", "paused"),
          ev["supervisor_reject"])

    # ---------- 6) SSE 增量游标：同秒多事件不漏不重 ----------
    cursor = ""
    seen_ids = []
    seen_events = []
    for _ in range(3):
        batch = queue_store.list_audit_since(run_id, cursor=cursor)
        for e in batch:
            cursor = e["audit_id"]
            seen_ids.append(e["audit_id"])
            seen_events.append(e["event"])
    fresh = queue_store.list_audit_since(run_id, cursor=cursor)
    ev["audit_cursor"] = {"events_seen": seen_events, "after_cursor": [e["event"] for e in fresh],
                          "total_in_db": queue_store.count_audit(run_id)}
    check("audit_cursor_no_loss",
          set(seen_events) >= {"review_submitted", "human_decision", "todo_created"},
          {"events": sorted(set(seen_events))})
    # 游标必须"每条只推一次"：audit_id 不重复，且推完等于库内总条数
    check("audit_cursor_no_duplicate",
          len(seen_ids) == len(set(seen_ids))
          and len(seen_ids) == queue_store.count_audit(run_id),
          {"pushed": len(seen_ids), "unique": len(set(seen_ids)),
           "total_in_db": queue_store.count_audit(run_id)})

    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n[binding] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")
    print(f"[binding] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
