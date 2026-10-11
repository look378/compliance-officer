#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
企业 SSO（OIDC Authorization Code + PKCE）—— 与本地账号体系并存

背景（`docs/12-代码审查报告.md` §6 的"企业 SSO"门槛）：
    原来只有两种模式：`demo`（演示令牌）与 `jwt`（本地账号表发令牌）。
    企业环境要求"用公司统一身份登录"，因此这里加第三种模式 `sso`。

设计要点：
    1. **走标准 OIDC Authorization Code + PKCE**：兼容 Keycloak / Azure AD / Okta /
       企业微信/飞书（OIDC 模式）/ 自建 IdP —— 只要给出 issuer 与 client_id。
    2. **ID Token 必须严格校验**：签名（JWKS）、`iss`、`aud`、`exp`、`iat`、`nonce`、
       `state` 一个都不能省。任何一项不过就拒绝登录（不"容错放行"）。
    3. **本地令牌与 SSO 解耦**：SSO 登录成功后由本项目签发**自己的** JWT，
       网关其余部分（权限矩阵、令牌撤销、审计）完全不用改。
    4. **角色映射可配置**：`OIDC_ROLE_CLAIM` + `OIDC_ROLE_MAP` 把 IdP 的组/角色
       映射到本项目的 `specialist|supervisor|admin`；未匹配时落到 `OIDC_DEFAULT_ROLE`。
       管理员组可额外用 `OIDC_ADMIN_EMAILS` 白名单兜底。
    5. **自动开户**：首次 SSO 登录在本项目用户表落一条记录（角色来自映射），
       这样"谁审的"在审计里仍然可追溯；已停用的账号不得通过 SSO 复活。

配置（环境变量，详见 docs/15-企业SSO对接.md）：
    AUTH_MODE=sso
    OIDC_ISSUER              https://sso.corp.example/realms/corp
    OIDC_CLIENT_ID           compliance-officer
    OIDC_CLIENT_SECRET       机密客户端才需要（公开客户端留空）
    OIDC_REDIRECT_URI        http://127.0.0.1:8790/api/v1/auth/sso/callback
    OIDC_SCOPES              openid profile email（默认）
    OIDC_ROLE_CLAIM          roles（默认；也支持 realm_access.roles / groups 之类的点号路径）
    OIDC_ROLE_MAP            {"compliance-supervisor":"supervisor","compliance-admin":"admin"}
    OIDC_DEFAULT_ROLE        specialist
    OIDC_ADMIN_EMAILS        admin@corp.example,ciso@corp.example（可选兜底）
    OIDC_USERNAME_CLAIM      preferred_username（默认）
    OIDC_DISPLAY_CLAIM       name（默认）
    OIDC_JWKS_TTL_S          600（JWKS 缓存秒数）
    OIDC_TIMEOUT_S           15
    OIDC_VERIFY_TLS          true
    OIDC_AUTO_PROVISION      true（首次登录自动建号）
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

ROLES = ("specialist", "supervisor", "admin")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    return default if not raw else raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


