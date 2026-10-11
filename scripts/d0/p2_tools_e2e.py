#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P2 自检 B：岗位能力工具端到端自检（不启服务，直接调工具函数）

覆盖 15 个工具的关键路径与失败路径：
    文档类：解析 docx/pdf/文本；表格抽取（含"无表格"与"扫描件"两种显式说明）
    检索类：命中 / 领域过滤 / 空检索必须显式声明
    判定类：单条标注（high/mid/low）、整篇标注与汇总分级
    报告类：Markdown + PDF 生成
    规则类：提案去重（精确/相似/新增）、冲突提示、批准写入版本化、重复批准被拒
    流程类：待办创建幂等、原子领取互斥、决策审计、指标快照
产出：docs/evidence/p2-tools-e2e.json
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p2-tools-e2e.json"
FIX = PROJECT_ROOT / ".runtime" / "workspace" / "p2_fixtures"

CONTRACT_LINES = [
    "劳动合同（示范，用于自检）",
    "甲方：示例科技有限公司    乙方：张三",
    "第一条 本合同为固定期限劳动合同，期限三年，试用期六个月。",
    "第二条 乙方月工作时长不超过 280 小时，甲方可安排加班。",
    "第三条 甲方有权无条件解除本合同，最终解释权归甲方所有。",
    "第四条 乙方违反保密义务的，违约金为合同总额的 50%。",
    "第五条 甲方收集乙方个人信息无需取得同意，亦无需另行授权。",
    "第六条 双方应按约定时间完成交付，验收标准以附件一为准。",
    "附表一：付款计划如下表所示。",
]


