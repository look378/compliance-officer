#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**企业 SSO 端到端验证**：本地真实 OIDC IdP → 网关 SSO 端点 → 签发会话令牌 → 访问受保护接口

为什么这样能算"真验证"：
    IdP 是真实协议实现（真 RSA 签名、真 discovery/JWKS、真 PKCE 校验），
    跑的是**网关真实代码路径**（`/api/v1/auth/sso/login` → `/callback` → verify_id_token
    → sso_login 落地开户 → 签发本项目 JWT），不是单元测试里的替身。

一条命令内完成：起 IdP → 起能力服务与网关（AUTH_MODE=sso）→ 走完整登录 → 校验 → 收尾。

产出：docs/evidence/sso-e2e.json

----------------------------------------------------------------------------
【第二次审查修正】本脚本原先有 5 个问题，已全部修：
  1. 主体没有任何 try/except/finally —— 中途抛异常就**三个服务全留在后台**、证据也不落盘，
     下一次运行还会复用这些旧进程（旧代码/旧环境变量），门禁结果失真。
     现在主体包在 try/except/finally 里，异常也留痕，收尾必定停端口。
  2. 用的是**共享工作区库** `.runtime/workspace/.lemonclaw/lemonclaw.db`：
     自动开户会往演示库里写 zhangsan/lisi/admin 与撤销水位（污染演示数据），
     断言还会受库里既有账号影响（不确定）。现在整轮跑在独立的
     `.runtime/sso-e2e/sso-e2e.db` 上（`LEMONCLAW_DB_PATH`，开跑前先删干净）。
  3. 启动子进程用的日志文件句柄从不关闭（句柄泄漏），且**子进程是否起来只看端口**：
     进程崩溃后只报一句 "failed"，看不出原因。现在用 with 打开日志、
     并把「子进程退出码 + 日志尾部」一起记进证据。
  4. 起了能力服务却**从不检查**它是否起来（诊断接口会显示能力服务不可用，
     但脚本照样 PASS）。现在补 `capability_service_up`。
  5. IdP 登录页是否真的渲染出可选身份，只记进证据、不做断言（等于没验）；
     另外强制 `DB_MODE=sqlite`，避免外层环境带 `DB_MODE=pg` 时把账号写进真库。
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, port_pid, stop_port  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "sso-e2e.json"
LOGS = PROJECT_ROOT / ".runtime" / "logs"
# 【第二次审查修正】整轮跑在独立库上，演示库不被污染、断言也不受既有数据影响
ISOLATED_DB = PROJECT_ROOT / ".runtime" / "sso-e2e" / "sso-e2e.db"
IDP_PORT = 8760
CAP_PORT = 8770
GW_PORT = 8790
IDP = f"http://127.0.0.1:{IDP_PORT}"
GW = f"http://127.0.0.1:{GW_PORT}"
CLIENT_ID = "compliance-officer"
REDIRECT = f"{GW}/api/v1/auth/sso/callback"


def http(url: str, method: str = "GET", data: bytes | None = None,
         headers: dict | None = None, timeout: int = 30, follow: bool = True):
    """返回 (status, body_text, headers_lowercase, final_url)。

    用 httpx（项目依赖）而不是 urllib：后者在 `follow_redirects=False` 时
    对 302 的处理不可靠（实测拿不到 Location，导致流程在"回跳"这一步断掉）。
    """
    import httpx
    try:
        with httpx.Client(follow_redirects=follow, timeout=timeout) as c:
            r = c.request(method, url, content=data, headers=headers or {})
            return (r.status_code, r.text,
                    {k.lower(): v for k, v in r.headers.items()}, str(r.url))
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}", {}, url