@dataclass
class SsoConfig:
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = "openid profile email"
    role_claim: str = "roles"
    role_map: dict = field(default_factory=dict)
    default_role: str = "specialist"
    admin_emails: list = field(default_factory=list)
    username_claim: str = "preferred_username"
    display_claim: str = "name"
    jwks_ttl_s: int = 600
    timeout_s: int = 15
    verify_tls: bool = True
    auto_provision: bool = True
    # 测试/内网自签场景可显式指定端点，省去 discovery
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    jwks_uri: str = ""

    @classmethod
    def from_env(cls) -> "SsoConfig":
        raw_map = _env("OIDC_ROLE_MAP")
        role_map: dict = {}
        if raw_map:
            try:
                parsed = json.loads(raw_map)
                if isinstance(parsed, dict):
                    role_map = {str(k).lower(): str(v).lower() for k, v in parsed.items()}
            except Exception:
                # 也支持 "grp1=supervisor,grp2=admin" 这种简写
                for pair in raw_map.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        role_map[k.strip().lower()] = v.strip().lower()
        return cls(
            issuer=_env("OIDC_ISSUER"),
            client_id=_env("OIDC_CLIENT_ID"),
            client_secret=_env("OIDC_CLIENT_SECRET"),
            redirect_uri=_env("OIDC_REDIRECT_URI"),
            scopes=_env("OIDC_SCOPES", "openid profile email"),
            role_claim=_env("OIDC_ROLE_CLAIM", "roles"),
            role_map=role_map,
            default_role=_env("OIDC_DEFAULT_ROLE", "specialist").lower(),
            admin_emails=[e.strip().lower() for e in _env("OIDC_ADMIN_EMAILS").split(",") if e.strip()],
            username_claim=_env("OIDC_USERNAME_CLAIM", "preferred_username"),
            display_claim=_env("OIDC_DISPLAY_CLAIM", "name"),
            jwks_ttl_s=_env_int("OIDC_JWKS_TTL_S", 600),
            timeout_s=_env_int("OIDC_TIMEOUT_S", 15),
            verify_tls=_env_bool("OIDC_VERIFY_TLS", True),
            auto_provision=_env_bool("OIDC_AUTO_PROVISION", True),
            authorization_endpoint=_env("OIDC_AUTHORIZATION_ENDPOINT"),
            token_endpoint=_env("OIDC_TOKEN_ENDPOINT"),
            jwks_uri=_env("OIDC_JWKS_URI"),
        )

    def problems(self) -> list[str]:
        """返回阻断性问题（空表示可用）。"""
        out = []
        if not self.issuer and not (self.authorization_endpoint and self.token_endpoint
                                    and self.jwks_uri):
            out.append("AUTH_MODE=sso 时必须设置 OIDC_ISSUER（或显式给出三个端点）")
        if not self.client_id:
            out.append("AUTH_MODE=sso 时必须设置 OIDC_CLIENT_ID")
        if not self.redirect_uri:
            out.append("AUTH_MODE=sso 时必须设置 OIDC_REDIRECT_URI（需与 IdP 登记值一致）")
        # 【第二次审查修正·P2】显式端点模式下也必须给出 issuer：
        # PyJWT 在 `issuer=None` 时**完全跳过 iss 校验**（已读源码确认），
        # 那与本模块"iss 一个都不能省"的声明矛盾 —— 只校验 aud 的 ID Token 都能过。
        if not self.issuer:
            out.append("显式端点模式下仍必须设置 OIDC_ISSUER：否则 ID Token 的 iss 不做校验")
        if self.default_role not in ROLES:
            out.append(f"OIDC_DEFAULT_ROLE 非法：{self.default_role}（应为 {list(ROLES)}）")
        bad = [v for v in self.role_map.values() if v not in ROLES]
        if bad:
            out.append(f"OIDC_ROLE_MAP 的目标角色非法：{bad}（应为 {list(ROLES)}）")
        return out


# ============================================================ 外部 HTTP（可测）

def http_json(url: str, method: str = "GET", data: bytes | None = None,
              headers: dict | None = None, timeout: int = 15,
              verify_tls: bool = True) -> tuple[int, Any, str]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    ctx = None
    if url.lower().startswith("https") and not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body), ""
            except Exception:
                return r.status, None, f"响应不是 JSON（前 120 字符：{body[:120]!r}）"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return exc.code, None, f"HTTP {exc.code}：{detail}"
    except Exception as exc:
        return 0, None, f"{type(exc).__name__}: {exc}"


# ============================================================ 端点发现与 JWKS

_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def discover(cfg: SsoConfig) -> tuple[dict, str]:
    """读取 OIDC discovery 文档（`{issuer}/.well-known/openid-configuration`）。"""
    if cfg.authorization_endpoint and cfg.token_endpoint and cfg.jwks_uri:
        return {"authorization_endpoint": cfg.authorization_endpoint,
                "token_endpoint": cfg.token_endpoint, "jwks_uri": cfg.jwks_uri}, ""
    if not cfg.issuer:
        return {}, "未配置 OIDC_ISSUER"
    url = cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
    with _cache_lock:
        hit = _discovery_cache.get(url)
        if hit and time.time() - hit[0] < cfg.jwks_ttl_s:
            return hit[1], ""
    code, data, err = http_json(url, timeout=cfg.timeout_s, verify_tls=cfg.verify_tls)
    if err or not isinstance(data, dict):
        return {}, f"OIDC discovery 失败：{err or '响应结构异常'}"
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not data.get(key):
            return {}, f"OIDC discovery 缺少字段 {key}"
    # issuer 必须与配置一致（防止 discovery 指向别处）
    if data.get("issuer") and data["issuer"].rstrip("/") != cfg.issuer.rstrip("/"):
        return {}, f"OIDC issuer 不匹配：配置 {cfg.issuer}，discovery 返回 {data['issuer']}"
    with _cache_lock:
        _discovery_cache[url] = (time.time(), data)
    return data, ""


def fetch_jwks(cfg: SsoConfig, jwks_uri: str) -> tuple[dict, str]:
    with _cache_lock:
        hit = _jwks_cache.get(jwks_uri)
        if hit and time.time() - hit[0] < cfg.jwks_ttl_s:
            return hit[1], ""
    code, data, err = http_json(jwks_uri, timeout=cfg.timeout_s, verify_tls=cfg.verify_tls)
    if err or not isinstance(data, dict) or not data.get("keys"):
        return {}, f"JWKS 获取失败：{err or '缺少 keys 字段'}"
    with _cache_lock:
        _jwks_cache[jwks_uri] = (time.time(), data)
    return data, ""


