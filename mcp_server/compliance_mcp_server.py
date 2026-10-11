#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
合规官「小合」· 岗位能力服务（MCP Streamable HTTP 传输层）

职责边界：
    本文件**只负责协议与传输**（JSON-RPC 2.0 + Streamable HTTP 2025-03-26 子集）；
    全部业务在 `mcp_server/tools_registry.py` 及其依赖模块中。
    这样协议层可以单测（协议正确性）与业务层单测（能力正确性）分开。

为什么用 MCP 而不是改运行时框架源码（方案设计 §2）：
    框架对 MCP 的支持是一等公民——服务端工具会被注册成 mcp__<server_id>__<tool>，
    主 Agent 与工作流的 tool 节点都能直接调用。岗位能力因此完全落在本项目进程内，
    业务能力全部在本项目进程内（D0 已用 110 个文件的哈希基线留证）。

协议实现（框架客户端实际使用的子集）：
    POST /mcp   initialize -> 响应头 Mcp-Session-Id
                notifications/initialized
                tools/list  -> tools[]
                tools/call  -> content[{type:text, text:JSON}]
    DELETE /mcp 会话终止
    GET  /health 健康检查（非 MCP 协议）
"""

import argparse
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 允许"直接以脚本方式运行"（python mcp_server/compliance_mcp_server.py）：
# 此时 sys.path[0] 是 mcp_server/ 目录，包内绝对导入（from mcp_server import ...）会失败。
# 这里把项目根加入 path，使脚本方式与模块方式（python -m）行为一致。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 【审查修正 P0】服务侧必须与网关/自检进入同一个运行时上下文：
#   1) 框架目录入 sys.path —— 否则 queue_store/runtime_store 的 `from dao.db import get_db_path`
#      在服务进程内必 ModuleNotFoundError，于是 6 个落库工具
#      （propose_rule / approve_rule_proposal / reject_rule_proposal / record_decision /
#        list_pending / list_audit / metrics）只在"进程内自检"里绿、在真实 MCP 通道里坏。
#   2) chdir 到框架工作区 —— 框架 dao 用 Path.cwd()/.lemonclaw/lemonclaw.db 定位库文件，
#      不改 cwd 会落到 <项目根>/.lemonclaw/lemonclaw.db，与网关的库不是同一个文件。
# 两者都只在本进程内生效；岗位能力经 MCP 挂载，不需要就地改底座。
# ---------------------------------------------------------------------------
_FRAMEWORK = Path(os.environ.get("LEMONCLAW_HOME",
                                 str(_PROJECT_ROOT.parent / "lemonclaw-main")))
if _FRAMEWORK.exists() and str(_FRAMEWORK) not in sys.path:
    sys.path.insert(0, str(_FRAMEWORK))
_WORKSPACE = _PROJECT_ROOT / ".runtime" / "workspace"
# 【实跑容器发现·P0】必须先建出工作区再 chdir（原因同 gateway/app.py：否则框架库
#   与 runtime_store 的规范路径分裂成两份，审批相关调用全部失败）
_WORKSPACE.mkdir(parents=True, exist_ok=True)
os.chdir(_WORKSPACE)

from mcp_server import contract_parser, regulations_store, rules_store  # noqa: E402
from mcp_server.tools_registry import TOOLS, list_tools_payload  # noqa: E402

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "compliance-officer"
SERVER_VERSION = "0.2.0"


# ============================================================ JSON-RPC

class Session:
    __slots__ = ("session_id", "initialized", "lock")

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.initialized = False
        self.lock = threading.Lock()


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()

STATS = {"initialize": 0, "tools_list": 0, "tools_call": 0, "call_names": {}, "errors": 0}


def _rpc_result(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def handle_rpc(payload: dict, session: Session | None):
    """返回 (response|None, new_session_id|None)；通知返回 (None, None)。"""
    method = payload.get("method")
    rid = payload.get("id")
    params = payload.get("params") or {}

    if method == "initialize":
        STATS["initialize"] += 1
        sid = (session.session_id if session else None) or uuid.uuid4().hex
        with SESSIONS_LOCK:
            SESSIONS.setdefault(sid, Session(sid))
        return _rpc_result(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }), sid

    if method == "notifications/initialized":
        if session:
            session.initialized = True
        return None, None

    if method == "tools/list":
        STATS["tools_list"] += 1
        return _rpc_result(rid, {"tools": list_tools_payload()}), None

    if method == "tools/call":
        STATS["tools_call"] += 1
        name = params.get("name")
        args = params.get("arguments") or {}
        with SESSIONS_LOCK:
            STATS["call_names"][name] = STATS["call_names"].get(name, 0) + 1
        spec = TOOLS.get(name)
        if spec is None:
            STATS["errors"] += 1
            return _rpc_result(rid, {"isError": True, "content": [
                {"type": "text", "text": f"未知工具: {name}"}]}), None
        try:
            out = spec["fn"](**args)
            return _rpc_result(rid, {"isError": False, "content": [
                {"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}]}), None
        except TypeError as exc:
            # 参数不匹配是最常见的调用错误，单独归类便于定位
            STATS["errors"] += 1
            return _rpc_result(rid, {"isError": True, "content": [
                {"type": "text", "text": f"参数错误（{name}）：{exc}"}]}), None
        except Exception as exc:
            STATS["errors"] += 1
            return _rpc_result(rid, {"isError": True, "content": [
                {"type": "text", "text": f"{type(exc).__name__}: {exc}"}]}), None

    if method == "ping":
        return _rpc_result(rid, {}), None

    return _rpc_error(rid, -32601, f"Method not found: {method}"), None


# ============================================================ HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def _json(self, code: int, obj, extra_headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _session(self):
        sid = self.headers.get("Mcp-Session-Id")
        if not sid:
            return None
        with SESSIONS_LOCK:
            return SESSIONS.get(sid)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            self._json(200, {"status": "running", "server": SERVER_NAME,
                             "version": SERVER_VERSION, "sessions": len(SESSIONS),
                             "stats": STATS})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        sid = self.headers.get("Mcp-Session-Id")
        if sid:
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path.rstrip("/") not in ("/mcp", ""):
            self._json(404, {"error": f"unknown path {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._json(400, _rpc_error(None, -32700, f"Parse error: {exc}"))
            return

        session = self._session()
        try:
            resp, new_sid = handle_rpc(payload, session)
        except Exception as exc:
            self._json(200, _rpc_error(payload.get("id"), -32603, f"Internal error: {exc}"))
            return

        extra = {"Mcp-Session-Id": new_sid} if new_sid else {}
        if resp is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            return
        self._json(200, resp, extra)


def main() -> int:
    parser = argparse.ArgumentParser(description="合规官岗位能力服务（MCP Streamable HTTP）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    print(f"[mcp] {SERVER_NAME} v{SERVER_VERSION} 监听 http://{args.host}:{args.port}/mcp")
    print(f"[mcp] 工具 {len(TOOLS)} 个: {', '.join(sorted(TOOLS))}")
    print(f"[mcp] 规则 {len(rules_store.active())}/{len(rules_store.load_all())} 条 active"
          f"（指纹 {rules_store.digest()}）；法规 {regulations_store.count()} 条"
          f"（来源 {regulations_store.load()['source']}）")
    print(f"[mcp] 解析档位: {contract_parser.OCR_TIER}")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[mcp] 收到中断，退出")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