def start(name: str, script: Path, port: int, extra_env: dict) -> dict:
    """起一个子服务并等它就绪。返回 {ok,status,pid,rc,log,log_tail}。"""
    out = {"name": name, "port": port, "script": script.name, "ok": False, "pid": None,
           "rc": None, "log": str(LOGS / f"sso-e2e-{name}.log"), "log_tail": ""}
    if port_pid(port):
        stop_port(port, why=f"{name} 需要以确定性环境重启")
        out["restarted_stale"] = True
    log = LOGS / f"sso-e2e-{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log, "a", encoding="utf-8") as fh:      # 用 with：不留句柄
            fh.write(f"\n===== {name} @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            p = subprocess.Popen([sys.executable, str(script)], stdout=fh,
                                 stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                      "PYTHONPATH": str(PROJECT_ROOT), **extra_env})
        out["pid"] = p.pid
    except Exception as exc:
        out["status"] = f"spawn-failed: {type(exc).__name__}: {exc}"
        return out
    for _ in range(90):
        time.sleep(0.4)
        if p.poll() is not None:                # 进程已经死了：立刻报出退出码，不空等
            out["rc"] = p.returncode
            break
        if port_pid(port):
            for _ in range(20):                 # 端口起来后再确认 /health 可答
                st, *_ = http(f"http://127.0.0.1:{port}/health", timeout=3)
                if st == 200:
                    out.update({"ok": True, "status": "started"})
                    return out
                time.sleep(0.5)
    if not out.get("status"):
        out["status"] = "failed"
        if p.poll() is not None:
            out["rc"] = p.returncode
    try:
        out["log_tail"] = log.read_text(encoding="utf-8", errors="replace")[-600:]
    except Exception:
        pass
    return out