def build_fixtures() -> dict:
    FIX.mkdir(parents=True, exist_ok=True)
    out = {}

    # 1) docx：段落 + 表格
    try:
        import docx
        d = docx.Document()
        for line in CONTRACT_LINES:
            d.add_paragraph(line)
        t = d.add_table(rows=3, cols=3)
        data = [["期次", "比例", "金额(元)"], ["首期", "30%", "30000"], ["尾期", "70%", "70000"]]
        for i, row in enumerate(data):
            for j, val in enumerate(row):
                t.cell(i, j).text = val
        p = FIX / "contract_with_table.docx"
        d.save(str(p))
        out["docx"] = {"ok": True, "path": str(p), "bytes": p.stat().st_size}
    except Exception as exc:
        out["docx"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 2) 文本
    p = FIX / "contract.txt"
    p.write_text("\n".join(CONTRACT_LINES), encoding="utf-8")
    out["txt"] = {"ok": True, "path": str(p), "bytes": p.stat().st_size}

    # 3) 原生文本 PDF（带中文）
    try:
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        font = "Helvetica"
        for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"):
            try:
                pdfmetrics.registerFont(TTFont("CJK", cand))
                font = "CJK"
                break
            except Exception:
                continue
        p = FIX / "contract_text.pdf"
        c = canvas.Canvas(str(p))
        c.setFont(font, 11)
        y = 800
        for line in CONTRACT_LINES:
            c.drawString(50, y, line)
            y -= 20
        c.save()
        out["pdf_text"] = {"ok": True, "path": str(p), "bytes": p.stat().st_size}
    except Exception as exc:
        out["pdf_text"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 4) 图片型 PDF（模拟扫描件，无文本层）
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (1200, 400), "white")
        d = ImageDraw.Draw(img)
        f = None
        for cand in (r"C:\Windows\Fonts\msyh.ttc",):
            try:
                f = ImageFont.truetype(cand, 18)
            except Exception:
                pass
        y = 20
        for line in CONTRACT_LINES[:6]:
            d.text((15, y), line, fill="black", font=f)
            y += 40
        p = FIX / "scanned.pdf"
        img.save(p, "PDF", resolution=150)
        out["pdf_scanned"] = {"ok": True, "path": str(p), "bytes": p.stat().st_size}
    except Exception as exc:
        out["pdf_scanned"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def _cleanup_selftest(run_id: str) -> dict:
    """清理自检产生的待办与审计记录（保持环境可重复运行）。

    【审查修正】加 run_id 非空保护：自检若在创建待办之前就异常，
    finally 会以空 run_id 调用本函数，而 `WHERE run_id = ''` 会命中
    **系统级审计事件**（认证类事件的 run_id 就是空串）—— 必须避免误删。

    【第二次审查修正】原来直接 `sqlite3.connect(get_db_path())`：`DB_MODE=pg` 时
    待办/审计在 PostgreSQL 里，这行删的是**另一个库**（删不掉也不报错），
    自检数据会永久留在真库里。现在统一走 `mcp_server.db.connect()`（按后端分派）。
    """
    if not (run_id or "").strip():
        return {"skipped": "run_id 为空（自检未创建待办），不执行清理"}
    from mcp_server import db as db_mod
    conn = db_mod.connect()
    try:
        t = conn.execute("DELETE FROM compliance_todo WHERE run_id = ?", (run_id,)).rowcount
        a = conn.execute("DELETE FROM compliance_audit WHERE run_id = ?", (run_id,)).rowcount
        conn.commit()
        return {"todos_deleted": t, "audit_deleted": a, "backend": db_mod.describe().get("db_mode")}
    finally:
        conn.close()


def _body(ev: dict, checks: dict, problems: list, check) -> None:
    """自检主体。状态由 `main()` 持有，因此异常路径也能落证据。"""
    from mcp_server import tools_registry as T

    fixtures = build_fixtures()
    ev["fixtures"] = fixtures
    print(f"[e2e] 测试件: { {k: v['ok'] for k, v in fixtures.items()} }")

    # ---------------- 文档类 ----------------
    r = T._service_info()
    check("service_info_ok", r["ok"] and len(r["tools"]) >= 15,
          {"tools": len(r["tools"]), "regulations": r["regulations"]["count"],
           "rules_active": r["rule_table"]["active"]})
    print(f"[e2e] 工具 {len(r['tools'])} 个；法规 {r['regulations']['count']} 条；"
          f"规则 active {r['rule_table']['active']}；档位 {r['ocr_tier']}")

    if fixtures["docx"]["ok"]:
        p = fixtures["docx"]["path"]
        parsed = T._parse_contract(path=p)
        check("parse_docx_ok", parsed["ok"] and parsed["clause_count"] >= 5 and parsed["tables_found"] >= 1,
              {"clauses": parsed["clause_count"], "tables": parsed["tables_found"],
               "confidence": parsed["confidence"], "engine": parsed["engine"]})
        tabs = T._extract_tables(path=p)
        md_ok = tabs["table_count"] >= 1 and "|" in tabs["tables"][0]["markdown"]
        check("docx_table_markdown", md_ok, {"tables": tabs["table_count"]})
        print(f"[e2e] docx 解析: 条款 {parsed['clause_count']}，表格 {parsed['tables_found']}，"
              f"置信度 {parsed['confidence']}")

    if fixtures["pdf_text"]["ok"]:
        parsed = T._parse_contract(path=fixtures["pdf_text"]["path"])
        check("parse_pdf_native_ok", parsed["ok"] and parsed["clause_count"] >= 4 and parsed["confidence"] >= 0.6,
              {"clauses": parsed["clause_count"], "confidence": parsed["confidence"],
               "tier": parsed["ocr_tier"]})
        print(f"[e2e] 文本 PDF 解析: 条款 {parsed['clause_count']}，置信度 {parsed['confidence']}")

    if fixtures["pdf_scanned"]["ok"]:
        parsed = T._parse_contract(path=fixtures["pdf_scanned"]["path"])
        # 【OCR 升级后语义变化，必须两种形态都成立】
        #   ① 无 OCR 引擎（本机未装）：不能给出低风险结论 → 低置信度 + "扫描件"告警（转人工）
        #   ② 有 OCR 引擎（本机装了 RapidOCR 等）：真的把位图里的字识别出来 → 置信度提升、
        #      并显式标注 `ocr_tier` 与"已用 OCR"告警（解析来源必须可见）
        # 两者都不能"静默当成原生文本"：这正是升级过程中要守住的边界。
        ocr_tier = parsed.get("ocr_tier")
        warns = " ".join(parsed.get("warnings", []))
        if ocr_tier in ("ocr_lite", "ocr_full"):
            check("scanned_pdf_flagged",
                  parsed["ok"] and parsed["confidence"] > 0.2 and "已用 OCR" in warns
                  and (parsed.get("ocr") or {}).get("engine"),
                  {"tier": ocr_tier, "confidence": parsed["confidence"],
                   "engine": (parsed.get("ocr") or {}).get("engine"), "warnings": warns[:200]})
        else:
            check("scanned_pdf_flagged",
                  parsed["ok"] and parsed["confidence"] <= 0.2
                  and parsed.get("low_confidence") is True
                  and any("扫描件" in w for w in parsed.get("warnings", [])),
                  {"tier": ocr_tier, "confidence": parsed["confidence"],
                   "warnings": parsed.get("warnings")})
        print(f"[e2e] 扫描件 PDF: 档位={ocr_tier} 置信度 {parsed['confidence']}，"
              f"引擎={(parsed.get('ocr') or {}).get('engine')}")

    txt = T._parse_contract(text="\n".join(CONTRACT_LINES))
    check("parse_text_ok", txt["ok"] and txt["clause_count"] >= 5, {"clauses": txt["clause_count"]})

    # ---------------- 检索类 ----------------
    s1 = T._search_regulations("月工作时长 260 小时", top_k=3)
    check("search_hit", s1["ok"] and not s1["empty_result"] and s1["hits"][0]["regulation_ref"],
          {"top": [h["regulation_ref"] for h in s1["hits"]]})
    s2 = T._search_regulations("保本收益", top_k=3, domain="金融销售")
    check("search_domain_filter", all(h["domain"] == "金融销售" for h in s2["hits"]) and s2["hits"],
          {"domains": [h["domain"] for h in s2["hits"]]})
    s3 = T._search_regulations("火星移民审批流程 zzqqxx", top_k=3)
    check("search_empty_declared", s3["empty_result"] is True and s3["degraded_to_rules_only"] is True,
          {"empty": s3["empty_result"]})
    print(f"[e2e] 检索: 命中 {len(s1['hits'])}，领域过滤 {len(s2['hits'])}，空检索声明 {s3['empty_result']}")

    # ---------------- 判定类 ----------------
    a_high = T._annotate_clause("本产品保本，年化收益不低于 5%。")
    a_mid = T._annotate_clause("本协议最终解释权归甲方所有。")
    a_low = T._annotate_clause("双方应按约定时间完成交付。")
    check("annotate_levels", a_high["risk_level"] == "high" and a_mid["risk_level"] == "mid"
          and a_low["risk_level"] == "low" and not a_low["risk_hits"],
          {"high": a_high["risk_hits"][0]["rule_id"] if a_high["risk_hits"] else None,
           "mid": a_mid["risk_hits"][0]["rule_id"] if a_mid["risk_hits"] else None})
    # 【第二次审查修正】原断言在 risk_hits 为空时恒真（空循环）：先要求确有命中，
    # 再要求每条命中都带法条引用（"无引用不判风险"）。
    check("annotate_citation",
          a_high["has_citation"] and len(a_mid["risk_hits"]) >= 1
          and all(h.get("regulation_ref") for h in a_mid["risk_hits"]),
          {"hits": [h.get("rule_id") for h in a_mid["risk_hits"]]})

    doc = T._annotate_document(text="\n".join(CONTRACT_LINES))
    check("annotate_document", doc["ok"] and doc["overall_risk_level"] == "high"
          and doc["risky_clause_count"] >= 3 and doc["rule_table_digest"],
          {"overall": doc["overall_risk_level"], "risky": doc["risky_clause_count"],
           "clauses": doc["clause_count"]})
    print(f"[e2e] 整篇标注: 条款 {doc['clause_count']}，风险 {doc['risky_clause_count']}，"
          f"整体 {doc['overall_risk_level']}")

    # ---------------- 报告类 ----------------
    rep = T._generate_report(doc_id=doc["doc_id"], annotation=doc, trace_id="trace-p2-selfcheck",
                             audit_trail=["自检：无人工介入"])
    check("generate_report", rep["ok"] and any(p.endswith(".md") for p in rep["paths"])
          and "合同合规审查报告" in rep["markdown"],
          {"paths": rep["paths"], "warnings": rep["warnings"]})
    print(f"[e2e] 报告: {[Path(p).name for p in rep['paths']]} 告警={rep['warnings']}")

    # ---------------- 规则提案 ----------------
    # 自检会写规则表：先快照，结束前还原，保证自检可重复运行且不污染唯一真相源
    before_rules = T.rules_store.load_all()
    snap_file = PROJECT_ROOT / ".runtime" / "logs" / "p2-rules-before-selfcheck.json"
    snap_file.parent.mkdir(parents=True, exist_ok=True)
    snap_file.write_text(json.dumps(before_rules, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    rules_restored = {"done": False}
    SELFTEST_RUN = ""

    try:
        # 用一个**与现有规则表无关**的模式，确保走"新增"路径并能验证生效
        NEW_PATTERN = "自动续约.{0,6}(无需|不需).{0,4}通知"
        pr_new = T._propose_rule(reject_reason="该条款约定自动续约但未约定通知义务，存在续约争议与费用风险",
                                 pattern=NEW_PATTERN, severity="mid",
                                 regulation_ref="《民法典》第 470 条",
                                 suggestion="明确续约通知期限与拒绝续约方式", proposed_by="specialist-li")
        check("propose_new", pr_new["ok"] and pr_new["kind"] == "new",
              {"kind": pr_new["kind"], "id": pr_new.get("proposal_id"),
               "dedup": pr_new.get("dedup", {}).get("note")})
        # 新增规则在批准前**不得**参与判定
        pre_effective = T._annotate_clause("本合同到期后自动续约，无需另行通知乙方。")
        check("proposal_not_effective_before_approval",
              all(h["rule_id"] != pr_new["proposed_rule_id"] for h in pre_effective["risk_hits"]),
              {"hits": [h["rule_id"] for h in pre_effective["risk_hits"]]})

        new_pid = pr_new["proposal_id"]
        ap_new = T._approve_rule_proposal(new_pid, approved_by="supervisor-zhang")
        after_new = T._annotate_clause("本合同到期后自动续约，无需另行通知乙方。")
        new_active = [h for h in after_new["risk_hits"] if h["rule_id"] == ap_new.get("rule_id")]
        check("new_rule_takes_effect",
              ap_new["ok"] and ap_new.get("version") == 1 and bool(new_active)
              and new_active[0]["rule_version"] == 1 and after_new["risk_level"] == "mid",
              {"rule_id": ap_new.get("rule_id"), "version": ap_new.get("version"),
               "level": after_new["risk_level"], "hits": [h["rule_id"] for h in after_new["risk_hits"]]})
        print(f"[e2e] 新增规则生效: {ap_new.get('rule_id')} v{ap_new.get('version')} "
              f"-> {after_new['risk_level']}（批准前命中 {[h['rule_id'] for h in pre_effective['risk_hits']]}）")

        pr_exact = T._propose_rule(reject_reason="同模式再次出现（补充了通知义务缺失的证据）",
                                   pattern=pr_new["pattern"], severity="mid",
                                   regulation_ref="《民法典》第 470 条",
                                   suggestion="明确续约通知期限与拒绝续约方式",
                                   proposed_by="specialist-li")
        check("propose_exact_dedup", pr_exact["ok"] and pr_exact["kind"] == "revise"
              and pr_exact["target_rule_id"] == pr_new["proposed_rule_id"]
              and pr_exact["dedup"]["layer"] == "exact",
              {"kind": pr_exact["kind"], "target": pr_exact["target_rule_id"],
               "layer": pr_exact["dedup"]["layer"]})

        # 包含关系去重（只读检测，不改动既有规则）
        pr_similar = T._propose_rule(reject_reason="表述略有不同的同义风险",
                                     pattern="最终解释权归.{0,4}方所有", severity="mid",
                                     proposed_by="specialist-li")
        check("propose_containment_dedup", pr_similar["ok"] and pr_similar["kind"] == "revise"
              and pr_similar["dedup"]["containment_rule"] == "RR-003",
              {"kind": pr_similar["kind"], "layer": pr_similar["dedup"]["layer"],
               "containment": pr_similar["dedup"]["containment_rule"],
               "similarity": pr_similar["dedup"]["similarity"]})

        # 冲突检测（只读）：同模式异等级必须转为"待主管裁决"，不得自动修订
        pr_conflict = T._propose_rule(reject_reason="等级判断分歧：认为应为高风险",
                                      pattern="最终解释权", severity="high",
                                      proposed_by="specialist-li")
        check("propose_conflict_detected",
              pr_conflict["ok"] and pr_conflict["kind"] == "conflict"
              and pr_conflict["conflict_requires_manual"] and pr_conflict["conflict_with"]
              and pr_conflict["conflict_with"][0]["rule_id"] == "RR-003",
              {"kind": pr_conflict["kind"], "conflicts": pr_conflict["conflict_with"]})
        # 冲突提案不得被直接批准写入
        conflict_approve = T._approve_rule_proposal(pr_conflict["proposal_id"],
                                                    approved_by="supervisor-zhang")
        check("conflict_proposal_not_writable", conflict_approve["ok"] is False,
              {"errors": conflict_approve.get("errors")})
        print(f"[e2e] 提案：精确去重 target={pr_exact['target_rule_id']}，"
              f"包含去重 {pr_similar['dedup']['containment_rule']}"
              f"(层={pr_similar['dedup']['layer']}, difflib={pr_similar['dedup']['similarity']})，"
              f"冲突 kind={pr_conflict['kind']} -> 拒绝写入={conflict_approve['ok'] is False}")

        # 修订提案 -> 版本化写入
        ap = T._approve_rule_proposal(pr_exact["proposal_id"], approved_by="supervisor-zhang")
        d = T.rules_store.diff_since(before_rules)
        check("approve_writes_versioned", ap["ok"] and ap["version"] >= 2
              and bool(d["added"] or d["changed"]),
              {"rule_id": ap.get("rule_id"), "version": ap.get("version"),
               "diff": {"added": d["added"], "changed": d["changed"]}})
        ap_again = T._approve_rule_proposal(pr_exact["proposal_id"], approved_by="supervisor-zhang")
        check("approve_idempotent_guard", ap_again["ok"] is False and ap_again.get("errors"),
              {"errors": ap_again.get("errors")})
        print(f"[e2e] 修订写入: {ap.get('rule_id')} v{ap.get('version')}；重复批准被拒={ap_again['ok'] is False}")

        # ---------------- 待办 / 审计 / 指标 ----------------
        # 用唯一 run_id，避免与历史自检数据互相干扰；结束后清理，保证可重复运行
        SELFTEST_RUN = f"wf_run_selftest_{int(time.time())}"
        todo = T.queue_store.create_todo(run_id=SELFTEST_RUN, node_name="review_risk",
                                         role_required="specialist", title="待处理：自检合同",
                                         summary="高/中风险需专员确认", payload={"case": "selfcheck"})
        todo2 = T.queue_store.create_todo(run_id=SELFTEST_RUN, node_name="review_risk",
                                          role_required="specialist", title="待处理：自检合同",
                                          summary="重复调用", payload={"case": "selfcheck"})
        check("todo_idempotent", todo["ok"] and todo["created"] is True and todo2["created"] is False,
              {"first": todo["created"], "second": todo2["created"]})
        tid = todo["todo"]["todo_id"]
        c1 = T.queue_store.claim_todo(tid, actor="specialist-li")
        c2 = T.queue_store.claim_todo(tid, actor="specialist-wang")
        check("todo_atomic_claim", c1["ok"] and c1.get("claimed") is True and c2["ok"] is False
              and c2.get("holder") == "specialist-li",
              {"first": c1.get("claimed"), "second_holder": c2.get("holder")})
        print(f"[e2e] 待办: 幂等创建={todo['created']}，抢占 first={c1.get('claimed')} "
              f"second_holder={c2.get('holder')}")

        dec = T._record_decision(run_id=SELFTEST_RUN, node_name="review_risk", decision="modify",
                                 actor="specialist-li", role="specialist", note="要求补充收益不确定性提示",
                                 todo_id=tid, trace_id="trace-p2-selfcheck")
        ld = T._list_pending(status="done", run_id=SELFTEST_RUN)
        audit = T._list_audit(run_id=SELFTEST_RUN)
        check("decision_and_audit",
              dec["ok"] and (dec["todo_result"] or {}).get("decision") == "modify"
              and ld["count"] == 1 and len(audit["events"]) >= 3,
              {"todo_done": ld["count"], "audit_events": len(audit["events"]),
               "todo_result": dec.get("todo_result")})
        m = T._metrics()
        check("metrics_ok", m["ok"] and m["todo_total"] >= 1 and m["rule_table_digest"]
              and m["regulation_count"] >= 40,
              {"todo_total": m["todo_total"], "by_decision": m["by_decision"],
               "regulations": m["regulation_count"]})
        print(f"[e2e] 决策+审计: 审计事件 {len(audit['events'])} 条；指标 {m['by_decision']}")

        # 规则表还原与自检数据清理见下方 finally（保证异常路径也会执行）
    finally:
        # 【审查修正 P0】还原与清理必须在 finally 中执行：原来放在函数末尾，
        # restore_rules() 之前的任何异常/中断（例如提案返回 ok=False）
        # 都会把自检写入的 active 规则**永久留在唯一真相源里**，
        # 并让下次运行 propose_new 退化为 revise，自检从此永久红。
        try:
            rr = T.rules_store.restore(before_rules)
            rules_restored.update({"done": True, "digest": rr.get("new_digest"),
                                   "count": rr.get("count")})
        except Exception as exc:
            rules_restored["error"] = f"{type(exc).__name__}: {exc}"
        try:
            cleaned = _cleanup_selftest(SELFTEST_RUN)
        except Exception as exc:
            cleaned = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            after_restore = T.rules_store.load_all()
            ev["rules_restored"] = {
                "count_before": len(before_rules), "count_after": len(after_restore),
                "digest_before": T.rules_store._digest_of(before_rules),
                "digest_after": T.rules_store._digest_of(after_restore),
                "selftest_cleanup": cleaned,
            }
            ev["rules_restored"]["identical"] = (ev["rules_restored"]["digest_before"]
                                                 == ev["rules_restored"]["digest_after"])
            if not ev["rules_restored"]["identical"] and "rules_restore_mismatch" not in problems:
                problems.append("rules_restore_mismatch")
                ev["verdict"] = "FAIL"
            print(f"[e2e] 规则表已还原: {ev['rules_restored']['count_after']} 条，"
                  f"指纹一致={ev['rules_restored']['identical']}")
        except Exception as exc:
            problems.append("rules_restore_failed")
            ev["rules_restore_error"] = f"{type(exc).__name__}: {exc}"
            print(f"[e2e] 还原检查异常: {ev['rules_restore_error']}")
        ev["rules_restored_meta"] = rules_restored

def _finalize(ev: dict, checks: dict, problems: list) -> None:
    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    ev: dict = {"check": "P2-tools-e2e", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    checks: dict[str, dict] = {}
    problems: list[str] = []
    rc = 1

    def check(name: str, ok: bool, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    try:
        _body(ev, checks, problems, check)
        rc = 0
    except Exception as exc:
        # 【第二次审查修正】原来主体抛异常时，try/finally 之后的"落证据"代码
        # **根本不会执行**（异常继续向外传播）——证据文件停留在上一次运行的内容，
        # 看上去还是上一次的 PASS（假绿）。现在异常被捕获、留痕，并且必定落盘。
        import traceback
        ev["exception"] = f"{type(exc).__name__}: {exc}"
        ev["traceback_tail"] = traceback.format_exc()[-2000:]
        if "unexpected_exception" not in problems:
            problems.append("unexpected_exception")
        print(f"[e2e] 未预期异常：{ev['exception']}")

    _finalize(ev, checks, problems)

    print(f"\n[e2e] 自检 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    if problems:
        for p in problems:
            print(f"   - 失败: {p}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"[e2e] 证据: {EVIDENCE}")
    return 0 if rc == 0 and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
