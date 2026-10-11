#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
G7 门禁：标注质量基线（预埋风险命中率 / 误报率）

目的：在写业务代码之前，先建立**可复现的质量基线**，避免"看起来能跑"却无法量化。
评测对象是 D0 阶段已经上线的确定性标注链路（规则表匹配 + 无引用不判风险），
不含 LLM 泛化——LLM 泛化通道在 P2 接入后会另设评测，且必须与本基线分开报告。

指标定义：
    hit_rate   = 命中期望规则的用例数 / 有预埋风险的用例数
    false_pos  = 被标为 mid/high 但期望为 low 的用例数 / 期望为 low 的用例数
    citation_ok= 所有 mid/high 标注均带法条引用（"无引用不判风险"）

产出：docs/evidence/d0-g7-eval-baseline.json
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES = PROJECT_ROOT / "data" / "eval" / "d0_eval_cases.json"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "d0-g7-eval-baseline.json"


def main() -> int:
    ev = {"gate": "G7", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "cases_file": str(CASES)}
    sys.path.insert(0, str(PROJECT_ROOT))
    # 走"工具契约层"而不是内部实现模块：这才是对外的稳定接口，
    # 内部重构（如 P2 把规则读取下沉到 rules_store）不应让门禁失效
    from mcp_server import tools_registry as T

    info = T._service_info()
    ev["rule_table"] = {"active_rules": info["rule_table"]["active"],
                        "total_rules": info["rule_table"]["count"],
                        "digest": info["rule_table"]["digest"],
                        "regulation_count": info["regulations"]["count"]}

    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    ev["case_count"] = len(cases)
    results, hits, expected_risky, false_pos, expected_clean = [], 0, 0, 0, 0

    for c in cases:
        out = T._annotate_clause(c["clause"], c["case_id"])
        got_ids = [h["rule_id"] for h in out["risk_hits"]]
        is_risky = out["risk_level"] in ("mid", "high")
        citations = [h.get("regulation_ref") for h in out["risk_hits"]]
        expected = c["expect_rule_ids"]
        matched = set(expected).issubset(set(got_ids)) if expected else not is_risky
        if expected:
            expected_risky += 1
            hits += 1 if matched else 0
        else:
            expected_clean += 1
            false_pos += 1 if is_risky else 0
        item = {
            "case_id": c["case_id"], "domain": c["domain"],
            "expect_rule_ids": expected, "got_rule_ids": got_ids,
            "expect_level": c["expect_level"], "got_level": out["risk_level"],
            "rule_table_digest": out.get("rule_table_digest"),
            "citations": citations, "matched": matched,
            "citation_ok": all(bool(x) for x in citations) if citations else True,
        }
        results.append(item)
        flag = "OK " if matched else "MISS"
        print(f"   [{flag}] {c['case_id']} 期望 {expected or 'low'} -> 实际 {got_ids or 'low'}"
              f"({out['risk_level']})")

    hit_rate = round(hits / expected_risky, 4) if expected_risky else None
    fp_rate = round(false_pos / expected_clean, 4) if expected_clean else None
    citation_ok = all(r["citation_ok"] for r in results)
    # 【审查修正 P2】数据集里的 expect_level 原来只被采集进证据、从不参与判定，
    # 于是"把 high 判成 mid"这类等级回归不会被 G7 发现（命中集合仍相同）。
    # 现在按严重度序关系判定：实际等级不得低于期望等级。
    LEVEL_ORDER = {"low": 1, "mid": 2, "high": 3}
    level_mismatches = [
        {"case_id": r["case_id"], "expect_level": r["expect_level"], "got_level": r["got_level"]}
        for r in results
        if LEVEL_ORDER.get(r["got_level"], 0) < LEVEL_ORDER.get(r["expect_level"], 0)
    ]
    level_ok = not level_mismatches

    ev["results"] = results
    ev["metrics"] = {
        "risky_cases": expected_risky, "clean_cases": expected_clean,
        "hits": hits, "hit_rate": hit_rate,
        "false_positive_count": false_pos, "false_positive_rate": fp_rate,
        "citation_ok": citation_ok,
        "level_consistency": round(1 - len(level_mismatches) / len(results), 4) if results else None,
        "level_mismatches": level_mismatches,
    }
    print(f"\n[G7] 命中率 = {hits}/{expected_risky} = {hit_rate}")
    print(f"[G7] 误报率 = {false_pos}/{expected_clean} = {fp_rate}")
    print(f"[G7] 等级一致性 = {ev['metrics']['level_consistency']}"
          f"（不一致 {len(level_mismatches)} 例）")
    print(f"[G7] 引用完整性（无引用不判风险）= {citation_ok}")

    verdicts = {
        "hit_rate_ok": hit_rate is not None and hit_rate == 1.0,
        "false_positive_zero": false_pos == 0,
        "citation_ok": citation_ok,
        "level_consistent": level_ok,
        "rule_table_stable": bool(info["rule_table"]["digest"]),
    }
    ev["verdicts"] = verdicts
    ev["verdict"] = "PASS" if all(verdicts.values()) else "FAIL"
    ev["note"] = ("D0 基线只覆盖确定性规则通道；LLM 泛化通道与表格抽取在 P2/P7 另设评测，"
                  "两类指标必须分开报告，不得混合成一个'准确率'。")
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[G7] 总判定: {ev['verdict']}")
    print(f"[G7] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