def _prepare_isolated_db(ev: dict) -> None:
    """开跑前把独立库删干净：断言必须只依赖本轮写入的数据。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(ISOLATED_DB) + suffix)
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    ISOLATED_DB.parent.mkdir(parents=True, exist_ok=True)
    os.environ["LEMONCLAW_DB_PATH"] = str(ISOLATED_DB)
    os.environ["DB_MODE"] = "sqlite"            # 外层若设了 pg，会把验证账号写进真库
    ev["isolated_db"] = str(ISOLATED_DB)


def _stop_services(ev: dict) -> None:
    """收尾：三个端口一律停掉（异常路径同样要执行）。"""
    teardown = []
    for port, name in ((GW_PORT, "gateway"), (CAP_PORT, "capability"), (IDP_PORT, "idp")):
        if port_pid(port):
            r = stop_port(port, why="sso-e2e 收尾")
            r["name"] = name
            teardown.append(r)
    ev["teardown"] = teardown


def _finalize(ev: dict, checks: dict, problems: list) -> None:
    """落盘（异常路径也调用，避免证据停留在上一次运行的内容上）。"""
    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")


def main() -> int:
    ev: dict = {"check": "SSO-e2e", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "idp": IDP, "gateway": GW, "client_id": CLIENT_ID}
    checks: dict = {}
    problems: list[str] = []
    rc = 1

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    def full_login(user: str) -> dict:
        """走完整 SSO 登录（真实浏览器等价路径）。

        返回 {authorize_status, code, callback_status, token, me, location}
        """
        st_a, body_a, _, _ = http(f"{GW}/api/v1/auth/sso/login")
        info = json.loads(body_a) if st_a == 200 else {}
        auth_url = info.get("authorization_url", "")
        out = {"authorize_url_head": auth_url[:120], "start_status": st_a,
               "state_present": bool(info.get("state"))}
        if not auth_url:
            out["error"] = body_a[:200]
            return out
        # 3.1 访问 IdP 授权页（应返回登录选择页）
        st_p, html, _, _ = http(auth_url)
        out["idp_page_status"] = st_p
        out["idp_page_has_users"] = "选择以哪个身份登录" in (html or "")
        # 3.1b 授权 URL 必须带 PKCE challenge（用空 challenge 绕过 PKCE 的路径要堵死）
        q = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        out["has_pkce"] = bool(q.get("code_challenge")) and \
            q.get("code_challenge_method") == ["S256"]
        # 3.2 从登录页取**一次性 ticket**（第二次审查修正：不再自填 authorize 参数）
        m = re.search(r'/approve\?ticket=([^"&]+)', html or "")
        out["ticket_found"] = bool(m)
        if not m:
            out["error"] = "IdP 登录页未提供 ticket"
            return out
        approve_url = f"{IDP}/approve?ticket={m.group(1)}&user={urllib.parse.quote(user)}"
        st_ap, _, hdr_ap, _ = http(approve_url, follow=False)
        loc = hdr_ap.get("location", "")
        out["approve_status"] = st_ap
        out["callback_url"] = loc[:160]
        if not loc:
            out["error"] = "IdP 未返回重定向"
            return out
        # 3.3 访问网关回调（网关会自行向 IdP 换令牌并校验）
        st_cb, _, hdr_cb, _ = http(loc, follow=False)
        out["callback_status"] = st_cb
        target = hdr_cb.get("location", "")
        out["redirect_to_ui"] = target[:160]
        if target.startswith("/"):
            frag = urllib.parse.parse_qs(urllib.parse.urlparse(target).fragment)
            out["token"] = (frag.get("token") or [""])[0]
            out["via"] = (frag.get("via") or [""])[0]
        return out

    try:
        _prepare_isolated_db(ev)

        # ---------- 1) 起本地真实 IdP ----------
        ev["idp_start"] = start("idp", PROJECT_ROOT / "mock" / "mock_idp.py", IDP_PORT,
                                {"IDP_PORT": str(IDP_PORT), "IDP_CLIENT_ID": CLIENT_ID})
        check("local_idp_healthy", ev["idp_start"].get("ok") is True, ev["idp_start"])

        if checks["local_idp_healthy"]["pass"]:
            st, body, _, _ = http(f"{IDP}/.well-known/openid-configuration")
            disc = json.loads(body) if st == 200 else {}
            ev["discovery"] = disc
            check("idp_discovery_ok", st == 200 and disc.get("issuer") == IDP
                  and disc.get("jwks_uri") == f"{IDP}/jwks", {"status": st, "disc": disc})
            st_jwks, jwks_body, _, _ = http(f"{IDP}/jwks")
            jwks = json.loads(jwks_body) if st_jwks == 200 else {}
            ev["jwks_alg"] = (jwks.get("keys") or [{}])[0].get("alg")
            check("idp_jwks_has_rsa_key", st_jwks == 200 and ev["jwks_alg"] == "RS256",
                  {"keys": len(jwks.get("keys") or []), "alg": ev["jwks_alg"]})

        # ---------- 2) 起能力服务 + 网关（AUTH_MODE=sso） ----------
        jwt_secret = "sso-e2e-secret-0123456789-abcdef"
        sso_env = {
            "AUTH_MODE": "sso",
            "JWT_SECRET": jwt_secret,
            "DB_MODE": "sqlite",
            "OIDC_ISSUER": IDP,
            "OIDC_CLIENT_ID": CLIENT_ID,
            "OIDC_REDIRECT_URI": REDIRECT,
            "OIDC_ROLE_CLAIM": "roles",
            "OIDC_ROLE_MAP": json.dumps({"compliance-specialist": "specialist",
                                         "compliance-supervisor": "supervisor",
                                         "compliance-admin": "admin"}),
            "OIDC_DEFAULT_ROLE": "specialist",
            "OIDC_AUTO_PROVISION": "true",
            "OIDC_VERIFY_TLS": "false",
        }
        ev["capability_start"] = start(
            "capability", PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py",
            CAP_PORT, {"DB_MODE": "sqlite"})
        ev["gateway_start"] = start("gateway", PROJECT_ROOT / "gateway" / "app.py",
                                    GW_PORT, sso_env)
        # 【第二次审查修正】原来起了能力服务却从不检查：它挂着也能 PASS
        check("capability_service_up", ev["capability_start"].get("ok") is True,
              ev["capability_start"])
        check("gateway_up_in_sso_mode", ev["gateway_start"].get("ok") is True,
              ev["gateway_start"])

        if checks["gateway_up_in_sso_mode"]["pass"]:
            st, body, _, _ = http(f"{GW}/api/v1/auth/sso/config")
            cfg = json.loads(body) if st == 200 else {}
            ev["sso_config"] = cfg
            check("gateway_reports_sso_enabled", st == 200 and cfg.get("enabled") is True,
                  {"status": st, "body": cfg})

            # 演示令牌在 sso 模式下必须被拒（与 jwt 模式同样的姿态要求）
            st_demo, _, _, _ = http(f"{GW}/api/v1/auth/me", headers={
                "X-Auth-Token": "local-dev-token", "X-Role": "admin"})
            check("demo_token_rejected_in_sso_mode", st_demo == 401, {"status": st_demo})

            # ---------- 3) 走完整 SSO 登录（用真实浏览器等价路径） ----------
            login_sp = full_login("zhangsan")
            ev["login_specialist"] = login_sp
            check("sso_login_flow_completes",
                  login_sp.get("idp_page_status") == 200
                  and login_sp.get("approve_status") == 302
                  and login_sp.get("callback_status") == 302 and login_sp.get("via") == "sso",
                  {k: login_sp.get(k) for k in ("idp_page_status", "approve_status",
                                                "callback_status", "via", "error")})
            check("authorize_request_carries_pkce",
                  login_sp.get("has_pkce") is True and login_sp.get("ticket_found") is True,
                  {"has_pkce": login_sp.get("has_pkce"), "ticket": login_sp.get("ticket_found")})
            # 【第二次审查修正】原来只把 idp_page_has_users 记进证据、不做断言：
            # IdP 返回一个 200 的空壳页面也能过。现在它是断言的一部分。
            check("idp_login_page_renders_identity_choices",
                  login_sp.get("idp_page_has_users") is True,
                  {"idp_page_status": login_sp.get("idp_page_status"),
                   "has_users": login_sp.get("idp_page_has_users")})
            check("token_in_url_fragment_not_query",
                  bool(login_sp.get("token"))
                  and "?token=" not in (login_sp.get("redirect_to_ui") or ""),
                  {"redirect_to_ui": login_sp.get("redirect_to_ui")})

            # ---------- 4) 会话令牌必须真的能用，且角色来自 IdP 组映射 ----------
            tok = login_sp.get("token", "")
            st_me, body_me, _, _ = http(f"{GW}/api/v1/auth/me",
                                        headers={"Authorization": f"Bearer {tok}"})
            me = json.loads(body_me) if st_me == 200 else {}
            ev["me_specialist"] = {k: me.get(k) for k in ("username", "role", "via", "name")}
            check("session_token_works_and_role_mapped",
                  st_me == 200 and me.get("role") == "specialist"
                  and me.get("username") == "zhangsan" and me.get("via") == "jwt",
                  {"status": st_me, "me": ev["me_specialist"]})

            # 受保护接口：专员可读规则，不可管账号
            st_rules, _, _, _ = http(f"{GW}/api/v1/rules",
                                     headers={"Authorization": f"Bearer {tok}"})
            st_users, _, _, _ = http(f"{GW}/api/v1/users",
                                     headers={"Authorization": f"Bearer {tok}"})
            check("permission_matrix_enforced_for_sso_user",
                  st_rules == 200 and st_users == 403, {"rules": st_rules, "users": st_users})

            # ---------- 5) 主管/管理员组 → 角色映射真实生效 ----------
            login_sv = full_login("lisi")
            tok_sv = login_sv.get("token", "")
            st_sv, body_sv, _, _ = http(f"{GW}/api/v1/auth/me",
                                        headers={"Authorization": f"Bearer {tok_sv}"})
            sv = json.loads(body_sv) if st_sv == 200 else {}
            ev["me_supervisor"] = {k: sv.get(k) for k in ("username", "role")}
            st_prop, _, _, _ = http(f"{GW}/api/v1/proposals",
                                    headers={"Authorization": f"Bearer {tok_sv}"})
            check("supervisor_group_maps_to_supervisor_role",
                  st_sv == 200 and sv.get("role") == "supervisor" and st_prop == 200,
                  {"me": ev["me_supervisor"], "proposals": st_prop})

            login_ad = full_login("admin")
            tok_ad = login_ad.get("token", "")
            st_ad, body_ad, _, _ = http(f"{GW}/api/v1/auth/me",
                                       headers={"Authorization": f"Bearer {tok_ad}"})
            ad = json.loads(body_ad) if st_ad == 200 else {}
            st_diag, diag_body, _, _ = http(f"{GW}/api/v1/diagnostics",
                                            headers={"Authorization": f"Bearer {tok_ad}"})
            diag = json.loads(diag_body) if st_diag == 200 else {}
            ev["me_admin"] = {k: ad.get(k) for k in ("username", "role")}
            ev["diagnostics_sso"] = (diag.get("sso") or {})
            check("admin_group_maps_to_admin_role",
                  st_ad == 200 and ad.get("role") == "admin" and st_diag == 200,
                  {"me": ev["me_admin"], "diagnostics": st_diag,
                   "capability": diag.get("capability_service")})

            # ---------- 6) 安全边界：篡改/复用/停用 ----------
            # 6.1 code 只能用一次（真实 IdP 也会拒第二次）
            st_a, body_a, _, _ = http(f"{GW}/api/v1/auth/sso/login")
            auth_url = (json.loads(body_a) if st_a == 200 else {}).get("authorization_url", "")
            check("second_login_can_start", bool(auth_url),
                  {"status": st_a, "body": body_a[:160]})
            if auth_url:
                _, page, _, _ = http(auth_url)
                m2 = re.search(r'/approve\?ticket=([^"&]+)', page or "")
                if not m2:
                    check("authorization_code_single_use", False,
                          {"reason": "登录页无 ticket"})
                    hdr_ap = {}
                else:
                    approve_url = f"{IDP}/approve?ticket={m2.group(1)}&user=zhangsan"
                    _, _, hdr_ap, _ = http(approve_url, follow=False)
                cb = hdr_ap.get("location", "")
                ev["reuse_callback_url"] = cb[:160]
                if cb:
                    st_first, _, _, _ = http(cb, follow=False)
                    st_second, body_second, _, _ = http(cb, follow=False)
                    ev["code_reuse"] = {"first": st_first, "second": st_second,
                                        "body": body_second[:160]}
                    check("authorization_code_single_use",
                          st_first == 302 and st_second != 302, ev["code_reuse"])
                else:
                    check("authorization_code_single_use", False,
                          {"reason": "IdP 未返回回调地址",
                           "approve_status": hdr_ap.get("status")})

            # 6.2 state 复用 / 伪造必须被拒
            st_bad_state, body_bs, _, _ = http(
                f"{GW}/api/v1/auth/sso/callback?code=x&state=forged")
            ev["forged_state"] = {"status": st_bad_state, "body": body_bs[:160]}
            check("forged_state_rejected", st_bad_state == 400, ev["forged_state"])

            # 6.3 IdP 已停用该账号后，SSO 不得让其复活
            from service import auth as auth_mod
            auth_mod.set_user_status("zhangsan", "disabled", actor="sso-e2e")
            login_disabled = full_login("zhangsan")
            ev["disabled_user_login"] = {k: login_disabled.get(k) for k in
                                         ("callback_status", "redirect_to_ui", "error", "token")}
            # 【第二次审查修正】原断言是 `callback_status != 302`：None（IdP 不可达/流程中断）
            # 也能满足，等于安全边界可以假绿。现在要求**明确拒绝**且**没有下发会话令牌**。
            check("disabled_user_cannot_sso_login",
                  login_disabled.get("callback_status") in (400, 401)
                  and not login_disabled.get("token"),
                  ev["disabled_user_login"])
            auth_mod.set_user_status("zhangsan", "active", actor="sso-e2e")

            # ---------- 7) 审计留痕 ----------
            st_ev, body_ev, _, _ = http(f"{GW}/api/v1/audit?event=sso&limit=50",
                                        headers={"Authorization": f"Bearer {tok_ad}"})
            evs = json.loads(body_ev).get("events", []) if st_ev == 200 else []
            ev["sso_audit_events"] = [e.get("event") for e in evs][:8]
            check("sso_login_audited",
                  any("sso_login_ok" in (e.get("event") or "") for e in evs)
                  or any("sso_role_synced" in (e.get("event") or "") for e in evs),
                  {"events": ev["sso_audit_events"]})
            rc = 0
    except Exception as exc:
        # 【第二次审查修正】原来没有异常保护：中途抛异常就留下三个后台服务、且不落证据
        import traceback
        ev["exception"] = f"{type(exc).__name__}: {exc}"
        ev["traceback_tail"] = traceback.format_exc()[-2000:]
        if "unexpected_exception" not in problems:
            problems.append("unexpected_exception")
        print(f"[sso-e2e] 未预期异常：{ev['exception']}")
    finally:
        _stop_services(ev)

    _finalize(ev, checks, problems)

    print(f"\n[sso-e2e] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")
    for k in ("me_specialist", "me_supervisor", "me_admin"):
        print(f"   {k}: {ev.get(k)}")
    print(f"[sso-e2e] 独立库: {ev.get('isolated_db')}")
    print(f"[sso-e2e] 证据: {EVIDENCE}")
    return 0 if rc == 0 and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
