#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
企业 SSO（OIDC）单元测试 —— 用**本地假 IdP** 走完整流程，不依赖外网

运行：
    .runtime\\venv\\Scripts\\python.exe -m pytest tests -q

覆盖重点（都是 SSO 接入最容易出事的地方）：
    - PKCE：授权请求必须带 S256 challenge，兑换时必须带 verifier
    - state：一次性、过期即拒（防重放/CSRF）
    - ID Token 严格校验：签名、iss、aud、exp、iat、nonce、alg=none 一律拒绝
    - 声明映射：组 → 角色、点号路径（realm_access.roles）、管理员邮箱兜底、默认角色
    - 落地策略：自动开户、角色同步、**已停用账号不得通过 SSO 复活**、SSO 账号不能用口令登录
"""

import base64
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, os.environ.get("LEMONCLAW_HOME", str(PROJECT_ROOT.parent / "lemonclaw-main")))

from service import sso as S  # noqa: E402


@pytest.fixture()
def workdir():
    base = PROJECT_ROOT / ".runtime" / "testtmp"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"s{int(time.time() * 1000)}-{time.perf_counter_ns() % 100000}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================ 假 IdP

class _IdpHandler(BaseHTTPRequestHandler):
    """极简 OIDC Provider：discovery + JWKS + token 端点。"""

    issuer = ""
    client_id = "compliance-officer"
    client_secret = ""
    alg = "HS256"
    private_key = None            # RS256 时使用
    claims: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).seen.append({"method": "GET", "path": self.path})
        if self.path.endswith("/.well-known/openid-configuration"):
            return self._json({
                "issuer": type(self).issuer,
                "authorization_endpoint": f"{type(self).issuer}/authorize",
                "token_endpoint": f"{type(self).issuer}/token",
                "jwks_uri": f"{type(self).issuer}/jwks",
            })
        if self.path.endswith("/jwks"):
            return self._json({"keys": self._jwks()})
        return self._json({"error": "not found"}, 404)

    def _jwks(self) -> list:
        if type(self).alg.startswith("HS"):
            return []          # 对称签名不走 JWKS
        import jwt as pyjwt
        pub = type(self).private_key.public_key()
        jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(pub))
        jwk.update({"kid": "test-key", "use": "sig", "alg": type(self).alg})
        return [jwk]

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = dict(urllib.parse.parse_qsl(raw))
        type(self).seen.append({"method": "POST", "path": self.path, "form": form,
                                "auth": self.headers.get("Authorization", "")})
        if not self.path.endswith("/token"):
            return self._json({"error": "not found"}, 404)
        # 【第二次审查修正】夹具里一直设了 `token_status`，但 do_POST **从来没用过它** ——
        # "令牌端点返回 4xx/5xx" 这条最容易出错的路径其实没有任何覆盖。现在按它返回错误。
        if type(self).token_status != 200:
            return self._json({"error": "invalid_grant",
                               "error_description": "授权码无效、已过期或已被使用"},
                              type(self).token_status)
        import jwt as pyjwt
        claims = dict(type(self).claims)
        claims.setdefault("iss", type(self).issuer)
        claims.setdefault("aud", type(self).client_id)
        claims.setdefault("iat", int(time.time()))
        claims.setdefault("exp", int(time.time()) + 300)
        if "nonce" in form:
            claims.setdefault("nonce", form["nonce"])
        # 真实 IdP 会把授权请求里的 nonce 回填；这里由测试显式给（见 fix_nonce）
        if type(self).alg.startswith("HS"):
            token = pyjwt.encode(claims, type(self).client_secret, algorithm=type(self).alg)
        else:
            token = pyjwt.encode(claims, type(self).private_key, algorithm=type(self).alg,
                                 headers={"kid": "test-key"})
        return self._json({"access_token": "at-" + token[:8], "id_token": token,
                           "token_type": "Bearer", "expires_in": 300})


@pytest.fixture()
def idp():
    """起一个假 IdP，返回 (issuer, handler_class, cfg注入函数)。"""
    import jwt as pyjwt
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    except Exception:
        key = None
    _IdpHandler.private_key = key
    _IdpHandler.alg = "RS256" if key else "HS256"
    _IdpHandler.client_secret = "test-client-secret-0123456789abcdef"
    _IdpHandler.claims = {"preferred_username": "zhangsan", "name": "张三",
                          "email": "zhangsan@corp.example", "roles": ["compliance-specialist"]}
    _IdpHandler.seen = []
    _IdpHandler.token_status = 200
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _IdpHandler)
    _IdpHandler.issuer = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield _IdpHandler.issuer, _IdpHandler
    finally:
        srv.shutdown()
        srv.server_close()


def _cfg(issuer: str, **over) -> S.SsoConfig:
    base = dict(issuer=issuer, client_id="compliance-officer",
                client_secret="test-client-secret-0123456789abcdef",
                redirect_uri="http://127.0.0.1:8790/api/v1/auth/sso/callback",
                role_map={"compliance-specialist": "specialist",
                          "compliance-supervisor": "supervisor",
                          "compliance-admin": "admin"},
                verify_tls=False)
    base.update(over)
    return S.SsoConfig(**base)


@pytest.fixture(autouse=True)
def _clean_caches():
    S.clear_caches()
    yield
    S.clear_caches()


# ============================================================ 配置校验

class TestConfig:
    def test_missing_required_fields_are_reported(self):
        problems = S.SsoConfig().problems()
        assert any("OIDC_ISSUER" in p for p in problems)
        assert any("OIDC_CLIENT_ID" in p for p in problems)
        assert any("OIDC_REDIRECT_URI" in p for p in problems)

    def test_invalid_role_map_target_is_rejected(self):
        cfg = _cfg("https://sso.example", role_map={"g": "boss"})
        assert any("OIDC_ROLE_MAP" in p for p in cfg.problems())

    def test_role_map_shorthand_parsing(self, monkeypatch):
        monkeypatch.setenv("OIDC_ROLE_MAP", "grp-a=supervisor, grp-b=admin")
        cfg = S.SsoConfig.from_env()
        assert cfg.role_map == {"grp-a": "supervisor", "grp-b": "admin"}

    def test_describe_hides_secret(self, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "sso")
        monkeypatch.setenv("OIDC_CLIENT_SECRET", "top-secret")
        d = S.describe()
        assert "top-secret" not in json.dumps(d, ensure_ascii=False)
        assert d["client_secret_set"] is True


# ============================================================ 授权跳转与 PKCE

class TestStartLogin:
    def test_authorization_url_contains_pkce_and_state(self, idp):
        issuer, _ = idp
        info, err = S.start_login(_cfg(issuer))
        assert err == "", err
        q = urllib.parse.parse_qs(urllib.parse.urlparse(info["authorization_url"]).query)
        assert q["response_type"] == ["code"]
        assert q["code_challenge_method"] == ["S256"]
        assert q["state"] == [info["state"]]
        assert q["nonce"] == [info["nonce"]]
        assert q["client_id"] == ["compliance-officer"]
        assert q["scope"] == ["openid profile email"]
        # challenge 必须是 verifier 的 S256（不是明文）
        item, _ = S.consume_state(info["state"])
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(item["verifier"].encode()).digest()).decode().rstrip("=")
        assert q["code_challenge"] == [expect]
        assert q["code_challenge"] != [item["verifier"]]

    def test_state_is_one_time(self, idp):
        issuer, _ = idp
        info, _ = S.start_login(_cfg(issuer))
        first, err1 = S.consume_state(info["state"])
        assert first and err1 == ""
        second, err2 = S.consume_state(info["state"])
        assert second == {} and "过期" in err2

    def test_unknown_state_rejected(self):
        got, err = S.consume_state("not-a-state")
        assert got == {} and "无效" in err

    def test_discovery_failure_is_reported(self):
        info, err = S.start_login(_cfg("http://127.0.0.1:1"))
        assert info == {} and "discovery 失败" in err

    def test_issuer_mismatch_is_rejected(self, idp, monkeypatch):
        issuer, handler = idp
        cfg = _cfg(issuer)
        orig = handler.claims
        # 让 discovery 返回不同的 issuer
        handler_issuer = handler.issuer
        handler.issuer = "https://evil.example"
        try:
            info, err = S.start_login(cfg)
            assert info == {} and "issuer 不匹配" in err
        finally:
            handler.issuer = handler_issuer
            handler.claims = orig


# ============================================================ 令牌交换与 ID Token 校验

class TestTokenExchangeAndValidation:
    def _full_login(self, issuer, handler, nonce_source=None):
        cfg = _cfg(issuer)
        info, err = S.start_login(cfg)
        assert err == "", err
        pending, _ = S.consume_state(info["state"])
        handler.claims["nonce"] = pending["nonce"]
        tokens, terr = S.exchange_code(cfg, "code-123", pending["verifier"])
        assert terr == "", terr
        return cfg, pending, tokens

    def test_exchange_sends_verifier_and_secret(self, idp):
        issuer, handler = idp
        cfg, pending, tokens = self._full_login(issuer, handler)
        post = [s for s in handler.seen if s["method"] == "POST"][-1]
        assert post["form"]["code_verifier"] == pending["verifier"]
        assert post["form"]["grant_type"] == "authorization_code"
        assert post["form"]["redirect_uri"] == cfg.redirect_uri
        assert post["auth"].startswith("Basic "), "机密客户端必须用 Basic 认证"
        decoded = base64.b64decode(post["auth"][6:]).decode()
        assert decoded.startswith(f"{cfg.client_id}:")

    def test_valid_id_token_passes(self, idp):
        issuer, handler = idp
        cfg, pending, tokens = self._full_login(issuer, handler)
        claims, err = S.verify_id_token(cfg, tokens["id_token"], nonce=pending["nonce"])
        assert err == "", err
        assert claims["preferred_username"] == "zhangsan"

    def test_alg_none_is_rejected(self, idp):
        import jwt as pyjwt
        issuer, handler = idp
        cfg = _cfg(issuer)
        unsigned = pyjwt.encode({"iss": issuer, "aud": cfg.client_id, "sub": "x",
                                 "exp": int(time.time()) + 60, "iat": int(time.time())},
                                key="", algorithm="none")
        claims, err = S.verify_id_token(cfg, unsigned)
        assert claims == {} and "alg=none" in err

    def test_wrong_audience_rejected(self, idp):
        issuer, handler = idp
        cfg, pending, tokens = self._full_login(issuer, handler)
        bad_cfg = _cfg(issuer, client_id="other-app")
        claims, err = S.verify_id_token(bad_cfg, tokens["id_token"], nonce=pending["nonce"])
        assert claims == {} and "校验失败" in err

    def test_expired_token_rejected(self, idp):
        issuer, handler = idp
        handler.claims["exp"] = int(time.time()) - 3600
        try:
            cfg, pending, tokens = self._full_login(issuer, handler)
            claims, err = S.verify_id_token(cfg, tokens["id_token"], nonce=pending["nonce"])
            assert claims == {} and "校验失败" in err
        finally:
            handler.claims.pop("exp", None)

    def test_nonce_mismatch_rejected(self, idp):
        issuer, handler = idp
        cfg, pending, tokens = self._full_login(issuer, handler)
        claims, err = S.verify_id_token(cfg, tokens["id_token"], nonce="different-nonce")
        assert claims == {} and "nonce" in err

    def test_tampered_signature_rejected(self, idp):
        issuer, handler = idp
        cfg, pending, tokens = self._full_login(issuer, handler)
        parts = tokens["id_token"].split(".")
        parts[1] = base64.urlsafe_b64encode(
            json.dumps({"sub": "attacker", "iss": issuer, "aud": cfg.client_id,
                        "exp": int(time.time()) + 60, "iat": int(time.time())},
                       separators=(",", ":")).encode()).decode().rstrip("=")
        claims, err = S.verify_id_token(cfg, ".".join(parts), nonce=pending["nonce"])
        assert claims == {} and "校验失败" in err

    def test_missing_id_token_reported(self, idp, monkeypatch):
        issuer, handler = idp
        cfg = _cfg(issuer)
        info, _ = S.start_login(cfg)
        pending, _ = S.consume_state(info["state"])
        orig = _IdpHandler.do_POST

        def _no_id(self):
            self._json({"access_token": "only-access"})
        monkeypatch.setattr(_IdpHandler, "do_POST", _no_id)
        try:
            tokens, err = S.exchange_code(cfg, "c", pending["verifier"])
            assert tokens == {} and "id_token" in err
        finally:
            monkeypatch.setattr(_IdpHandler, "do_POST", orig)

    def test_unverified_email_rejected(self, idp):
        issuer, handler = idp
        handler.claims["email_verified"] = False
        try:
            cfg, pending, tokens = self._full_login(issuer, handler)
            claims, err = S.verify_id_token(cfg, tokens["id_token"], nonce=pending["nonce"])
            assert claims == {} and "邮箱未验证" in err
        finally:
            handler.claims.pop("email_verified", None)

    # ---------- 第二次审查补强：算法白名单与密钥类型匹配 ----------

    def test_unsupported_algorithm_rejected(self, idp):
        """对称签名（HS*）必须在缺密钥时被拒、在密钥足够长时才允许，且**必须真校验签名**。

        【第二次审查修正】原先只拒绝 `alg=none`；现在有显式算法白名单与密钥长度要求。
        """
        import jwt as pyjwt
        issuer, handler = idp
        cfg = _cfg(issuer, client_secret="")           # 公开客户端：没有对称密钥
        token = pyjwt.encode({"iss": issuer, "aud": cfg.client_id, "sub": "x",
                              "exp": int(time.time()) + 60, "iat": int(time.time())},
                             "a" * 40, algorithm="HS256")
        claims, err = S.verify_id_token(cfg, token)
        assert claims == {} and ("CLIENT_SECRET" in err.upper() or "不支持的" in err), err

        # 配置足够长的 secret 时 HS256 可用，但**换一个 secret 签的令牌必须失败**
        hs_cfg = _cfg(issuer, client_secret="b" * 40)
        bad = pyjwt.encode({"iss": issuer, "aud": cfg.client_id, "sub": "x",
                            "exp": int(time.time()) + 60, "iat": int(time.time())},
                           "c" * 40, algorithm="HS256")
        claims2, err2 = S.verify_id_token(hs_cfg, bad)
        assert claims2 == {} and ("校验失败" in err2 or "不一致" in err2), err2

    def test_short_symmetric_secret_rejected(self, idp):
        """对称密钥过短必须拒绝（否则可被离线爆破）。"""
        import jwt as pyjwt
        issuer, _ = idp
        cfg = _cfg(issuer, client_secret="short")
        token = pyjwt.encode({"iss": issuer, "aud": cfg.client_id, "sub": "x",
                              "exp": int(time.time()) + 60, "iat": int(time.time())},
                             "short", algorithm="HS256")
        claims, err = S.verify_id_token(cfg, token)
        assert claims == {} and "32 字节" in err, err

    def test_jwks_key_type_mismatch_reported(self, idp, monkeypatch):
        """JWKS 声明的算法/密钥类型与令牌不一致时，必须给出明确原因而不是笼统失败。

        【第二次审查修正】原来直接往私有缓存 `S._jwks_cache` 里塞一条"过期时间在未来
        10 分钟"的记录：既依赖私有结构，又靠"时钟"来生效（TTL 语义一旦调整，
        用例会静默变成"其实没生效却仍然通过"）。现在改为拦截 `http_json` 返回这份
        JWKS —— 走的是真实的取键路径，也不再触碰私有状态。
        """
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        issuer, handler = idp
        cfg, pending, tokens = self._full_login(issuer, handler)
        pub = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
        fake = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(pub))
        fake.update({"kid": "test-key", "use": "sig", "alg": "ES256", "kty": "EC"})
        monkeypatch.setattr(S, "http_json",
                            lambda url, **kw: (200, {"keys": [fake]}, ""))
        claims, err2 = S.verify_id_token(cfg, tokens["id_token"], nonce=pending["nonce"])
        assert claims == {} and ("不一致" in err2 or "EC" in err2), err2

    # ---------- 第二次审查补强：令牌端点的失败路径 ----------

    def test_token_endpoint_4xx_reported(self, idp):
        """令牌端点返回 4xx（授权码失效/被重放）时必须报错，且**不得产出任何令牌**。"""
        issuer, handler = idp
        cfg = _cfg(issuer)
        info, err = S.start_login(cfg)
        assert err == "", err
        pending, _ = S.consume_state(info["state"])
        handler.token_status = 400
        try:
            tokens, terr = S.exchange_code(cfg, "code-123", pending["verifier"])
        finally:
            handler.token_status = 200
        assert tokens == {}, tokens
        assert terr and ("400" in terr or "invalid_grant" in terr), terr

    def test_token_endpoint_5xx_reported(self, idp):
        """令牌端点 5xx（IdP 故障）同样必须是**显式失败**，不能退化成"拿到空令牌继续走"。"""
        issuer, handler = idp
        cfg = _cfg(issuer)
        info, err = S.start_login(cfg)
        assert err == "", err
        pending, _ = S.consume_state(info["state"])
        handler.token_status = 503
        try:
            tokens, terr = S.exchange_code(cfg, "code-123", pending["verifier"])
        finally:
            handler.token_status = 200
        assert tokens == {}, tokens
        assert terr and ("503" in terr or "失败" in terr), terr

    def test_token_endpoint_non_json_body_reported(self, idp, monkeypatch):
        """令牌端点返回非 JSON（网关 HTML 错误页）时必须报"结构异常"，不能抛异常穿透。"""
        issuer, handler = idp
        cfg = _cfg(issuer)
        info, err = S.start_login(cfg)
        assert err == "", err
        pending, _ = S.consume_state(info["state"])
        monkeypatch.setattr(S, "http_json", lambda url, **kw: (200, "<html>502</html>", ""))
        tokens, terr = S.exchange_code(cfg, "code-123", pending["verifier"])
        assert tokens == {}, tokens
        assert terr and ("结构" in terr or "JSON" in terr.upper()), terr


# ============================================================ 声明 → 角色映射

class TestRoleMapping:
    def test_group_mapping(self):
        cfg = _cfg("https://sso.example",
                   role_map={"compliance-supervisor": "supervisor"})
        role, why = S.map_role(cfg, {"roles": ["compliance-supervisor"]})
        assert role == "supervisor" and "compliance-supervisor" in why

    def test_nested_claim_path(self):
        cfg = _cfg("https://sso.example", role_claim="realm_access.roles",
                   role_map={"compliance-admin": "admin"})
        role, why = S.map_role(cfg, {"realm_access": {"roles": ["compliance-admin"]}})
        assert role == "admin"

    def test_admin_email_whitelist_wins(self):
        cfg = _cfg("https://sso.example", admin_emails=["ciso@corp.example"],
                   role_map={"g": "specialist"})
        role, why = S.map_role(cfg, {"email": "CISO@corp.example", "roles": ["g"]})
        assert role == "admin" and "OIDC_ADMIN_EMAILS" in why

    def test_role_name_passthrough(self):
        cfg = _cfg("https://sso.example")
        role, why = S.map_role(cfg, {"roles": ["supervisor"]})
        assert role == "supervisor" and "同名" in why

    def test_default_role_when_unmatched(self):
        cfg = _cfg("https://sso.example", default_role="specialist")
        role, why = S.map_role(cfg, {"roles": ["someone-else"]})
        assert role == "specialist" and "默认角色" in why

    def test_identity_from_claims_falls_back_to_email(self):
        cfg = _cfg("https://sso.example", username_claim="preferred_username")
        ident, err = S.identity_from_claims(cfg, {"email": "a.b@corp.example", "sub": "u1",
                                                 "roles": []})
        assert err == "" and ident["username"] == "a.b@corp.example"
        assert ident["role"] == "specialist"

    def test_identity_without_any_username_is_rejected(self):
        cfg = _cfg("https://sso.example")
        ident, err = S.identity_from_claims(cfg, {"sub": ""})
        assert ident == {} and "没有可用的用户名" in err

    def test_unsafe_username_chars_are_filtered(self):
        cfg = _cfg("https://sso.example")
        ident, err = S.identity_from_claims(cfg, {"preferred_username": "zhang san;DROP"})
        assert err == "" and ident["username"] == "zhangsanDROP"


# ============================================================ SSO 落地到本地账号体系

@pytest.fixture()
def auth_env(workdir, monkeypatch):
    """把 auth 的库切到临时目录，并配好 jwt/sso 环境。"""
    monkeypatch.setenv("LEMONCLAW_DB_PATH", str(workdir / "sso.db"))
    monkeypatch.setenv("AUTH_MODE", "sso")
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-key-0123456789-abcdef")
    monkeypatch.setenv("TOKEN_TTL_MIN", "60")
    from service import auth as A
    A._migrated.clear()
    A.ensure_schema()
    yield A
    A._migrated.clear()


class TestSsoLanding:
    def test_first_login_auto_provisions_account(self, auth_env):
        A = auth_env
        res = A.sso_login("lisi", "specialist", display_name="李四",
                          email="lisi@corp.example", subject="sub-1")
        assert res["ok"] and res["created"] is True and res["sso_only"] is True
        assert A.get_user("lisi")["role"] == "specialist"
        info = A.verify_token(res["access_token"])
        assert info["ok"] and info["username"] == "lisi" and info["role"] == "specialist"

    def test_role_change_from_idp_is_synced(self, auth_env):
        A = auth_env
        A.sso_login("wangwu", "specialist", subject="s2")
        res = A.sso_login("wangwu", "supervisor", subject="s2")
        assert res["ok"] and res["created"] is False
        assert A.get_user("wangwu")["role"] == "supervisor"
        events = [e["event"] for e in A.auth_events(limit=50)]
        assert any("sso_role_synced" in e for e in events), "角色同步必须留审计"

    def test_disabled_account_cannot_sso_back_in(self, auth_env):
        """企业里被开除的人不能靠 SSO 复活 —— 这是必须守住的边界。"""
        A = auth_env
        A.sso_login("zhaoliu", "specialist", subject="s3")
        A.set_user_status("zhaoliu", "disabled", actor="admin")
        res = A.sso_login("zhaoliu", "specialist", subject="s3")
        assert res["ok"] is False and any("停用" in e for e in res["errors"])

    def test_auto_provision_can_be_disabled(self, auth_env):
        A = auth_env
        res = A.sso_login("newbie", "specialist", auto_provision=False)
        assert res["ok"] is False and any("自动开户" in e for e in res["errors"])

    def test_sso_account_cannot_password_login(self, auth_env):
        A = auth_env
        A.sso_login("ssouser", "specialist", subject="s4")
        # 口令是随机不可知占位值：任何猜测都必须失败
        for guess in ("", "admin12345", "ssouser", "password"):
            r = A.login("ssouser", guess)
            assert r["ok"] is False, f"SSO 账号不应能用口令登录（猜 {guess!r} 成功）"

    def test_invalid_role_from_idp_is_rejected(self, auth_env):
        A = auth_env
        res = A.sso_login("odd", "superuser")
        assert res["ok"] is False and any("角色非法" in e for e in res["errors"])

    def test_issue_token_for_requires_active_user(self, auth_env):
        A = auth_env
        assert A.issue_token_for("nobody")["ok"] is False
        A.sso_login("temp", "specialist", subject="s5")
        A.set_user_status("temp", "disabled", actor="admin")
        res = A.issue_token_for("temp")
        assert res["ok"] is False and any("停用" in e for e in res["errors"])

    def test_local_admin_bootstrap_still_works_in_sso_mode(self, auth_env):
        """SSO 模式下保留本地兜底管理员（IdP 不可用时仍能进管理台）。"""
        A = auth_env
        boot = A.bootstrap_admin()
        assert boot["ok"] and boot["created"] is True
        assert "兜底管理员" in boot.get("warning", "")
        assert A.get_user("admin")["role"] == "admin"
