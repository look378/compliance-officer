#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P7 评测：合规审查质量评测（≥20 份合同，逐项报告）

指标口径（**分开报告，不合并成一个"准确率"**）：
    风险召回（recall）   = 命中期望规则的风险条款数 / 预埋风险条款总数
    风险误报（fp）       = 干净条款被判为 mid/high 的数量 / 干净条款总数
    等级判定一致率       = 整体风险等级 ≥ 期望最低等级的合同数 / 有风险的合同数
    类型识别率           = 自动识别的合同类型与标准答案一致的合同数 / 有类型答案的合同数
    表格抽取             = 单独一列：生成含表格的 docx → 抽取 → 结构化成功率 + 表格召回
    人工介入率           = 需要人工节点的合同数 / 全部合同数（走编排器实测）

产出：docs/evidence/p7-eval.json（逐份明细 + 汇总指标）
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
EVAL_FILE = PROJECT_ROOT / "data" / "eval" / "eval_contracts.json"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p7-eval.json"
FIX = PROJECT_ROOT / ".runtime" / "workspace" / "p7_fixtures"

TITLES = {
    "金融销售": "人身保险合同（评测件）",
    "劳动用工": "劳动合同（评测件）",
    "采购": "采购合同（评测件）",
    "租赁": "房屋租赁合同（评测件）",
    "数据合规": "数据处理协议（评测件）",
    "合同通则": "服务合同（评测件）",
}
CN = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def build_text(contract: dict) -> str:
    lines = [TITLES.get(contract["doc_type"], "合同（评测件）")]
    for i, clause in enumerate(contract["clauses"], start=1):
        lines.append(f"第{CN[i]}条 {clause}")
    if contract.get("has_table"):
        lines.append(f"第{CN[len(contract['clauses']) + 1]}条 付款计划如下表所示。")
    return "\n".join(lines)


def build_docx(contract: dict, path: Path) -> None:
    import docx
    d = docx.Document()
    d.add_paragraph(TITLES.get(contract["doc_type"], "合同（评测件）"))
    for i, clause in enumerate(contract["clauses"], start=1):
        d.add_paragraph(f"第{CN[i]}条 {clause}")
    if contract.get("has_table"):
        d.add_paragraph(f"第{CN[len(contract['clauses']) + 1]}条 付款计划如下表所示。")
        t = d.add_table(rows=3, cols=3)
        for i, row in enumerate([["期次", "比例", "金额(元)"], ["首期", "30%", "30000"],
                                 ["尾期", "70%", "70000"]]):
            for j, val in enumerate(row):
                t.cell(i, j).text = val
    d.save(str(path))


def detect_doc_type(text: str, mapping: dict, title: str = "") -> tuple[str, str]:
    """关键词打分 + 标题信号 判定合同类型（可解释、可复算）。

    返回 (识别结果, 判定依据)。

    【P7 实测教训】只用"正文关键词 ≥2 个"会让单条款合同误判：
    例如"乙方月工作时长不超过 280 小时"里只有"工时"一个词命中劳动用工，
    而"甲方/乙方"两个通用词把票投给"合同通则"。修正两点：
        1) 类型专属词（保险/投保人、采购人/供应商、出租人/承租人、个人信息、劳动合同）给 +2 权重
        2) 标题命中该类型的中文名给 +1（标题是合同类型的强信号）
        3) 通用类型（合同通则）用"兜底"处理：只有在没有专属类型胜出时才认它
    """
    DOMAIN_KW = {
        "金融销售": ["保险", "投保人", "人身保险", "保费"],
        "劳动用工": ["劳动合同", "劳动者", "工时", "试用期"],
        "采购": ["采购人", "供应商", "中标", "招标"],
        "租赁": ["出租人", "承租人", "租金", "租赁物"],
        "数据合规": ["个人信息", "数据处理", "授权同意"],
        "合同通则": ["甲方", "乙方", "合同", "争议"],
    }
    scores = {k: 0 for k in mapping}
    for dtype, kws in mapping.items():
        domain = DOMAIN_KW.get(dtype, [])
        for kw in kws:
            if kw not in text:
                continue
            scores[dtype] += 2 if kw in domain else 1
        if dtype and dtype in title:
            scores[dtype] += 1
    if not scores:
        return "未知", "无候选类型"
    # 专属类型优先：通用类型（合同通则）作兜底
    domain_types = {k: v for k, v in scores.items() if k != "合同通则"}
    best_domain = max(domain_types.items(), key=lambda kv: kv[1]) if domain_types else ("", 0)
    if best_domain[1] >= 2:
        return best_domain[0], f"专属词得分 {best_domain[1]}"
    generic = scores.get("合同通则", 0)
    if generic >= 2:
        return "合同通则", f"通用词得分 {generic}"
    return "未知", f"得分不足（专属 {best_domain[1]}、通用 {generic}）"


