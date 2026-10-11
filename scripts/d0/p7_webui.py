#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P7 自检：工作台前端可用性

为什么需要：界面是给人看的，自动化没法"看"，但可以验证**可用性的必要条件**：
    1) 首页能返回 200 且是 HTML（含标题与关键区块 id）
    2) 页面引用的每个接口都真实存在且能返回 200（避免"按钮点了报 404"）
    3) 示例合同接口内容齐全（低/中/高三种）
    4) 文件上传接口对 .txt 与不支持类型的行为正确（415）
    5) 页面内 JS 调用的接口路径集合与网关已注册路由集合一致（静态比对，防止漏注册）

产出：docs/evidence/p7-webui.json
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p7-webui.json"
GATEWAY = "http://127.0.0.1:8790"
TOKEN = "local-dev-token"
INDEX = PROJECT_ROOT / "gateway" / "web" / "index.html"


def ensure_gateway() -> str:
    """确保网关在线（本脚本原先假定"网关已在跑"，单独运行时全部子项会失败）。

    【审查修正 P2】改为自行拉起：网关启动前先确认岗位能力服务在线
    （图里的 MCP 工具在编译期解析，能力服务不可达会导致网关启动失败）。
    """
    import subprocess
    import urllib.request
    from _bootstrap import port_pid

    def healthy(port: int) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return True
        except Exception:
            return False

    for name, port, script, extra in (
            ("capability", 8770, PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py", []),
            ("gateway", 8790, PROJECT_ROOT / "gateway" / "app.py", [])):
        if healthy(port):
            continue
        log = PROJECT_ROOT / ".runtime" / "logs" / f"p7-webui-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log, "a", encoding="utf-8")
        subprocess.Popen([sys.executable, str(script), *extra], stdout=fh,
                         stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
                         env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
                              "PYTHONPATH": str(PROJECT_ROOT)})
        for _ in range(90):
            time.sleep(0.4)
            if healthy(port):
                break
        else:
            return f"{name} 启动失败（端口 {port}，见 {log}）"
    return "ok" if port_pid(8790) else "gateway 未监听 8790"


