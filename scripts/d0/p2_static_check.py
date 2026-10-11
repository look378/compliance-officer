#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P2 自检 A：规则表与法规库的静态质量检查

为什么需要它：规则表是唯一真相源，一旦出现坏正则 / 缺引用 / 编号重复，
整条标注链路会静默降级或误判。把它做成可复跑的自检，而不是靠人眼。

检查项：
    规则表：YAML 可解析 / 必填字段 / 正则可编译 / 编号唯一 / active 必须有法条引用 /
            严重度与状态取值合法 / version 为正整数 / 编号命名规范
    法规库：条数 / 编号唯一 / 必填字段 / 领域分布 / 法条引用可被规则命中的覆盖度
产出：docs/evidence/p2-static-check.json
"""

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p2-static-check.json"

REQUIRED_RULE_FIELDS = ["id", "version", "status", "pattern", "severity"]
REQUIRED_REG_FIELDS = ["regulation_ref", "source", "domain", "text"]


def main() -> int:
    from mcp_server import rules_store, regulations_store

    ev = {"check": "P2-static", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    problems: list[str] = []

    # ---------------- 规则表 ----------------
    rules = rules_store.load_all()
    ev["rules"] = {"count": len(rules), "active": len(rules_store.active()),
                   "digest": rules_store.digest(),
                   "by_status": dict(Counter(r.get("status") for r in rules)),
                   "by_severity": dict(Counter(r.get("severity") for r in rules))}
    ids = [r.get("id") for r in rules]
    if len(ids) != len(set(ids)):
        problems.append(f"规则编号重复：{ [k for k, v in Counter(ids).items() if v > 1] }")
    for r in rules:
        rid = r.get("id")
        for f in REQUIRED_RULE_FIELDS:
            if r.get(f) in (None, ""):
                problems.append(f"{rid} 缺必填字段 {f}")
        if r.get("status") not in {"draft", "active", "deprecated"}:
            problems.append(f"{rid} status 非法：{r.get('status')}")
        if r.get("severity") not in {"low", "mid", "high"}:
            problems.append(f"{rid} severity 非法：{r.get('severity')}")
        if not isinstance(r.get("version"), int) or r.get("version", 0) < 1:
            problems.append(f"{rid} version 非法：{r.get('version')}")
        try:
            re.compile(r.get("pattern", ""))
        except re.error as exc:
            problems.append(f"{rid} 正则无法编译：{exc}")
        if r.get("status") == "active" and not (r.get("regulation_ref") or "").strip():
            problems.append(f"{rid} 为 active 但没有法条引用（违反『无引用不判风险』）")
        if not re.match(r"^RR-\d{3}$", str(rid)):
            problems.append(f"{rid} 编号不符合 RR-NNN 规范")

    # ---------------- 法规库 ----------------
    lib = regulations_store.load(force=True)
    items = lib["items"]
    refs = [i["regulation_ref"] for i in items]
    ev["regulations"] = {"count": len(items), "source": lib["source"], "digest": lib["digest"],
                         "by_domain": dict(Counter(i["domain"] for i in items)),
                         "domains": sorted({i["domain"] for i in items})}
    if len(refs) != len(set(refs)):
        problems.append(f"法规编号重复：{ [k for k, v in Counter(refs).items() if v > 1] }")
    for i in items:
        for f in REQUIRED_REG_FIELDS:
            if not (i.get(f) or "").strip():
                problems.append(f"法规 {i.get('regulation_ref')} 缺字段 {f}")

    # ---------------- 交叉覆盖 ----------------
    # 规则引用的法条，是否能在法规库中按编号找到（找不到就是"引用悬空"）
    dangling = []
    for r in rules_store.active():
        cited = (r.get("regulation_ref") or "").strip()
        if not cited:
            continue
        key = re.sub(r"（.*?）|\(.*?\)", "", cited).strip()
        found = any(key and key in ref for ref in refs)
        if not found:
            dangling.append({"rule_id": r["id"], "cited": cited})
    ev["citation_coverage"] = {"active_rules_with_citation": len(
        [r for r in rules_store.active() if (r.get("regulation_ref") or "").strip()]),
        "dangling": dangling}
    if dangling:
        problems.append(f"有 {len(dangling)} 条规则的引用无法在法规库定位（悬空引用）")

    # ---------------- 检索冒烟 ----------------
    probes = {
        "工时上限": "月工作时长 260 小时",
        "保本收益": "保证收益 保本 无风险",
        "最终解释权": "最终解释权归甲方",
        "数据同意": "收集个人信息无需同意",
        "违约金过高": "违约金为合同总额的百分之五十",
    }
    smoke = {}
    for name, q in probes.items():
        res = regulations_store.search(q, top_k=3)
        smoke[name] = {"query": q, "hits": [h["regulation_ref"] for h in res["hits"]],
                       "empty": res["empty_result"]}
        if res["empty_result"]:
            problems.append(f"检索冒烟失败（空结果）：{name} <- {q}")
    ev["search_smoke"] = smoke

    ev["problems"] = problems
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    print(f"[静态自检] 规则 {ev['rules']['count']} 条（active {ev['rules']['active']}）"
          f"  法规 {ev['regulations']['count']} 条  来源 {ev['regulations']['source']}")
    print(f"[静态自检] 领域分布: {ev['regulations']['by_domain']}")
    for name, s in smoke.items():
        print(f"   检索[{name}] -> {s['hits'] or '空'}")
    if problems:
        print(f"\n[静态自检] 发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"   - {p}")
    print(f"[静态自检] 判定: {ev['verdict']}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[静态自检] 证据: {EVIDENCE}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
