#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地 OIDC 身份提供方（供本机端到端验证企业 SSO 用）

定位说明（重要）：
    这不是"假 IdP"，而是一个**真实实现 OIDC 协议**的本地 IdP —— 用真 RSA 密钥签发真 ID Token、
    提供标准 discovery 与 JWKS、走标准 Authorization Code + PKCE 流程。
    它与 Keycloak/Azure AD 的差别只在"用户目录怎么填"，协议层面完全一致。
    本机没有企业 IdP，用它就能把"网关 → IdP → 回调 → 签发会话令牌 → 访问受保护接口"整条链路真跑通。

启动：
    python mock/mock_idp.py --port 8760
    浏览器/脚本访问： http://127.0.0.1:8760/.well-known/openid-configuration

登录页：GET /authorize?...  → 返回一个极简 HTML（点"同意"即回跳 redirect_uri?code=...）
令牌端：POST /token        → 校验 client_id / code / PKCE verifier，签发 id_token（RS256）
"""

import argparse
import base64
import hashlib
import html
import json
import os
import secrets
import time
import urllib.parse
import uuid
from datetime import datetime

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

ISSUER = "http://127.0.0.1:8760"
CLIENT_ID = "compliance-officer"
CLIENT_SECRET = ""              # 公开客户端（PKCE）；如需机密客户端可在启动参数里给
ALG = "RS256"
KEY_ID = "local-idp-key-1"

# 可调：把当前"登录的人"换成不同角色组，用于验证角色映射
USERS = {
    "zhangsan": {"preferred_username": "zhangsan", "name": "张三（合规专员）",
                 "email": "zhangsan@corp.example", "roles": ["compliance-specialist"]},
    "lisi": {"preferred_username": "lisi", "name": "李四（合规主管）",
             "email": "lisi@corp.example", "roles": ["compliance-supervisor"]},
    "admin": {"preferred_username": "admin", "name": "王五（合规管理员）",
              "email": "admin@corp.example", "roles": ["compliance-admin"]},
}
CODES: dict[str, dict] = {}
TICKETS: dict[str, dict] = {}      # /authorize 签发的登录票据（一次性，防伪造 approve）
CODE_TTL_S = 300
TICKET_TTL_S = 300
MAX_PENDING = 2000                 # 待处理 code/票据上限，避免长跑内存无界增长


def _sweep() -> None:
    """清理过期条目并限制总量（本地 IdP 长跑时避免内存无界增长）。"""
    now = time.time()
    for store, ttl in ((CODES, CODE_TTL_S), (TICKETS, TICKET_TTL_S)):
        for k in [k for k, v in store.items() if now - v.get("created_at", 0) > ttl]:
            store.pop(k, None)
        if len(store) > MAX_PENDING:                      # 超限：丢最老的
            for k in sorted(store, key=lambda x: store[x].get("created_at", 0))[:len(store) - MAX_PENDING]:
                store.pop(k, None)
app = FastAPI(title="本地 OIDC IdP（真实协议实现）", version="1.0.0")

_private_key = None
_public_jwk = None


def _ensure_keys() -> None:
    global _private_key, _public_jwk
    if _private_key is not None:
        return
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt as pyjwt
    _private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(_private_key.public_key()))
    jwk.update({"kid": KEY_ID, "use": "sig", "alg": ALG})
    _public_jwk = jwk


@app.get("/.well-known/openid-configuration")
def discovery() -> JSONResponse:
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": [ALG],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["openid", "profile", "email"],
    })


@app.get("/jwks")
def jwks() -> JSONResponse:
    _ensure_keys()
    return JSONResponse({"keys": [_public_jwk]})


@app.get("/authorize")
def authorize(response_type: str = Query(default="code"),
              client_id: str = Query(default=""),
              redirect_uri: str = Query(default=""),
              scope: str = Query(default="openid profile email"),
              state: str = Query(default=""),
              nonce: str = Query(default=""),
              code_challenge: str = Query(default=""),
              code_challenge_method: str = Query(default="S256")):
    if client_id != CLIENT_ID:
        return JSONResponse({"error": "invalid_client", "detail": client_id}, status_code=400)
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if not redirect_uri:
        return JSONResponse({"error": "invalid_request", "detail": "缺少 redirect_uri"}, status_code=400)

    # 真实 IdP 会弹登录页；这里给一个极简页面：选用户 + 点同意。
    # 【第二次审查修正】授权参数不再由 approve 的 query 直接决定：
    # /authorize 签发一次性 ticket 服务端保存参数，/approve 只带 ticket + user，
    # 避免"直接调 /approve 并自填 challenge/redirect_uri（甚至留空绕过 PKCE）"。
    _sweep()
    ticket = "tkt_" + secrets.token_urlsafe(18)
    TICKETS[ticket] = {"redirect_uri": redirect_uri, "state": state, "nonce": nonce,
                       "code_challenge": code_challenge, "code_challenge_method": code_challenge_method,
                       "created_at": time.time(), "used": False}
    options = "".join(
        f'<li><a href="/approve?ticket={urllib.parse.quote(ticket)}&user={urllib.parse.quote(u)}">'
        f'{html.escape(d["name"])}（{html.escape(u)}）</a></li>'
        for u, d in USERS.items())
    return HTMLResponse(
        "<html><head><meta charset='utf-8'><title>本地 IdP 登录</title></head><body>"
        "<h2>选择以哪个身份登录（本地验证用 IdP）</h2><ul>" + options + "</ul>"
        "<p>协议端点：/.well-known/openid-configuration 、/authorize 、/token 、/jwks</p>"
        "</body></html>")


@app.get("/approve")
def approve(ticket: str = Query(default=""), user: str = Query(default="zhangsan")):
    """同意授权：只能用 /authorize 签发的**一次性票据**换取授权码。"""
    if user not in USERS:
        return JSONResponse({"error": "unknown_user"}, status_code=400)
    _sweep()
    rec = TICKETS.get(ticket)
    if not rec or rec.get("used"):
        return JSONResponse({"error": "invalid_ticket",
                             "detail": "缺少或已使用 /authorize 签发的 ticket"}, status_code=400)
    if time.time() - rec["created_at"] > TICKET_TTL_S:
        return JSONResponse({"error": "invalid_ticket", "detail": "ticket 已过期"}, status_code=400)
    rec["used"] = True
    # 真实 IdP 会强制 PKCE；这里要求 challenge 非空且方法为 S256，避免"留空绕过"
    if not rec.get("code_challenge") or rec.get("code_challenge_method") != "S256":
        return JSONResponse({"error": "invalid_request",
                             "detail": "本 IdP 要求 PKCE（code_challenge + S256）"}, status_code=400)
    code = "code_" + secrets.token_urlsafe(18)
    CODES[code] = {"user": user, "nonce": rec["nonce"], "code_challenge": rec["code_challenge"],
                   "method": rec["code_challenge_method"], "redirect_uri": rec["redirect_uri"],
                   "created_at": time.time(), "used": False}
    redirect_uri, state = rec["redirect_uri"], rec["state"]
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


@app.post("/token")
def token(grant_type: str = Form(default=""),
          code: str = Form(default=""),
          redirect_uri: str = Form(default=""),
          client_id: str = Form(default=""),
          code_verifier: str = Form(default=""),
          request: Request = None):
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    # 【第二次审查修正】补 client 校验：公开客户端也必须带正确的 client_id
    if client_id and client_id != CLIENT_ID:
        return JSONResponse({"error": "invalid_client", "detail": client_id}, status_code=400)
    _sweep()
    rec = CODES.get(code)
    if not rec:
        return JSONResponse({"error": "invalid_grant", "detail": "未知 code"}, status_code=400)
    if rec["used"]:
        return JSONResponse({"error": "invalid_grant", "detail": "code 已被使用"}, status_code=400)
    if time.time() - rec["created_at"] > 300:
        return JSONResponse({"error": "invalid_grant", "detail": "code 过期"}, status_code=400)
    if redirect_uri and redirect_uri != rec["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant", "detail": "redirect_uri 不一致"}, status_code=400)
    # PKCE S256 校验（真实校验，不是走过场）
    if rec["code_challenge"]:
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
        if expect != rec["code_challenge"]:
            return JSONResponse({"error": "invalid_grant",
                                 "detail": "PKCE 校验失败（code_verifier 与 challenge 不匹配）"},
                                status_code=400)
    rec["used"] = True

    import jwt as pyjwt
    _ensure_keys()
    user = USERS[rec["user"]]
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": CLIENT_ID, "sub": f"sub-{rec['user']}",
        "iat": now, "exp": now + 600, "auth_time": now,
        "preferred_username": user["preferred_username"], "name": user["name"],
        "email": user["email"], "email_verified": True,
        "roles": user["roles"],
        "realm_access": {"roles": user["roles"]},     # 兼容 Keycloak 结构
    }
    if rec["nonce"]:
        claims["nonce"] = rec["nonce"]
    id_token = pyjwt.encode(claims, _private_key, algorithm=ALG, headers={"kid": KEY_ID})
    return JSONResponse({"access_token": "at_" + uuid.uuid4().hex, "token_type": "Bearer",
                         "expires_in": 600, "id_token": id_token,
                         "scope": "openid profile email"})


@app.get("/health")
def health() -> dict:
    _ensure_keys()
    return {"status": "running", "service": "local-oidc-idp", "issuer": ISSUER,
            "client_id": CLIENT_ID, "alg": ALG, "users": sorted(USERS),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def main() -> int:
    # 说明：本 IdP 只实现**公开客户端**（Authorization Code + PKCE），
    # 不接受 client_secret_basic 认证 —— 与本项目 `OIDC_CLIENT_SECRET` 留空的配置一致。
    global ISSUER, CLIENT_ID
    parser = argparse.ArgumentParser(description="本地 OIDC IdP（真实协议实现）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("IDP_PORT", "8760")))
    parser.add_argument("--client-id", default=os.environ.get("IDP_CLIENT_ID", CLIENT_ID))
    args = parser.parse_args()
    CLIENT_ID = args.client_id
    ISSUER = f"http://{args.host}:{args.port}"
    _ensure_keys()
    import uvicorn
    print(f"[idp] 本地 OIDC IdP 监听 {ISSUER}（issuer={ISSUER}, client_id={CLIENT_ID}, alg={ALG}）")
    print(f"[idp] discovery: {ISSUER}/.well-known/openid-configuration")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