def clear_caches() -> None:
    with _cache_lock:
        _discovery_cache.clear()
        _jwks_cache.clear()


# ============================================================ PKCE / state

_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()
PENDING_TTL_S = 600


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def start_login(cfg: SsoConfig, next_url: str = "") -> tuple[dict, str]:
    """生成授权跳转所需的 state / code_verifier / 授权 URL。"""
    problems = cfg.problems()
    if problems:
        return {}, "；".join(problems)
    meta, err = discover(cfg)
    if err:
        return {}, err
    state = _b64url(secrets.token_bytes(24))
    nonce = _b64url(secrets.token_bytes(24))
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    with _pending_lock:
        # 顺手清理过期条目，避免 state 表无界增长
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["created_at"] > PENDING_TTL_S]:
            _pending.pop(k, None)
        _pending[state] = {"nonce": nonce, "verifier": verifier, "created_at": now,
                           "next": next_url}
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {"authorization_url": f"{meta['authorization_endpoint']}?"
                                  f"{urllib.parse.urlencode(params)}",
            "state": state, "nonce": nonce, "expires_in": PENDING_TTL_S}, ""


def consume_state(state: str) -> tuple[dict, str]:
    """取出并作废 state（一次性，防重放）。"""
    with _pending_lock:
        item = _pending.pop(state or "", None)
    if not item:
        return {}, "state 无效或已过期（请重新发起登录）"
    if time.time() - item["created_at"] > PENDING_TTL_S:
        return {}, "state 已过期（请重新发起登录）"
    return item, ""


# ============================================================ 令牌交换与校验

def exchange_code(cfg: SsoConfig, code: str, verifier: str) -> tuple[dict, str]:
    meta, err = discover(cfg)
    if err:
        return {}, err
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id,
        "code_verifier": verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cfg.client_secret:
        basic = base64.b64encode(f"{cfg.client_id}:{cfg.client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    code_status, data, err = http_json(meta["token_endpoint"], method="POST",
                                       data=urllib.parse.urlencode(form).encode(),
                                       headers=headers, timeout=cfg.timeout_s,
                                       verify_tls=cfg.verify_tls)
    if err or not isinstance(data, dict):
        return {}, f"令牌交换失败：{err or '响应结构异常'}"
    if not data.get("id_token"):
        return {}, f"令牌端点未返回 id_token（返回字段：{sorted(data.keys())}）"
    return data, ""


def verify_id_token(cfg: SsoConfig, id_token: str, nonce: str = "") -> tuple[dict, str]:
    """严格校验 ID Token：签名(JWKS) + iss + aud + exp + iat + nonce。"""
    import jwt as pyjwt

    try:
        header = pyjwt.get_unverified_header(id_token)
    except Exception as exc:
        return {}, f"ID Token 头部不可解析：{type(exc).__name__}: {exc}"
    alg = header.get("alg", "")
    if alg.lower() == "none":
        return {}, "拒绝 alg=none 的 ID Token（必须签名）"
    # 【第二次审查修正】算法白名单：只接受企业 IdP 实际使用的非对称签名算法族，
    #   避免"客户端拿 IdP 的 JWKS 当 HMAC 密钥"这类经典算法混淆攻击
    #   （虽然本实现用 PyJWK 解析公钥后不会被当对称密钥用，但显式拒绝更清晰、更可审计）。
    allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
    if not alg.startswith("HS") and alg not in allowed:
        return {}, f"不支持的 ID Token 签名算法：{alg}（支持 {'/'.join(sorted(allowed))} 或 HS*）"

    key: Any
    if alg.startswith("HS"):
        if not cfg.client_secret:
            return {}, f"IdP 使用对称签名（{alg}）但未配置 OIDC_CLIENT_SECRET"
        if len(cfg.client_secret.encode("utf-8")) < 32:
            # 对称签名把 client_secret 当 HMAC 密钥用，短密钥可被离线爆破。
            # 这不是标准 OIDC 做法（标准是 RS256 + JWKS），仅在受限场景使用。
            return {}, (f"IdP 使用对称签名（{alg}）但 OIDC_CLIENT_SECRET 长度不足 32 字节："
                        "请改用 RS256+JWKS，或提供足够长的强口令")
        key = cfg.client_secret
    else:
        meta, err = discover(cfg)
        if err:
            return {}, err
        jwks, err = fetch_jwks(cfg, meta["jwks_uri"])
        if err:
            return {}, err
        kid = header.get("kid")
        candidates = [k for k in jwks["keys"] if not kid or k.get("kid") == kid]
        if not candidates:
            return {}, f"JWKS 中找不到匹配的 kid={kid}（IdP 可能已轮换密钥）"
        # 【第二次审查修正】密钥与算法必须匹配：JWKS 明确声明了 alg/kty 时以它为准，
        # 避免"用 RSA 公钥去验 ES256 令牌"这类错配被静默放过（错配时校验必然失败，
        # 但给出明确原因能让对接排查快很多）。
        jwk = candidates[0]
        jwk_alg = (jwk.get("alg") or "").upper()
        if jwk_alg and jwk_alg != alg.upper():
            return {}, f"JWKS 声明的算法({jwk_alg})与令牌签名算法({alg})不一致"
        kty = (jwk.get("kty") or "").upper()
        if kty == "RSA" and not alg.upper().startswith(("RS", "PS")):
            return {}, f"JWKS 是 RSA 密钥，但令牌算法是 {alg}"
        if kty == "EC" and not alg.upper().startswith("ES"):
            return {}, f"JWKS 是 EC 密钥，但令牌算法是 {alg}"
        if kty and kty not in ("RSA", "EC", "OKP"):
            return {}, f"不支持的 JWKS 密钥类型：{kty}"
        try:
            key = pyjwt.PyJWK(jwk).key
        except Exception as exc:
            return {}, f"JWKS 公钥解析失败：{type(exc).__name__}: {exc}"

    options = {"require": ["exp", "iat", "iss", "aud"]}
    try:
        claims = pyjwt.decode(
            id_token, key=key, algorithms=[alg], audience=cfg.client_id,
            issuer=cfg.issuer if cfg.issuer else None, options=options,
            leeway=_env_int("OIDC_CLOCK_SKEW_S", 60))
    except Exception as exc:
        return {}, f"ID Token 校验失败：{type(exc).__name__}: {exc}"

    if nonce and claims.get("nonce") != nonce:
        return {}, "nonce 不匹配（可能存在重放攻击）"
    if claims.get("email_verified") is False:
        return {}, "邮箱未验证（IdP 返回 email_verified=false）"
    return claims, ""