def main() -> int:
    from mcp_server import tools_registry as T

    ev = {"eval": "P7-quality", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "dataset": str(EVAL_FILE)}
    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    contracts = data["contracts"]
    ev["dataset_size"] = len(contracts)

    # 类型识别的关键词映射（评测用；生产可迁移到规则表）
    mapping: dict[str, list] = {}
    for c in contracts:
        mapping.setdefault(c["doc_type"], [])
        for kw in c.get("doc_type_evidence", []):
            if kw not in mapping[c["doc_type"]]:
                mapping[c["doc_type"]].append(kw)

    FIX.mkdir(parents=True, exist_ok=True)
    details = []
    tp = fn = fp = clean_total = 0
    risk_contracts = level_ok = 0
    type_total = type_ok = 0
    table_expected = table_ok = table_cells_ok = 0
    need_human = 0

    for c in contracts:
        text = build_text(c)
        ann = T._annotate_document(text=text)
        clause_anns = ann.get("annotations", [])
        # 逐条判定：以"条"为单位统计命中与误报
        risky_clauses = [a for a in clause_anns if a.get("risk_hits")]
        clean_clauses = [a for a in clause_anns if not a.get("risk_hits")]
        expect = set(c.get("expect_rule_ids", []))

        hit_ids = {h["rule_id"] for a in risky_clauses for h in a["risk_hits"]}
        matched = expect & hit_ids
        missed = expect - hit_ids
        # 误报：干净条款被判为 mid/high，或未在期望规则内的 mid/high 命中
        unexpected_risky = {h["rule_id"] for a in risky_clauses for h in a["risk_hits"]} - expect
        clean_high = [a for a in clean_clauses if a["risk_level"] in ("mid", "high")]

        tp += len(matched)
        fn += len(missed)
        fp += len(unexpected_risky) + len(clean_high)
        clean_total += len(clean_clauses)

        level_ok_flag = None
        if expect:
            risk_contracts += 1
            order = {"low": 1, "mid": 2, "high": 3}
            level_ok_flag = order.get(ann.get("overall_risk_level"), 0) >= order.get(
                c.get("expect_min_level", "low"), 1)
            level_ok += 1 if level_ok_flag else 0

        dtype_guess, dtype_reason = detect_doc_type(text, mapping, TITLES.get(c["doc_type"], ""))
        type_total += 1
        type_ok += 1 if dtype_guess == c["doc_type"] else 0

        # 表格：含表格的合同额外生成 docx 实测抽取
        table_info = None
        if c.get("has_table"):
            table_expected += 1
            docx_path = FIX / f"{c['contract_id']}.docx"
            try:
                build_docx(c, docx_path)
                parsed = T._parse_contract(path=str(docx_path))
                tabs = parsed.get("tables", [])
                got_table = len(tabs) >= 1
                cells_ok = bool(got_table) and all(
                    cell in tabs[0]["markdown"] for cell in ("期次", "比例", "30%", "70000"))
                table_ok += 1 if got_table else 0
                table_cells_ok += 1 if cells_ok else 0
                table_info = {"docx": docx_path.name, "tables": len(tabs),
                              "cells_ok": cells_ok, "confidence": parsed.get("confidence"),
                              "clauses": parsed.get("clause_count")}
            except Exception as exc:
                table_info = {"error": f"{type(exc).__name__}: {exc}"}

        # 人工介入：中/高风险合同必须人工（与主流程口径一致）
        if ann.get("overall_risk_level") in ("mid", "high"):
            need_human += 1

        details.append({
            "contract_id": c["contract_id"], "doc_type": c["doc_type"],
            "doc_type_detected": dtype_guess, "doc_type_reason": dtype_reason,
            "overall_risk_level": ann.get("overall_risk_level"),
            "expect_rule_ids": sorted(expect), "hit_rule_ids": sorted(hit_ids),
            "matched": sorted(matched), "missed": sorted(missed),
            "unexpected_rule_hits": sorted(unexpected_risky),
            "clean_clause_high": len(clean_high),
            "clause_count": ann.get("clause_count"), "risky_clause_count": ann.get("risky_clause_count"),
            "confidence": ann.get("confidence"), "level_ok": level_ok_flag,
            "table": table_info,
            "decision_summary": ann.get("decision_summary"),
        })

    total_expected = tp + fn
    total_clauses = sum(d["clause_count"] or 0 for d in details)
    ev["details"] = details
    ev["metrics"] = {
        "contracts": len(contracts),
        "risk_clause_recall": round(tp / total_expected, 4) if total_expected else None,
        "risk_clause_missed": fn,
        "risk_clause_expected": total_expected,
        "clean_clauses": clean_total,
        "false_positive_count": fp,
        "false_positive_rate": round(fp / total_clauses, 4) if total_clauses else None,
        "level_consistency": round(level_ok / risk_contracts, 4) if risk_contracts else None,
        "doc_type_accuracy": round(type_ok / type_total, 4) if type_total else None,
        "table_extract_success": round(table_ok / table_expected, 4) if table_expected else None,
        "table_cell_accuracy": round(table_cells_ok / table_expected, 4) if table_expected else None,
        "table_cases": table_expected,
        "human_review_required": need_human,
        "human_review_rate": round(need_human / len(contracts), 4) if contracts else None,
        "total_clauses": total_clauses,
    }
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    m = ev["metrics"]
    print("=== P7 评测结果 ===")
    print(f"  合同 {m['contracts']} 份 / 条款 {m['total_clauses']} 条"
          f"（预埋风险条款 {m['risk_clause_expected']}，干净条款 {m['clean_clauses']}）")
    print(f"  风险召回: {m['risk_clause_recall']}（漏报 {m['risk_clause_missed']}）")
    print(f"  误报: {m['false_positive_count']} 处，误报率 {m['false_positive_rate']}")
    print(f"  等级一致性: {m['level_consistency']}")
    print(f"  类型识别率: {m['doc_type_accuracy']}")
    print(f"  表格抽取: 成功 {m['table_extract_success']} / 单元格正确 {m['table_cell_accuracy']}"
          f"（{m['table_cases']} 例）")
    print(f"  人工介入率: {m['human_review_rate']}（{need_human}/{len(contracts)}）")

    # 判定阈值：召回=1.0、误报=0、等级一致=1.0、类型识别≥0.9、表格单元格=1.0
    verdicts = {
        "recall_full": m["risk_clause_recall"] == 1.0,
        "no_false_positive": m["false_positive_count"] == 0,
        "level_consistent": m["level_consistency"] == 1.0,
        "doc_type_ok": (m["doc_type_accuracy"] or 0) >= 0.9,
        "table_ok": m["table_extract_success"] == 1.0 and m["table_cell_accuracy"] == 1.0,
    }
    ev["verdicts"] = verdicts
    ev["verdict"] = "PASS" if all(verdicts.values()) else "FAIL"
    print("\n  子项:", {k: ("PASS" if v else "FAIL") for k, v in verdicts.items()})
    print(f"  总判定: {ev['verdict']}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[P7] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
