#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
G4 门禁：岗位能力通道（自建 MCP 服务 -> 框架原生 mcp__* 工具）

验证内容：
    1) 本项目 MCP 服务可独立启动并通过 /health 自检
    2) 框架 MCP 客户端完成 initialize / notifications/initialized / tools/list 握手
    3) 远程工具被注册为 mcp__compliance__<tool>，并带完整参数 schema
    4) 工具真实可调用且返回结构化 JSON（不是自然语言）
    5) 主 Agent 的工具集里确实出现这些工具（零改动接入的最终证明）

模式：
    --mode serve   仅启动 MCP 服务（供后台运行）
    --mode check   拉起服务 + 握手 + 调用 + 生成证据

产出：docs/evidence/d0-g4-mcp.json
"""

import argparse
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
SERVER = PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "d0-g4-mcp.json"
LOG = PROJECT_ROOT / ".runtime" / "logs" / "d0-mcp-server.log"
HOST, PORT = "127.0.0.1", 8770
BASE = f"http://{HOST}:{PORT}"


def wait_health(timeout=25.0) -> dict | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.4)
    return None


def start_server() -> subprocess.Popen:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOG, "a", encoding="utf-8")
    fh.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    fh.flush()
    return subprocess.Popen(
        [sys.executable, str(SERVER), "--host", HOST, "--port", str(PORT)],
        stdout=fh, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
             # 保证服务进程能导入本项目包（脚本方式运行时 sys.path[0] 是 mcp_server/）
             "PYTHONPATH": str(PROJECT_ROOT)},
    )


def run_check() -> int:
    ev = {"gate": "G4", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    proc = None
    health = wait_health(timeout=3)
    if health is None:
        proc = start_server()
        health = wait_health()
        ev["server_started_by_check"] = True
    else:
        ev["server_started_by_check"] = False
    ev["health"] = health
    print(f"[G4] 服务健康: {health}")
    if health is None:
        ev["verdict"] = "FAIL"
        ev["reason"] = "MCP 服务未启动"
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    from agent.mcp import get_mcp_manager

    mgr = get_mcp_manager()
    assert mgr is not None, "MCPManager 未初始化（ENABLE_MCP 是否为 true？）"
    ok, err = mgr.connect("compliance")
    if not ok:
        # 自动连接可能发生在服务启动之前（框架初始化时服务还没起），此处重试一次
        print(f"[G4] 首次握手失败（{err}），重试…")
        time.sleep(1.0)
        ok, err = mgr.connect("compliance")
    ev["handshake"] = {"ok": ok, "error": err}
    print(f"[G4] 握手结果: ok={ok} err={err}")

    servers = [
        {"server_id": s.server_id, "url": s.url, "status": s.status,
         "protocol_version": s.protocol_version, "tool_count": len(mgr.list_tools(s.server_id))}
        for s in mgr.list_servers()
    ]
    ev["servers"] = servers
    print(f"[G4] 服务端: {servers}")

    tools = mgr.list_tools("compliance")
    ev["remote_tools"] = [
        {"name": t.name, "desc": t.description[:60],
         "schema_props": sorted((t.input_schema or {}).get("properties", {}).keys())}
        for t in tools
    ]
    print(f"[G4] 远程工具 {len(tools)} 个: {[t.name for t in tools]}")

    # 通过框架的 LangChain 适配器真实调用（与 LLM 走同一条路径）
    lc_tools = {t.name: t for t in mgr.get_langchain_tools()}
    ev["registered_tool_names"] = sorted(lc_tools.keys())
    print(f"[G4] 注册到 Agent 的工具名: {ev['registered_tool_names']}")

    calls = {}
    ev["calls"] = calls
    name = "mcp__compliance__search_regulations"
    if name in lc_tools:
        raw = lc_tools[name].invoke({"query": "保本收益 格式条款 提示说明义务"})
        calls["search_regulations"] = {"raw_head": raw[:300], "is_error": raw.startswith("❌")}
        try:
            parsed = json.loads(raw)
            calls["search_regulations"]["parsed_ok"] = True
            calls["search_regulations"]["hit_count"] = len(parsed.get("hits", []))
            calls["search_regulations"]["refs"] = [h["regulation_ref"] for h in parsed.get("hits", [])]
        except Exception as exc:
            calls["search_regulations"]["parsed_ok"] = False
            calls["search_regulations"]["parse_error"] = str(exc)
        print(f"[G4] search_regulations -> {calls['search_regulations'].get('refs')}")

    name2 = "mcp__compliance__annotate_clause"
    annotate_cases = {}
    if name2 in lc_tools:
        # 用例1：明确 high（命中 RR-001 保本…收益）
        raw2 = lc_tools[name2].invoke({"clause_text": "本产品保本，年化收益不低于 5%，最终解释权归甲方所有。"})
        annotate_cases["high_case"] = {"raw_head": raw2[:300], "is_error": raw2.startswith("❌")}
        try:
            p = json.loads(raw2)
            annotate_cases["high_case"].update({
                "parsed_ok": True, "risk_level": p.get("risk_level"),
                "rule_ids": [h["rule_id"] for h in p.get("risk_hits", [])],
                "rule_table_digest": p.get("rule_table_digest"),
                "advisory_only": p.get("advisory_only"),
            })
        except Exception as exc:
            annotate_cases["high_case"].update({"parsed_ok": False, "parse_error": str(exc)})
        # 用例2：命中 mid 且带法条引用（RR-003 最终解释权）
        raw3 = lc_tools[name2].invoke({"clause_text": "本协议最终解释权归甲方所有。"})
        try:
            p3 = json.loads(raw3)
            annotate_cases["mid_case"] = {
                "parsed_ok": True, "risk_level": p3.get("risk_level"),
                "rule_ids": [h["rule_id"] for h in p3.get("risk_hits", [])],
                "refs": [h.get("regulation_ref") for h in p3.get("risk_hits", [])],
            }
        except Exception as exc:
            annotate_cases["mid_case"] = {"parsed_ok": False, "parse_error": str(exc)}
        # 用例3：无命中 -> low，且不产生无引用风险
        raw4 = lc_tools[name2].invoke({"clause_text": "双方应按约定时间完成交付，验收标准以附件一为准。"})
        try:
            p4 = json.loads(raw4)
            annotate_cases["low_case"] = {"parsed_ok": True, "risk_level": p4.get("risk_level"),
                                          "hit_count": len(p4.get("risk_hits", []))}
        except Exception as exc:
            annotate_cases["low_case"] = {"parsed_ok": False, "parse_error": str(exc)}
        print(f"[G4] annotate_clause high={annotate_cases['high_case'].get('risk_level')} "
              f"mid={annotate_cases.get('mid_case', {}).get('risk_level')} "
              f"low={annotate_cases.get('low_case', {}).get('risk_level')}")
    ev["calls"]["annotate_clause"] = annotate_cases
    calls.pop("annotate_clause", None)

    # 主 Agent 工具集里是否真的出现（零改动接入的最终证明）
    from agent.tools import create_tool_list
    agent_tools = sorted(t.name for t in create_tool_list())
    ev["agent_tool_total"] = len(agent_tools)
    ev["agent_mcp_tools"] = [t for t in agent_tools if t.startswith("mcp__")]
    print(f"[G4] 主 Agent 工具总数={len(agent_tools)}，其中 MCP 工具={ev['agent_mcp_tools']}")

    verdicts = {
        "health_ok": health.get("status") == "running",
        "handshake_ok": ok,
        "tools_discovered": len(tools) >= 3,
        "registered_names_ok": all(t.startswith("mcp__compliance__") for t in ev["registered_tool_names"])
                              and len(ev["registered_tool_names"]) >= 3,
        "search_call_ok": calls.get("search_regulations", {}).get("parsed_ok")
                          and calls.get("search_regulations", {}).get("hit_count", 0) > 0,
        "annotate_call_ok": annotate_cases.get("high_case", {}).get("risk_level") == "high"
                            and annotate_cases.get("mid_case", {}).get("risk_level") == "mid"
                            and annotate_cases.get("low_case", {}).get("risk_level") == "low"
                            and bool(annotate_cases.get("mid_case", {}).get("refs")),
        "no_citation_no_risk": annotate_cases.get("low_case", {}).get("hit_count") == 0,
        "agent_exposes_mcp": len(ev["agent_mcp_tools"]) >= 3,
    }
    ev["verdicts"] = verdicts
    ev["verdict"] = "PASS" if all(verdicts.values()) else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print("\n[G4] 子项判定:")
    for k, v in verdicts.items():
        print(f"   {k}: {'PASS' if v else 'FAIL'}")
    print(f"[G4] 总判定: {ev['verdict']}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[G4] 证据: {EVIDENCE}")

    if proc is not None:
        proc.terminate()
    return 0 if ev["verdict"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="check", choices=["serve", "check"])
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if args.mode == "serve":
        # 前台启动（供后台任务托管）；直接复用服务实现
        sys.path.insert(0, str(PROJECT_ROOT))
        from mcp_server.compliance_mcp_server import main as serve_main
        sys.argv = ["compliance_mcp_server.py", "--host", args.host, "--port", str(args.port)]
        return serve_main()
    return run_check()


if __name__ == "__main__":
    sys.exit(main())