def call(method: str, path: str, payload=None, role: str = "specialist", token: str = TOKEN,
         raw: bytes | None = None, content_type: str = "application/json"):
    import urllib.error
    import urllib.request
    headers = {"X-Auth-Token": token, "X-Role": role}
    data = None
    if raw is not None:
        headers["Content-Type"] = content_type
        data = raw
    elif payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{GATEWAY}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ev = {"check": "P7-webui", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    checks: dict[str, dict] = {}
    problems: list[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    html = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
    check("index_file_exists", bool(html), {"path": str(INDEX), "bytes": len(html)})

    ev["gateway_boot"] = ensure_gateway()
    if ev["gateway_boot"] != "ok":
        check("gateway_available", False, {"error": ev["gateway_boot"]})

    # 1) 页面结构
    for marker, label in (("合规官「小合」", "标题"), ("id=\"tab-review\"", "合同审查台"),
                          ("id=\"tab-todos\"", "待办审批"), ("id=\"tab-runs\"", "运行概览"),
                          ("id=\"tab-rules\"", "规则库"), ("id=\"tab-proposals\"", "规则提案"),
                          ("X-Auth-Token", "认证头注入"), ("function esc(", "输出转义")):
        check(f"html_has_{label}", marker in html, {"marker": marker})

    # 2) 首页可用（要求网关在线）
    code, body = call("GET", "/")
    check("gateway_serves_index", code == 200 and "合规官" in body,
          {"status": code, "bytes": len(body), "head": body[:120]})

    # 3) 页面内引用的接口必须真实存在
    # 【审查修正 P2】原来只把 api_paths 记进证据、再探测写死的 6 个端点，
    # 文档声称的"页面路径集合 vs 网关路由集合静态比对"名存实亡。
    # 现在真做集合比对：解析网关已注册路由，逐条校验页面引用的路径都能匹配上。
    api_paths = sorted(set(re.findall(r"['\"`](/api/v1/[A-Za-z0-9_\-/${}]+)", html)))
    ev["referenced_paths"] = api_paths

    gw_src = (PROJECT_ROOT / "gateway" / "app.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'@app\.(?:get|post|put|delete)\("([^"]+)"', gw_src))
    ev["gateway_routes"] = sorted(registered)

    def _matches(ref: str) -> bool:
        """把页面里的 /api/v1/x/${id} 这类路径与注册路由做模板匹配。"""
        ref_parts = [p for p in ref.split("/") if p]
        for route in registered:
            rp = [p for p in route.split("/") if p]
            if len(rp) != len(ref_parts):
                continue
            if all(rp[i].startswith("{") or rp[i] == ref_parts[i] for i in range(len(rp))):
                return True
        return False

    unresolved = [p for p in api_paths if not _matches(p)]
    ev["unresolved_paths"] = unresolved
    check("referenced_paths_all_registered", not unresolved,
          {"referenced": len(api_paths), "registered": len(registered), "unresolved": unresolved})

    # 无法直接 GET 的接口（需要 POST/参数）用其静态前缀探测；
    # 【审查修正 P2】/api/v1/proposals 现已按权限矩阵收敛为"主管及以上"，
    # 探测必须带上对应角色，否则会把"正确的 403"误判成失败。
    probe_expect = {
        "/api/v1/samples": (200, "specialist"),
        "/api/v1/metrics": (200, "specialist"),
        "/api/v1/rules": (200, "specialist"),
        "/api/v1/pending": (200, "specialist"),
        "/api/v1/runs": (200, "specialist"),
        "/api/v1/proposals": (200, "supervisor"),
    }
    endpoint_results = {}
    for path, (expect, role) in probe_expect.items():
        c, _ = call("GET", path, role=role)
        endpoint_results[path] = c
        check(f"endpoint_{path.replace('/', '_')}", c == expect, {"status": c, "role": role})
    # 专员访问主管专属接口必须 403（权限矩阵生效）
    c_deny, _ = call("GET", "/api/v1/proposals", role="specialist")
    check("proposals_denied_for_specialist", c_deny == 403, {"status": c_deny})
    ev["endpoint_results"] = endpoint_results

    # 4) 示例合同齐全
    code, body = call("GET", "/api/v1/samples")
    try:
        samples = json.loads(body).get("samples", {})
    except Exception:
        samples = {}
    check("samples_complete", set(samples.keys()) >= {"low", "mid", "high"},
          {"keys": sorted(samples.keys())})

    # 5) 文件上传（.txt 正常、不支持类型 415）
    boundary = "----dsh"
    txt = "技术服务合同\n第一条 甲方委托乙方提供运维服务。\n第二条 乙方违反保密义务的，违约金为合同总额的 50%。\n"
    body_bytes = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                  f"filename=\"demo.txt\"\r\nContent-Type: text/plain\r\n\r\n{txt}\r\n--{boundary}--\r\n"
                  ).encode("utf-8")
    c_up, up_body = call("POST", "/api/v1/contracts/upload", raw=body_bytes,
                         content_type=f"multipart/form-data; boundary={boundary}")
    try:
        up = json.loads(up_body)
    except Exception:
        up = {}
    check("upload_txt_works", c_up == 200 and up.get("run_id"),
          {"status": c_up, "run_id": up.get("run_id"), "risk": up.get("risk_level"),
           "body_head": up_body[:160]})

    bad_bytes = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"demo.xlsx\"\r\nContent-Type: application/vnd.ms-excel\r\n\r\nxx\r\n"
                 f"--{boundary}--\r\n").encode("utf-8")
    c_bad, bad_body = call("POST", "/api/v1/contracts/upload", raw=bad_bytes,
                           content_type=f"multipart/form-data; boundary={boundary}")
    check("upload_unsupported_415", c_bad == 415, {"status": c_bad, "body": bad_body[:120]})

    # 6) 认证：首页免认证可访问（否则用户打不开）；接口仍需认证
    c_noauth, _ = call("GET", "/api/v1/metrics", token="")
    check("api_requires_token", c_noauth == 401, {"status": c_noauth})

    # 7) 生产化新增接口（账号/审计/配置/日志/员工对话）
    code, body = call("GET", "/api/v1/auth/me")
    try:
        me = json.loads(body)
    except Exception:
        me = {}
    check("auth_me_returns_identity", code == 200 and me.get("username"),
          {"status": code, "role": me.get("role"), "via": me.get("via"),
           "perms": sum(1 for v in (me.get("permissions") or {}).values() if v)})

    code, body = call("GET", "/api/v1/config", role="admin")
    try:
        cfg = json.loads(body)
    except Exception:
        cfg = {}
    check("config_view_admin_only", code == 200 and "config" in cfg,
          {"status": code, "auth_mode": (cfg.get("config") or {}).get("auth_mode"),
           "checklist": len(cfg.get("deployment_checklist") or [])})
    c403, _ = call("GET", "/api/v1/config", role="specialist")
    check("config_denied_for_specialist", c403 == 403, {"status": c403})

    code, body = call("GET", "/api/v1/users", role="admin")
    try:
        users = json.loads(body)
    except Exception:
        users = {}
    check("users_list_admin", code == 200 and users.get("count", 0) >= 1,
          {"status": code, "count": users.get("count"),
           "first": (users.get("users") or [{}])[0].get("username")})

    code, body = call("GET", "/api/v1/audit?limit=50", role="supervisor")
    try:
        audit = json.loads(body)
    except Exception:
        audit = {}
    check("audit_console_readable", code == 200 and "events" in audit,
          {"status": code, "count": audit.get("count"),
           "sample": [e.get("event") for e in (audit.get("events") or [])[:4]]})
    c403a, _ = call("GET", "/api/v1/audit", role="specialist")
    check("audit_denied_for_specialist", c403a == 403, {"status": c403a})

    code, body = call("GET", "/api/v1/logs?lines=20", role="admin")
    try:
        logs = json.loads(body)
    except Exception:
        logs = {}
    check("logs_readable_with_trace", code == 200 and logs.get("lines"),
          {"status": code, "lines": len(logs.get("lines") or []),
           "has_trace": any((l.get("trace_id") for l in (logs.get("lines") or [])))})

    code, body = call("GET", "/api/v1/agent/info", role="specialist")
    try:
        ainfo = json.loads(body)
    except Exception:
        ainfo = {}
    check("agent_runtime_ready", code == 200 and ainfo.get("ok"),
          {"status": code, "tools": ainfo.get("tool_total"),
           "mcp_tools": len(ainfo.get("mcp_tools") or []), "error": ainfo.get("error")})

    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    print(f"[webui] 页面引用接口 {len(api_paths)} 个：{api_paths}")
    print(f"[webui] 接口探测：{endpoint_results}")
    print(f"[webui] 上传 .txt -> {c_up}；不支持类型 -> {c_bad}；无 token -> {c_noauth}")
    print(f"[webui] 身份/配置/账号/审计/日志/员工：{me.get('role')} | "
          f"auth_mode={(cfg.get('config') or {}).get('auth_mode')} | "
          f"users={users.get('count')} | audit={audit.get('count')} | "
          f"logs={len(logs.get('lines') or [])} | "
          f"员工工具={ainfo.get('tool_total')}（MCP {len(ainfo.get('mcp_tools') or [])}）")
    print(f"[webui] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[webui] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
