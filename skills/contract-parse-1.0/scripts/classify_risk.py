#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D0 门禁用最小确定性岗位脚本（classify_risk）

用途：验证「技能包 → 依赖隔离安装 → run_skill_script 调用 → 结构化输出」这条确定性通道可用。
      这不是最终的岗位能力实现；P2 起由 mcp_server 承载完整能力（解析/检索/标注/报告/提案）。

契约：
    入参 --text "<合同条款文本>"   或   --file <文本文件路径>
    出参 单行紧凑 JSON（体积小，避免被上层工具输出截断）

设计约束：
    - 只做规则表匹配（不做语义判断），保证可复现、可单测；
    - 严格遵循「无引用不判风险」：命中规则必须带 rule_id、severity **与 regulation_ref**；
    - 【审查修正 P1】规则来源统一为共享规则表 `rules/risk_rules.yaml`（唯一真相源）。
      原实现内嵌了一份 6 条的最小规则表，且**编号语义与共享表冲突**
      （技能 RR-003=月工时 vs 共享表 RR-003=最终解释权；技能 RR-005=最终解释权 vs 共享表 RR-005=月工时），
      同一编号在两处指向不同规则；hits 里也没有 regulation_ref，
      于是"无引用不判风险"这条红线在技能通道上被静默绕过。
      现在：优先读共享规则表（只取 status=active）；读不到（技能包被单独拷走）时，
      退化为内嵌表，但内嵌表**必须与共享表同源**（编号/等级/引用一致），并在输出里标注来源。
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# 内嵌兜底表：与 rules/risk_rules.yaml 的 active 条目保持一致（编号、等级、引用）
FALLBACK_RULES = [
    {"id": "RR-001", "pattern": r"保本.{0,6}(收益|保本)|保本", "severity": "high",
     "regulation_ref": "《人身保险销售行为管理办法》第 22 条"},
    {"id": "RR-002", "pattern": r"(保证|承诺)(固定|年化|保底)?.{0,4}收益", "severity": "high",
     "regulation_ref": "《人身保险销售行为管理办法》第 22 条"},
    {"id": "RR-003", "pattern": r"最终解释权", "severity": "mid",
     "regulation_ref": "《民法典》第 498 条"},
    {"id": "RR-004", "pattern": r"无条件.{0,4}(解除|终止)", "severity": "high",
     "regulation_ref": "《民法典》第 496 条（格式条款提示说明义务）"},
    {"id": "RR-005", "pattern": r"月.{0,4}工作时长.{0,8}(不超过|不得超过|达|达到)?.{0,4}(2[6-9]\d|3\d\d)\s*(小时|h)",
     "severity": "high", "regulation_ref": "《劳动法》第 41 条"},
    {"id": "RR-006", "pattern": r"违约金.{0,10}(3\d|4\d|5\d)\s*%", "severity": "mid",
     "regulation_ref": "《民法典》第 585 条（违约金调整）"},
    {"id": "RR-007", "pattern": r"自动续约|自动顺延", "severity": "low",
     "regulation_ref": "《民法典》第 470 条（合同内容）"},
    {"id": "RR-008", "pattern": r"(个人信息|用户数据).{0,10}(无需|不需|不必).{0,6}(同意|授权)", "severity": "high",
     "regulation_ref": "《个人信息保护法》第 13 条"},
]

SEVERITY_ORDER = {"low": 1, "mid": 2, "high": 3}


def _shared_rules_path() -> Path:
    """定位共享规则表（技能包位于 <project>/skills/<name>/scripts/ 下）。"""
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "rules" / "risk_rules.yaml"
        if cand.exists():
            return cand
    env = (os.environ.get("COMPLIANCE_RULES_PATH") or "").strip()
    return Path(env) if env else Path("")


def load_rules() -> tuple[list[dict], str]:
    """返回 (规则列表, 来源标识)。优先共享规则表，其次内嵌兜底表。"""
    path = _shared_rules_path()
    if path and path.exists():
        try:
            import yaml
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            rules = [{"id": r["id"], "pattern": r["pattern"], "severity": r.get("severity", "mid"),
                      "regulation_ref": r.get("regulation_ref", "")}
                     for r in raw
                     if isinstance(r, dict) and r.get("id") and r.get("pattern")
                     and r.get("status") == "active"]
            if rules:
                return rules, f"rules/risk_rules.yaml（{len(rules)} 条 active）"
        except Exception:
            pass
    return FALLBACK_RULES, f"内嵌兜底表（{len(FALLBACK_RULES)} 条；未找到共享规则表）"


def classify(text: str) -> dict:
    rules, source = load_rules()
    hits = []
    for rule in rules:
        try:
            m = re.search(rule["pattern"], text)
        except re.error:
            continue                      # 坏正则不得让整条通道失败
        if not m:
            continue
        hits.append({"rule_id": rule["id"], "severity": rule["severity"],
                     "matched": m.group(0),
                     # 「无引用不判风险」：命中必须带可核验引用
                     "regulation_ref": rule.get("regulation_ref") or ""})
    # 引用缺失的命中不作为风险（只记录为 unverifiable，避免无依据的风险结论）
    verified = [h for h in hits if h["regulation_ref"]]
    unverifiable = [h for h in hits if not h["regulation_ref"]]
    if verified:
        level = max(verified, key=lambda h: SEVERITY_ORDER.get(h["severity"], 0))["severity"]
    else:
        level = "low"
    confidence = round(len(verified) / len(rules), 3) if rules else 0.0
    return {
        "risk_level": level,
        "confidence": confidence,
        "hit_count": len(verified),
        "hits": verified,
        "unverifiable_hits": unverifiable,
        "rule_source": source,
        # 证据链最小字段：本次判定的输入指纹，供人工复核时对齐原文
        "evidence_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "engine": "classify_risk/v2",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="合同条款风险规则匹配（D0 门禁脚本）")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", type=str, help="待判定文本")
    src.add_argument("--file", type=str, help="待判定文本文件路径（UTF-8）")
    parser.add_argument("--pretty", action="store_true", help="输出缩进 JSON（调试用）")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = args.text

    result = classify(text)
    # 默认单行紧凑输出：上层工具对长输出有截断，紧凑格式避免丢失字段
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