# ============================================================ 声明 → 身份映射

def _dig(data: Any, path: str) -> Any:
    """按点号路径取值（支持 `realm_access.roles` 这类嵌套声明）。"""
    node = data
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def map_role(cfg: SsoConfig, claims: dict) -> tuple[str, str]:
    """把 IdP 声明映射为本项目角色，返回 (role, 命中说明)。"""
    email = str(claims.get("email", "")).lower()
    if email and email in cfg.admin_emails:
        return "admin", f"命中 OIDC_ADMIN_EMAILS（{email}）"
    raw = _dig(claims, cfg.role_claim)
    groups: list[str] = []
    if isinstance(raw, str):
        groups = [raw.lower()]
    elif isinstance(raw, (list, tuple)):
        groups = [str(g).lower() for g in raw]
    elif isinstance(raw, dict):
        groups = [str(k).lower() for k, v in raw.items() if v]
    for g in groups:
        if g in cfg.role_map:
            return cfg.role_map[g], f"组 {g} → {cfg.role_map[g]}"
    for g in groups:
        if g in ROLES:                       # IdP 直接给的就是本项目角色名
            return g, f"组 {g} 与角色同名"
    return cfg.default_role, f"未匹配任何映射，落到默认角色 {cfg.default_role}"


def identity_from_claims(cfg: SsoConfig, claims: dict) -> tuple[dict, str]:
    """claims → 本项目身份（username/role/display/email）。"""
    username = (str(_dig(claims, cfg.username_claim) or "").strip()
                or str(claims.get("email") or "").strip()
                or str(claims.get("sub") or "").strip())
    if not username:
        return {}, "IdP 返回的声明里没有可用的用户名（preferred_username/email/sub 全空）"
    # 只保留可安全落库的字符，避免把 IdP 的奇怪字符带进本地账号表
    safe = "".join(ch for ch in username if ch.isalnum() or ch in "._-@")
    if not safe:
        return {}, f"用户名不含可用字符：{username!r}"
    role, why = map_role(cfg, claims)
    return {"username": safe, "role": role, "name": str(_dig(claims, cfg.display_claim) or safe),
            "email": str(claims.get("email") or ""), "subject": str(claims.get("sub") or ""),
            "role_reason": why}, ""


def describe(cfg: SsoConfig | None = None) -> dict:
    """供诊断端点展示（不泄露 client_secret）。"""
    cfg = cfg or SsoConfig.from_env()
    return {"mode_enabled": _env("AUTH_MODE", "demo").lower() == "sso",
            "issuer": cfg.issuer or None, "client_id": cfg.client_id or None,
            "redirect_uri": cfg.redirect_uri or None,
            "client_secret_set": bool(cfg.client_secret),
            "role_claim": cfg.role_claim, "role_map": cfg.role_map,
            "default_role": cfg.default_role, "admin_emails_count": len(cfg.admin_emails),
            "auto_provision": cfg.auto_provision, "verify_tls": cfg.verify_tls,
            "problems": cfg.problems()}
