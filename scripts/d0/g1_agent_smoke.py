#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
G1 门禁：主 Agent 对话冒烟（真实 LLM，DeepSeek）

验证内容：
    1) 框架可导入、配置可加载、数据库/记忆/技能/工作流管理器可初始化
    2) 岗位技能包被正确发现（含不可用技能的环境变量门禁）
    3) 真实 LLM 对话可用，且会发生工具调用（ReAct 循环成立）
    4) 返回最终答复（中文）

产出：docs/evidence/d0-g1-smoke.json + 控制台全文
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, paths  # noqa: E402

bootstrap()

from config import get_global_config          # noqa: E402
from agent.agent import AgentService          # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

PROMPT = (
    "请执行两件事并汇报："
    "1) 用 time 工具告诉我当前年月日与星期；"
    "2) 用 load_skill 加载技能 compliance-sop，然后用一句话概括该技能要求你在标注风险前必须做什么。"
    "最后用中文给出不超过 120 字的总结。"
)

EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "d0-g1-smoke.json"


def main() -> int:
    ev = {"gate": "G1", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "paths": paths()}
    cfg = get_global_config()
    ev["config"] = {
        "base_url": cfg.OPENAI_BASE_URL,
        "model": cfg.MODEL_NAME,
        "max_completion_tokens": cfg.MODEL_MAX_COMPLETION_TOKEN,
        "enable_memory": cfg.ENABLE_MEMORY,
        "enable_skills": cfg.ENABLE_SKILLS,
        "enable_skill_script": cfg.ENABLE_SKILL_SCRIPT,
        "enable_mcp": cfg.ENABLE_MCP,
        "enable_workflow": cfg.ENABLE_WORKFLOW,
    }
    print(f"[G1] 工作目录: {paths()['workspace']}")
    print(f"[G1] 模型: {cfg.MODEL_NAME} @ {cfg.OPENAI_BASE_URL}")

    t0 = time.time()
    svc = AgentService()
    ev["init_seconds"] = round(time.time() - t0, 2)
    ev["tool_count"] = len(svc.tools)
    ev["tool_names"] = sorted(t.name for t in svc.tools)
    print(f"[G1] 初始化 {ev['init_seconds']}s，注册工具 {ev['tool_count']} 个")

    # 技能发现（含被环境变量门禁挡住的技能）
    skills = svc.list_skills()
    ev["skills"] = [
        {
            "name": s.name,
            "version": getattr(s, "version", None),
            "enabled": getattr(s, "enabled", None),
            "required_envs": getattr(s, "required_envs", []),
            "available": svc.skill_manager.is_available(s.name) if svc.skill_manager else None,
        }
        for s in skills
    ]
    print(f"[G1] 发现技能 {len(skills)} 个: {[s['name'] for s in ev['skills']]}")

    t1 = time.time()
    result = svc.run(PROMPT, {})
    ev["run_seconds"] = round(time.time() - t1, 2)

    msgs = result.get("messages", [])
    ev["message_count"] = len(msgs)
    ev["tool_calls"] = [
        {"name": tc.get("name"), "args_hash": len(json.dumps(tc.get("args") or {}, ensure_ascii=False))}
        for m in msgs if isinstance(m, AIMessage) for tc in (m.tool_calls or [])
    ]
    ev["tool_messages"] = len([m for m in msgs if isinstance(m, ToolMessage)])
    last = msgs[-1] if msgs else None
    ev["final_reply"] = last.content if isinstance(last, AIMessage) else str(getattr(last, "content", ""))
    ev["active_skills"] = list(svc.skill_manager._active.keys()) if svc.skill_manager else []
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    print("\n===== 工具调用序列 =====")
    for i, tc in enumerate(ev["tool_calls"], 1):
        print(f"  {i}. {tc['name']}")
    print("===== 最终答复 =====")
    print(ev["final_reply"])
    print(f"===== 激活技能: {ev['active_skills']} =====")

    ok = bool(ev["final_reply"]) and ev["tool_messages"] >= 1
    ev["verdict"] = "PASS" if ok else "FAIL"
    print(f"\n[G1] 判定: {ev['verdict']}（工具消息 {ev['tool_messages']} 条）")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[G1] 证据: {EVIDENCE}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
