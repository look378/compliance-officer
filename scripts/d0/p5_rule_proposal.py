#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P5 端到端：规则提案工作流（meta-compliance 最小闭环）

业务红线（任务书 §4.3，必须逐条验证）：
    1) 驳回原因**绝不直接写规则库**：只有主管批准才落规则表
    2) 去重：模式精确匹配 / 字面核心包含 / difflib 相似度 > 0.85 → 转「修订提案」
    3) 冲突：同模式异等级 → 转「待主管裁决」，**禁止直接写入**
    4) 审计：条目内联审计字段（source/approved_by/approved_at/changelog）+ 审计事件
    5) 生效：批准后同类条款**再次自动命中**，且带版本号与出处

覆盖用例：
    A 新增规则闭环：提案(new) → 审批前不生效 → 主管批准 → 版本化写入 → 再次命中
    B 冲突禁止写入：同模式异等级 → 审批被拒且规则表指纹不变
    C 精确去重：同模式再提案 → 自动转修订（version+1）
    D 并发审批互斥：两个主管同时批准同一提案 → 仅 1 次生效
    E 驳回提案：不影响规则表，且提案状态可查

自检会写规则表，因此结束时**快照还原**并比对指纹；同时清理本次产生的提案与待办。

产出：docs/evidence/p5-rule-proposal.json
"""

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, paths  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p5-rule-proposal.json"

NEW_PATTERN = "自动续约.{0,6}(无需|不需).{0,4}(通知|告知)"
NEW_CLAUSE = "本合同到期后自动续约，无需另行通知乙方。"
REF = "《民法典》第 470 条"


def ensure_capability_service() -> tuple[subprocess.Popen | None, dict]:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8770/health", timeout=2) as r:
            return None, json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    log = PROJECT_ROOT / ".runtime" / "logs" / "p5-mcp-server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", encoding="utf-8")
    fh.write(f"\n===== P5 启动能力服务 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
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
    raise RuntimeError("能力服务启动失败")


def main() -> int:
    ev = {"flow": "P5-rule-proposal", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "cases": {}}
    checks: dict[str, dict] = {}
    problems: list[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    proc, health = ensure_capability_service()
    ev["capability_service"] = {"started_by_this_run": proc is not None, "health": health}

    from service.review_orchestrator import ReviewOrchestrator
    from mcp_server import rules_store, tools_registry as T
    orch = ReviewOrchestrator()

    # ---- 快照规则表，保证自检可重复且不污染唯一真相源 ----
    snap_dir = PROJECT_ROOT / ".runtime" / "logs" / "p5-snapshot"
    snap_dir.mkdir(parents=True, exist_ok=True)
    rules_snapshot = snap_dir / "risk_rules.yaml"
    shutil.copy2(rules_store.RULES_PATH, rules_snapshot)
    digest_before = rules_store.digest()
    ev["rule_table_before"] = {"digest": digest_before,
                               "active": len(rules_store.active()),
                               "total": len(rules_store.load_all())}
    created_proposals: list[str] = []
    before_proposals = {p.get("proposal_id") for p in orch.proposals()["proposals"]}
    print(f"[P5] 规则表快照完成：active={ev['rule_table_before']['active']} 指纹={digest_before}")

    def run_id(tag: str) -> str:
        return f"p5_{tag}_{int(time.time() * 1000) % 1000000}"

    try:
        # ---------------- A 新增规则闭环 ----------------
        rid_a = run_id("new")
        p = orch.propose_rule(reject_reason="合同约定自动续约但未约定通知义务，存在续约争议与费用风险",
                              pattern=NEW_PATTERN, severity="mid", regulation_ref=REF,
                              suggestion="明确续约通知期限与拒绝续约方式",
                              proposed_by="specialist-li", run_id=rid_a)
        created_proposals.append(p.get("proposal_id", ""))
        before_hit = T._annotate_clause(NEW_CLAUSE)
        before_has_new = any(h["rule_id"] == p.get("proposed_rule_id")
                             for h in before_hit["risk_hits"])
        check("A1_proposal_created_as_new",
              p.get("ok") and p.get("kind") == "new" and p.get("proposed_rule_id"),
              {"kind": p.get("kind"), "id": p.get("proposal_id"),
               "rule_id": p.get("proposed_rule_id"), "dedup": p.get("dedup")})
        check("A2_not_effective_before_approval", before_has_new is False,
              {"hits_before": [h["rule_id"] for h in before_hit["risk_hits"]]})

        approve = orch.review_proposal(p["proposal_id"], "approve", "supervisor-zhang",
                                      note="风险描述准确，纳入规则库")
        after_hit = T._annotate_clause(NEW_CLAUSE)
        new_hits = [h for h in after_hit["risk_hits"] if h["rule_id"] == approve.get("rule_id")]
        digest_after_approve = rules_store.digest()
        check("A3_approved_writes_versioned_rule",
              approve.get("ok") and approve.get("version") == 1
              and digest_after_approve != digest_before,
              {"rule_id": approve.get("rule_id"), "version": approve.get("version"),
               "digest_before": digest_before, "digest_after": digest_after_approve})
        check("A4_rule_takes_effect_with_audit_fields",
              bool(new_hits) and new_hits[0]["rule_version"] == 1
              and new_hits[0].get("regulation_ref") == REF,
              {"hits": [{"id": h["rule_id"], "v": h["rule_version"],
                         "ref": h.get("regulation_ref")} for h in after_hit["risk_hits"]]})
        stored = rules_store.get(approve.get("rule_id") or "")
        check("A5_rule_entry_has_audit_fields",
              stored and stored.get("approved_by") == "supervisor-zhang"
              and stored.get("source") == "proposal" and stored.get("changelog"),
              {"entry": {k: stored.get(k) for k in
                         ("id", "version", "status", "source", "approved_by", "approved_at",
                          "changelog")} if stored else None})
        print(f"[P5][A] 新增闭环: 提案 {p.get('proposal_id')} -> 规则 {approve.get('rule_id')} "
              f"v{approve.get('version')}；批准前命中={before_has_new}，批准后命中="
              f"{[h['rule_id'] for h in after_hit['risk_hits']]}")
        ev["cases"]["A_new_rule"] = {"proposal": p, "approve": approve,
                                     "hits_before": [h["rule_id"] for h in before_hit["risk_hits"]],
                                     "hits_after": [h["rule_id"] for h in after_hit["risk_hits"]],
                                     "stored_rule": stored,
                                     "digest_after": digest_after_approve}

        # ---------------- B 冲突禁止写入 ----------------
        rid_b = run_id("conflict")
        pc = orch.propose_rule(reject_reason="等级判断分歧：认为最终解释权应为高风险",
                               pattern="最终解释权", severity="high", regulation_ref="《民法典》第 498 条",
                               suggestion="按高风险处理", proposed_by="specialist-li", run_id=rid_b)
        created_proposals.append(pc.get("proposal_id", ""))
        digest_pre_conflict = rules_store.digest()
        appr_conflict = orch.review_proposal(pc["proposal_id"], "approve", "supervisor-zhang")
        digest_post_conflict = rules_store.digest()
        check("B1_conflict_proposal_flagged",
              pc.get("kind") == "conflict" and pc.get("conflict_requires_manual")
              and pc.get("conflict_with"),
              {"kind": pc.get("kind"), "conflicts": pc.get("conflict_with")})
        check("B2_conflict_approval_refused_and_rules_intact",
              appr_conflict.get("ok") is False and digest_pre_conflict == digest_post_conflict
              and any("冲突" in e for e in appr_conflict.get("errors", [])),
              {"errors": appr_conflict.get("errors"),
               "digest_same": digest_pre_conflict == digest_post_conflict})
        print(f"[P5][B] 冲突禁止写入: kind={pc.get('kind')} 审批被拒={appr_conflict.get('ok') is False} "
              f"规则表未变={digest_pre_conflict == digest_post_conflict}")
        ev["cases"]["B_conflict"] = {"proposal": pc, "approve": appr_conflict,
                                     "digest_same": digest_pre_conflict == digest_post_conflict}

        # ---------------- C 精确去重 → 修订提案 ----------------
        rid_c = run_id("dedup")
        p2 = orch.propose_rule(reject_reason="同一问题再次出现", pattern=NEW_PATTERN, severity="mid",
                               regulation_ref=REF, suggestion="补充通知期限的量化要求",
                               proposed_by="specialist-li", run_id=rid_c)
        created_proposals.append(p2.get("proposal_id", ""))
        check("C1_exact_dedup_to_revision",
              p2.get("kind") == "revise" and p2.get("target_rule_id") == approve.get("rule_id")
              and p2.get("version") == 2 and p2.get("dedup", {}).get("layer") == "exact",
              {"kind": p2.get("kind"), "target": p2.get("target_rule_id"),
               "version": p2.get("version"), "layer": p2.get("dedup", {}).get("layer")})
        appr2 = orch.review_proposal(p2["proposal_id"], "approve", "supervisor-liu")
        after_v2 = T._annotate_clause(NEW_CLAUSE)
        v2_hits = [h for h in after_v2["risk_hits"] if h["rule_id"] == approve.get("rule_id")]
        check("C2_revision_bumps_version",
              appr2.get("ok") and appr2.get("version") == 2 and v2_hits
              and v2_hits[0]["rule_version"] == 2,
              {"approve": {k: appr2.get(k) for k in ("ok", "version", "rules_digest")},
               "hit_version": v2_hits[0]["rule_version"] if v2_hits else None})
        print(f"[P5][C] 精确去重: 转修订 version={p2.get('version')} -> 批准后命中版本="
              f"{v2_hits[0]['rule_version'] if v2_hits else None}")
        ev["cases"]["C_dedup_revision"] = {"proposal": p2, "approve": appr2,
                                           "hit_version": v2_hits[0]["rule_version"] if v2_hits else None}

        # ---------------- D 并发审批互斥 ----------------
        rid_d = run_id("concurrent")
        pd = orch.propose_rule(reject_reason="并发审批测试用的独立规则",
                               pattern="签署前.{0,6}未.{0,4}(审阅|复核)", severity="low",
                               regulation_ref="《民法典》第 470 条", suggestion="补充签署前复核要求",
                               proposed_by="specialist-li", run_id=rid_d)
        created_proposals.append(pd.get("proposal_id", ""))
        results = []
        lock = threading.Lock()

        def approve_concurrent(i):
            r = orch.review_proposal(pd["proposal_id"], "approve", f"supervisor-{i}")
            with lock:
                results.append({"i": i, "ok": r.get("ok"), "claimed": r.get("claimed"),
                                "errors": r.get("errors")})

        threads = [threading.Thread(target=approve_concurrent, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        accepted = [r for r in results if r["ok"]]
        check("D1_concurrent_approve_single",
              len(accepted) == 1,
              {"accepted": len(accepted), "results": results})
        print(f"[P5][D] 并发审批: 4 个请求，接受 {len(accepted)} 次"
              f"（拒绝示例：{(results[[r['ok'] for r in results].index(False)]['errors'] or [''])[0] if len(accepted) < 4 else 'n/a'}）")
        ev["cases"]["D_concurrency"] = {"proposal_id": pd.get("proposal_id"),
                                        "accepted": len(accepted), "results": results}

        # ---------------- E 驳回提案不改规则表 ----------------
        rid_e = run_id("reject")
        pe = orch.propose_rule(reject_reason="主管认为该模式误报率高",
                               pattern="争议解决地.{0,6}(仲裁|法院)", severity="low",
                               regulation_ref="《民事诉讼法》第 35 条", suggestion="提示核对管辖",
                               proposed_by="specialist-li", run_id=rid_e)
        created_proposals.append(pe.get("proposal_id", ""))
        digest_pre_reject = rules_store.digest()
        rej = orch.review_proposal(pe["proposal_id"], "reject", "supervisor-zhang",
                                  note="先观察误报率，暂不入库")
        digest_post_reject = rules_store.digest()
        prop_after = [x for x in orch.proposals()["proposals"]
                      if x.get("proposal_id") == pe.get("proposal_id")]
        check("E1_reject_keeps_rule_table_intact",
              rej.get("ok") and digest_pre_reject == digest_post_reject
              and prop_after and prop_after[0].get("status") == "rejected",
              {"status": prop_after[0].get("status") if prop_after else None,
               "digest_same": digest_pre_reject == digest_post_reject})
        rej_again = orch.review_proposal(pe["proposal_id"], "reject", "supervisor-zhang")
        check("E2_repeat_review_refused", rej_again.get("ok") is False,
              {"errors": rej_again.get("errors")})
        print(f"[P5][E] 驳回: 状态={prop_after[0].get('status') if prop_after else None} "
              f"规则表未变={digest_pre_reject == digest_post_reject} 重复审批被拒={rej_again.get('ok') is False}")
        ev["cases"]["E_reject"] = {"proposal": pe, "reject": rej, "reject_again": rej_again,
                                   "digest_same": digest_pre_reject == digest_post_reject}

        # ---------------- F 审计事件 ----------------
        from mcp_server import queue_store
        events = [e["event"] for e in queue_store.list_audit(limit=400)]
        check("F_audit_events_recorded",
              "rule_proposal_approved" in events and "rule_proposal_rejected" in events,
              {"has_approved": "rule_proposal_approved" in events,
               "has_rejected": "rule_proposal_rejected" in events})
        print(f"[P5][F] 审计事件: 已记录批准与驳回（总数 {len(events)} 条）")
        ev["audit_events"] = {"has_approved": "rule_proposal_approved" in events,
                              "has_rejected": "rule_proposal_rejected" in events}

    finally:
        # ---- 还原规则表 + 清理本次提案与待办 ----
        shutil.copy2(rules_snapshot, rules_store.RULES_PATH)
        removed = []
        for pid in created_proposals:
            if not pid:
                continue
            f = rules_store.PROPOSALS_DIR / f"{pid}.json"
            if f.exists():
                f.unlink()
                removed.append(pid)
        from mcp_server import queue_store
        cleaned_todos = 0
        for t in queue_store.list_todos(role="supervisor", status="open", limit=300):
            if any(pid and (pid in (t.get("title") or "") or pid in (t.get("payload_json") or ""))
                   for pid in created_proposals):
                queue_store.complete_todo(t["todo_id"], "selfcheck-cleanup", "cancelled",
                                          "P5 自检清理")
                cleaned_todos += 1
        digest_restored = rules_store.digest()
        after_proposals = {p.get("proposal_id") for p in orch.proposals()["proposals"]}
        ev["cleanup"] = {"rules_digest_before": digest_before,
                         "rules_digest_restored": digest_restored,
                         "rules_restored": digest_restored == digest_before,
                         "proposals_removed": removed,
                         "todos_closed": cleaned_todos,
                         "proposals_new_left": sorted(after_proposals - before_proposals)}
        if not ev["cleanup"]["rules_restored"]:
            problems.append("rules_restore_mismatch")
        if ev["cleanup"]["proposals_new_left"]:
            problems.append("proposal_cleanup_incomplete")
        print(f"[P5] 还原规则表指纹一致={ev['cleanup']['rules_restored']}；"
              f"清理提案 {len(removed)} 条、待办 {cleaned_todos} 条")

    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ev["paths"] = paths()

    print(f"\n[P5] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[P5] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
