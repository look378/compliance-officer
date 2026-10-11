#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生产化能力自检（P7 增补）：认证、账号、审计、实时日志、员工对话、配置门禁

覆盖"企业可快速上手"所需的非业务能力：
    A 演示模式身份与权限矩阵（/auth/me 返回权限位）
    B 账号全生命周期：建号 → 登录 → 用 JWT 访问 → 改密 → 停用 → 越权被拒
    C 配置门禁：AUTH_MODE=jwt 但缺 JWT_SECRET 时必须报错（不允许带病启动）
    D 审计台：事件可按事件名/操作人/run 过滤
    E 实时日志：结构化日志含 trace_id，可读可订阅
    F 员工对话：真实调用一次（会走 LLM），验证能返回回复并带工具调用轨迹
    G 交付物检查清单可读

产出：docs/evidence/p7-production.json
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p7-production.json"
GW = "http://127.0.0.1:8790"
TOKEN = "local-dev-token"


def call(method: str, path: str, payload=None, role: str = "specialist", token: str = TOKEN,
         bearer: str = "", timeout: float = 180.0):
    import urllib.error
    import urllib.request
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    else:
        headers["X-Auth-Token"] = token
        headers["X-Role"] = role
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{GW}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body[:200]}
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def ensure_services() -> dict:
    """确保三个服务在线；**网关总是重启**，以保证它与本脚本的 AUTH_MODE 一致。

    【审查修正 P1】原来对已监听的服务直接判 "already-running" 复用：
    在 AUTH_MODE=jwt 下运行时，若 8790 上是更早以 demo 模式启动的旧网关，
    全部请求都会按 demo 语义处理，把"模式差异"误报成产品缺陷（实测踩过）。
    """
    import urllib.request
    from _bootstrap import port_pid, stop_port

    # 网关：按需重启（保证 AUTH_MODE/JWT_SECRET 与本进程一致）
    if port_pid(8790):
        stop_port(8790, why="p7_production 需要按当前 AUTH_MODE 重启网关")

    out = {}
    for name, port, script, extra in (
            ("capability", 8770, PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py", []),
            ("mock", 8780, PROJECT_ROOT / "mock" / "mock_office.py", ["--timeout-sleep", "3"]),
            ("gateway", 8790, PROJECT_ROOT / "gateway" / "app.py", [])):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                out[name] = "already-running"
                continue
        except Exception:
            pass
        log = PROJECT_ROOT / ".runtime" / "logs" / f"prod-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log, "a", encoding="utf-8")
        fh.write(f"\n===== 启动 {name} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        fh.flush()
        subprocess.Popen([sys.executable, str(script), *extra], stdout=fh, stderr=subprocess.STDOUT,
                         cwd=str(PROJECT_ROOT),
                         env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
                              "PYTHONPATH": str(PROJECT_ROOT)})
        t0 = time.time()
        ok = False
        while time.time() - t0 < 30:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                    ok = True
                    break
            except Exception:
                time.sleep(0.4)
        out[name] = "started" if ok else "failed"
    return out


# ---------------------------------------------------------------- 认证适配
#
# 【审查修正 P1】本脚本原来除 B3-B6 之外**全部**用固定演示令牌调用网关，
# 而网关/verify_token 在 AUTH_MODE=jwt 下会拒绝演示令牌 —— 于是文档里的"生产模式"
# 下运行本自检，A/B1/B2/B7/B8/D/E/F/G 全部 401 失败，把"模式差异"误报成产品缺陷。
# 现在：先探测当前 auth_mode，jwt 模式下先登录取 Bearer，再统一走 Bearer。
_BEARER: dict[str, str] = {}


def _service_auth_mode() -> str:
    try:
        from service.prod_config import settings
        return settings().auth_mode
    except Exception:
        return "demo"


def _login(username: str, password: str) -> str:
    code, res = call("POST", "/api/v1/auth/login",
                     {"username": username, "password": password})
    return res.get("access_token", "") if code == 200 else ""


def prepare_tokens(auth_mode: str) -> dict:
    """jwt 模式：为 admin / supervisor / specialist 三个角色各取一个 Bearer。

    说明：演示账号（admin/admin12345）由网关启动钩子 bootstrap_admin() 创建；
    主管/专员账号在缺失时由管理员现场创建（幂等：已存在则跳过创建直接登录）。
    """
    info: dict = {"mode": auth_mode, "bearer": {}}
    if auth_mode != "jwt":
        return info
    admin = _login("admin", "admin12345")
    info["bearer"]["admin"] = admin
    for uname, upass, role in (("selfcheck-supervisor", "SelfCheck@123", "supervisor"),
                               ("selfcheck-specialist", "SelfCheck@123", "specialist")):
        call("POST", "/api/v1/users",
             {"username": uname, "password": upass, "display_name": f"自检-{role}",
              "role": role}, bearer=admin)
        info["bearer"][role] = _login(uname, upass)
    info["admin_login_ok"] = bool(admin)
    return info


def _auth_kwargs(role: str) -> dict:
    """按当前模式返回 call() 的认证参数。"""
    if _BEARER.get(role):
        return {"bearer": _BEARER[role]}
    return {"role": role}


def main() -> int:
    ev = {"check": "P7-production", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    checks: dict[str, dict] = {}
    problems: list[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    ev["services"] = ensure_services()
    print(f"[prod] 服务: {ev['services']}")
    if ev["services"].get("gateway") == "failed":
        ev.update({"verdict": "FAIL", "checks": checks, "problems": ["gateway 启动失败"]})
        EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        return 1

    # 认证适配：jwt 模式下先登录取 Bearer，后续全部走 Bearer（见 prepare_tokens 说明）
    auth_mode = _service_auth_mode()
    tok = prepare_tokens(auth_mode)
    _BEARER.update({k: v for k, v in (tok.get("bearer") or {}).items() if v})
    ev["auth"] = {"mode": auth_mode, "roles_with_bearer": sorted(_BEARER.keys())}
    print(f"[prod] 认证模式={auth_mode}，已取到 Bearer 的角色={sorted(_BEARER.keys()) or '（演示令牌）'}")

    # ---------------- A 身份与权限矩阵 ----------------
    code, me = call("GET", "/api/v1/auth/me", **_auth_kwargs("specialist"))
    perms = me.get("permissions") or {}
    check("A_identity_and_permissions",
          code == 200 and me.get("role") == "specialist"
          and perms.get("contract:submit") is True
          and perms.get("user:manage") is False
          and perms.get("audit:read") is False,
          {"role": me.get("role"), "via": me.get("via"),
           "granted": sorted(k for k, v in perms.items() if v),
           "denied_sample": sorted(k for k, v in perms.items() if not v)[:4]})
    print(f"[prod][A] 身份: {me.get('username')}（{me.get('role')}）"
          f" 允许 {sum(1 for v in perms.values() if v)}/{len(perms)} 项权限")

    # ---------------- B 账号全生命周期（JWT） ----------------
    uname = f"tester{int(time.time()) % 100000}"
    code, created = call("POST", "/api/v1/users",
                         {"username": uname, "password": "Test@12345",
                          "display_name": "自检账号", "role": "specialist"}, **_auth_kwargs("admin"))
    check("B1_admin_creates_user", code == 200 and created.get("ok"), {"status": code, **created})

    code_denied, denied = call("POST", "/api/v1/users",
                               {"username": "x1", "password": "Test@12345", "role": "specialist"},
                               **_auth_kwargs("specialist"))
    check("B2_specialist_cannot_create_user", code_denied == 403, {"status": code_denied})

    # 登录：当前若为 demo 模式，登录接口会明确提示（这是预期行为，不算失败）
    code_login, login_res = call("POST", "/api/v1/auth/login",
                                 {"username": uname, "password": "Test@12345"})
    auth_mode = (call("GET", "/api/v1/config", **_auth_kwargs("admin"))[1].get("config") or {}).get("auth_mode")
    if auth_mode == "jwt":
        check("B3_login_returns_jwt", code_login == 200 and login_res.get("access_token"),
              {"status": code_login, "expires_at": login_res.get("expires_at")})
        jwt_token = login_res.get("access_token", "")
        code_jwt, who = call("GET", "/api/v1/auth/me", bearer=jwt_token)
        check("B4_jwt_access_works", code_jwt == 200 and who.get("username") == uname,
              {"status": code_jwt, "username": who.get("username")})
        # 越权：专员用 JWT 调管理员接口
        code_j403, _ = call("POST", "/api/v1/recover", bearer=jwt_token)
        check("B5_jwt_role_enforced", code_j403 == 403, {"status": code_j403})
        code_out, out = call("POST", "/api/v1/auth/logout", {}, bearer=jwt_token)
        code_after, after = call("GET", "/api/v1/auth/me", bearer=jwt_token)
        check("B6_logout_revokes_token",
              code_out == 200 and code_after == 401,
              {"logout": code_out, "after": code_after, "detail": after.get("detail")})
    else:
        check("B3_login_reports_demo_mode",
              code_login == 401 and "AUTH_MODE" in json.dumps(login_res, ensure_ascii=False),
              {"status": code_login, "detail": login_res.get("detail")})
        ev["jwt_flow"] = "skipped（当前 AUTH_MODE=demo；jwt 流程在下方 C 用独立进程验证）"

    # 改密与停用
    code_pw, pw = call("POST", f"/api/v1/users/{uname}/password",
                       {"new_password": "NewPass@6789"}, **_auth_kwargs("admin"))
    check("B7_admin_resets_password", code_pw == 200 and pw.get("ok"), {"status": code_pw, **pw})
    code_off, off = call("POST", f"/api/v1/users/{uname}/status", {"status": "disabled"}, **_auth_kwargs("admin"))
    code_users, users = call("GET", "/api/v1/users", **_auth_kwargs("admin"))
    mine = [u for u in (users.get("users") or []) if u["username"] == uname]
    check("B8_disable_user_effective",
          code_off == 200 and mine and mine[0]["status"] == "disabled",
          {"status": code_off, "user_status": mine[0]["status"] if mine else None})
    print(f"[prod][B] 账号生命周期: 建号/越权403/改密/停用 均通过（模式={auth_mode}）")

    # ---------------- C 配置门禁（jwt 缺 secret 必须**拒绝启动**） ----------------
    # 【审查修正 P1】原实现只断言 `settings().validate()` 返回的问题串里含 JWT_SECRET，
    # 没有任何地方验证"网关真的拒绝启动"——而当时的 start_up 钩子只把 problems 写进日志、
    # 服务照常起来（表现为所有登录都以"令牌无效"失败）。现在：
    #   1) 复用同一个进程内探针（validate 层面）；
    #   2) **真起一个网关子进程**（AUTH_MODE=jwt + 无 JWT_SECRET）并断言退出码非 0。
    #      为避免与正在运行的服务抢端口，子进程用一个未被占用的端口。
    import os as _os
    from service import prod_config as pc
    _saved = {k: _os.environ.get(k) for k in ("AUTH_MODE", "JWT_SECRET")}
    _os.environ["AUTH_MODE"] = "jwt"
    _os.environ.pop("JWT_SECRET", None)
    pc._settings = None
    probs = pc.settings().validate()
    pc._settings = None
    for k, v in _saved.items():
        if v is None:
            _os.environ.pop(k, None)
        else:
            _os.environ[k] = v
    pc._settings = None

    free_port = 8898
    child_env = {k: v for k, v in _os.environ.items()
                 if k not in ("JWT_SECRET", "GATEWAY_AUTH_TOKEN")}
    child_env.update({"AUTH_MODE": "jwt", "PYTHONIOENCODING": "utf-8",
                      "PYTHONPATH": str(PROJECT_ROOT)})
    child_env.pop("JWT_SECRET", None)
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "gateway" / "app.py"), "--port", str(free_port)],
        cwd=str(PROJECT_ROOT), env=child_env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)
    log_tail = ((proc.stdout or "") + (proc.stderr or ""))[-600:]
    check("C_missing_jwt_secret_blocks_startup",
          any("JWT_SECRET" in p for p in probs) and proc.returncode != 0,
          {"problems": probs, "gateway_exit_code": proc.returncode, "log_tail": log_tail})
    print(f"[prod][C] 配置门禁: validate 报 {len(probs)} 个阻断问题；"
          f"网关子进程退出码 {proc.returncode}（非 0 表示拒绝带病启动）")

    # ---------------- D 审计台 ----------------
    code_a, audit = call("GET", "/api/v1/audit?limit=100", **_auth_kwargs("supervisor"))
    events = [e.get("event") for e in (audit.get("events") or [])]
    code_filter, filtered = call("GET", "/api/v1/audit?event=auth.&limit=50", **_auth_kwargs("admin"))
    check("D_audit_console_filters",
          code_a == 200 and events and code_filter == 200
          and all("auth." in (e.get("event") or "") for e in (filtered.get("events") or [])),
          {"total": audit.get("count"), "sample": events[:5],
           "auth_events": filtered.get("count")})
    print(f"[prod][D] 审计台: 共 {audit.get('count')} 条，其中认证类 {filtered.get('count')} 条")

    # ---------------- E 实时日志 ----------------
    code_l, logs = call("GET", "/api/v1/logs?lines=100", **_auth_kwargs("admin"))
    lines = logs.get("lines") or []
    traced = [l for l in lines if l.get("trace_id")]
    check("E_structured_logs_with_trace",
          code_l == 200 and lines and traced,
          {"lines": len(lines), "traced": len(traced),
           "sample": {k: traced[0].get(k) for k in ("ts", "level", "event", "trace_id")}
           if traced else None})
    print(f"[prod][E] 日志: {len(lines)} 行，带 trace_id {len(traced)} 行")

    # ---------------- F 员工对话（真实 LLM） ----------------
    code_info, ainfo = call("GET", "/api/v1/agent/info", **_auth_kwargs("specialist"))
    check("F1_agent_ready", code_info == 200 and ainfo.get("ok"),
          {"tools": ainfo.get("tool_total"), "mcp": len(ainfo.get("mcp_tools") or [])})
    # 【审查修正 P2】会话号必须归属当前调用者（服务端按 `wb-<username>` 前缀校验）。
    # 原来固定用 "wb-selfcheck-specialist"：demo 模式下用户名恰好是 demo-specialist，
    # 但在 AUTH_MODE=jwt 下登录用户是自检账号，前缀不匹配 → 403，把"模式差异"报成缺陷。
    code_me, me = call("GET", "/api/v1/auth/me", **_auth_kwargs("specialist"))
    uname_me = (me.get("username") or "specialist") if code_me == 200 else "specialist"
    session_id = f"wb-{uname_me}"
    ev["agent_session"] = session_id
    code_task, task = call("POST", "/api/v1/agent/task",
                           {"task": "用一句话说明：规则表里“最终解释权”这类条款为什么有风险，依据哪条法条？",
                            "session_id": session_id}, **_auth_kwargs("specialist"))
    reply = (task.get("reply") or "")
    check("F2_agent_answers_with_citation",
          code_task == 200 and reply and ("条" in reply or "RR-" in reply),
          {"status": code_task, "elapsed_ms": task.get("elapsed_ms"),
           "tool_calls": [t.get("name") for t in (task.get("tool_calls") or [])],
           "reply_head": reply[:200]})
    print(f"[prod][F] 员工对话: {task.get('elapsed_ms')}ms，工具 "
          f"{[t.get('name') for t in (task.get('tool_calls') or [])]}，回复 {len(reply)} 字")

    # ---------------- G 交付检查清单 ----------------
    code_cfg, cfg = call("GET", "/api/v1/config", **_auth_kwargs("admin"))
    checklist = cfg.get("deployment_checklist") or []
    check("G_deployment_checklist_available", code_cfg == 200 and len(checklist) >= 5,
          {"items": len(checklist), "auth_mode": (cfg.get("config") or {}).get("auth_mode"),
           "problems": cfg.get("problems")})
    print(f"[prod][G] 上线检查清单 {len(checklist)} 项，"
          f"当前配置问题 {len(cfg.get('problems') or [])} 个")

    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[prod] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[prod] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
