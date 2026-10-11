#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
运行状态概览工具（本项目自有）

用途：一眼看清当前有多少在办 run、待办分布、最近的审计事件，便于演示与排障。
用法：
    python scripts/tools/status_overview.py
    python scripts/tools/status_overview.py --run wf_run_xxx     # 看单个 run 的完整链路
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_boot = PROJECT_ROOT / "scripts" / "d0" / "_bootstrap.py"
_spec = importlib.util.spec_from_file_location("_d0_bootstrap", str(_boot))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_mod.bootstrap()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="合规官运行状态概览")
    parser.add_argument("--run", default="", help="查看指定 run 的完整链路")
    args = parser.parse_args()

    from service.review_orchestrator import ReviewOrchestrator
    orch = ReviewOrchestrator()

    if args.run:
        st = orch.status(args.run)
        if not st.get("ok"):
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return 1
        print(f"=== run {st['run_id']} ===")
        print(f"  状态: {st['status']}  风险: {st['risk_level']}  文档: {st['doc_id']}")
        print(f"  复核: 专员={st['specialist_note']} 主管={st['supervisor_note']}")
        print(f"  命中规则: {', '.join(st['hit_rule_ids']) or '无'}")
        print(f"  占用: {st['claim']}")
        print(f"  待办 {len(st['todos'])} 条:")
        for t in st["todos"]:
            print(f"    - [{t['role_required']}/{t['status']}] {t['title'][:40]} "
                  f"决策={t['decision'] or '-'} 处理人={t['decided_by'] or '-'}")
        print(f"  审计 {len(st['audit'])} 条:")
        for e in st["audit"]:
            print(f"    - {e['created_at']} {e['event']:22s} {e['actor'] or '-'}")
        return 0

    m = orch.metrics()
    print("=== 概览 ===")
    print(f"  待办总数 {m.get('todo_total')}（办结 {m.get('todo_done')} / 在办 "
          f"{m.get('todo_open')} / 认领中 {m.get('todo_claimed')}）")
    print(f"  暂停中的 run: {m.get('pending_runs')}")
    print(f"  决策分布: {m.get('by_decision')}")
    print(f"  角色分布: {m.get('by_role')}")
    print(f"  规则表指纹: {m.get('rule_table_digest')}  法规库: {m.get('regulation_count')} 条")
    pending = orch.pending(status="open", limit=10)
    print(f"  待办明细（最多 10 条）: {pending['count']} 条")
    for t in pending["todos"]:
        print(f"    - [{t['role_required']}] {t['title'][:44]} (run={t['run_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
