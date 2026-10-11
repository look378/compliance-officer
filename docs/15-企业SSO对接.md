# 企业 SSO 对接指南（OIDC）

> 面向 **贵司 IT / 身份管理员**。目标：把「小合」接进公司统一身份，做到
> **用公司账号登录、按公司组织架构自动分配角色、员工停用立即失效**。
>
> 代码实现：`service/sso.py`（OIDC 协议层）+ `service/auth.py`（本地账号落地）+ 网关回调。
> 自动化验证：`tests/test_sso.py`（34 项，含本地假 IdP 跑完整流程，不依赖外网）。

---

## 1. 支持的形态

| 贵司身份系统 | 是否支持 | 说明 |
|---|---|---|
| Keycloak / Red Hat SSO | ✅ | 标准 OIDC discovery，本指南即按其默认行为书写 |
| Azure AD / Entra ID | ✅ | 用「应用注册 → 添加客户端 ID 与重定向 URI」；`roles` 需在应用清单里开启 |
| Okta / Auth0 / 自建 OIDC | ✅ | 只要有 `/.well-known/openid-configuration` |
| 企业微信 / 飞书 / 钉钉 | ⚠️ 需中转 | 这三家的原生协议不是 OIDC；通常经内部网关转成 OIDC 后再接 |
| LDAP / AD 直连（无 OIDC） | ❌ 不在本期范围 | 可用 `AUTH_MODE=jwt` 的本地账号表过渡，或由贵司网关封装 OIDC |

**签名算法**：优先 **RS256 + JWKS**（标准做法，本实现完整支持）；
`HS256`（对称，把 client_secret 当密钥）也支持，但要求 secret ≥32 字节，
**不建议**作为长期方案。

---

## 2. 配置（环境变量）

```ini
AUTH_MODE=sso
# 本项目自己的会话令牌密钥（SSO 登录成功后由本项目签发 JWT）
JWT_SECRET=<至少 16 字符，建议 48 位随机>
TOKEN_TTL_MIN=480

# ---- IdP 基本信息 ----
OIDC_ISSUER=https://sso.corp.example/realms/corp
OIDC_CLIENT_ID=compliance-officer
OIDC_CLIENT_SECRET=<机密客户端才需要；公开客户端留空>
OIDC_REDIRECT_URI=http://127.0.0.1:8790/api/v1/auth/sso/callback
OIDC_SCOPES=openid profile email
OIDC_VERIFY_TLS=true            # 内网自签证书可临时 false（不推荐长期）

# ---- 角色映射（把公司组映射为本项目角色）----
OIDC_ROLE_CLAIM=roles           # 支持点号路径，如 realm_access.roles / groups
OIDC_ROLE_MAP={"compliance-supervisor":"supervisor","compliance-admin":"admin","compliance-specialist":"specialist"}
OIDC_DEFAULT_ROLE=specialist    # 未匹配到任何组时的默认角色
OIDC_ADMIN_EMAILS=ciso@corp.example,compliance-head@corp.example   # 可选兜底白名单

# ---- 其他 ----
OIDC_USERNAME_CLAIM=preferred_username
OIDC_DISPLAY_CLAIM=name
OIDC_AUTO_PROVISION=true        # 首次登录自动建号（关掉则需先手工开户）
OIDC_JWKS_TTL_S=600
OIDC_TIMEOUT_S=15
OIDC_CLOCK_SKEW_S=60            # 时钟偏差容忍
```

> 配置项写错会被 `prod_config.validate()` 拦下并**拒绝启动**，不会带病运行。

---

## 3. IdP 侧登记要点

1. **应用类型**：Web 应用（机密客户端，推荐；此时填 `OIDC_CLIENT_SECRET`）。
2. **回调地址（Redirect URI）**：必须与 `OIDC_REDIRECT_URI` **完全一致**
   （含端口、路径、http/https），否则 IdP 会拒绝授权。
3. **授权类型**：Authorization Code（本实现用 **PKCE S256**，公开客户端也能安全使用）。
4. **需要返回的声明**：
   | 声明 | 用途 | 必需 |
   |---|---|---|
   | `sub` | 唯一标识（进审计） | ✅ |
   | `preferred_username` | 本地账号名 | ✅（缺失时回落到 `email`） |
   | `name` | 界面显示名 | 建议 |
   | `email` + `email_verified` | 邮箱白名单兜底；`email_verified=false` 会被拒绝 | 建议 |
   | 角色/组声明（如 `roles`、`groups`、`realm_access.roles`） | 映射到本项目角色 | ✅（否则只能落到默认角色） |
5. **令牌有效期**：ID Token 建议 5–15 分钟；本项目会话令牌默认 480 分钟（`TOKEN_TTL_MIN`）。

---

## 4. 登录流程（实现细节，便于贵司联调排查）

```
浏览器            网关                                  IdP
  │  GET /api/v1/auth/sso/login
  │ ─────────────────────────────►  1) 取 discovery
  │                                 2) 生成 state + nonce + PKCE(verifier/challenge)
  │  ◄── {authorization_url} ───────   3) 缓存 state（10 分钟、一次性）
  │  GET authorization_url（带 code_challenge）
  │ ────────────────────────────────────────────────────►
  │  ◄── 302 redirect_uri?code=...&state=... ────────────
  │  GET /api/v1/auth/sso/callback?code&state
  │ ─────────────────────────────►  4) 校验 state（一次性、防重放）
  │                                 5) POST /token（带 code_verifier；机密客户端用 Basic）
  │                                 6) 校验 ID Token：签名(JWKS)、iss、aud、exp、iat、nonce、
  │                                    email_verified、拒绝 alg=none
  │                                 7) 声明 → 角色映射 → 本地开户/同步角色
  │                                 8) 签发本项目 JWT
  │  ◄── 302 /#token=...&via=sso ───
```

**安全设计说明**：

| 关注点 | 做法 |
|---|---|
| CSRF / 重放 | `state` 一次性、10 分钟过期；`nonce` 必须与 ID Token 一致 |
| 授权码拦截 | PKCE S256（verifier 只在服务端保存） |
| 令牌伪造 | 严格校验签名；**明确拒绝 `alg=none`**；RS256 走 JWKS（带 kid 匹配与缓存） |
| 开放重定向 | 登录后的回跳地址只接受站内相对路径（`/` 开头且非 `//`） |
| 令牌泄露到日志 | 回跳把令牌放在 URL **fragment**（`#token=`），不进入服务端访问日志与 Referer |
| 越权 | 角色来自 IdP 声明映射，**不接受**客户端自报角色；权限仍走既有 `PERMISSIONS` 矩阵 |
| 离职/调岗 | IdP 停用 → 点击登录被拒；已停用账号**不得**通过 SSO 复活；角色变更立即同步并留审计 |

---

## 5. 本地账号与 SSO 的关系

- **首次 SSO 登录自动建号**（`OIDC_AUTO_PROVISION=true`）：账号名取 `preferred_username`（回落 `email`），
  角色取映射结果。这样"谁审的"在审计链里始终可追溯。
- **SSO 账号不能用口令登录**：建号时写入随机不可知占位口令，等价于"只能用 SSO 进"。
- **角色以 IdP 为准同步**：每次登录按声明刷新本地角色，并写 `auth.sso_role_synced` 审计事件。
- **保留本地兜底管理员**：SSO 模式下启动仍会创建 `admin`（口令需立即修改），
  用于 IdP 故障时应急登入管理台。若贵司不允许，可启动后停用该账号。

---

## 6. 上线验收步骤

```powershell
# 1) SSO 协议与落地策略测试（本地假 IdP，不需要外网）
.\.runtime\venv\Scripts\python.exe -m pytest tests\test_sso.py -q

# 2) 配置自检（写错会被拦下）
.\.runtime\venv\Scripts\python.exe -c "from service.prod_config import Settings; print(Settings().validate())"

# 3) 以 sso 模式启动，检查诊断端点
#    （先按第 2 节写入 .runtime/.lemonclaw/.env，再执行 start-demo）
curl -s http://127.0.0.1:8790/api/v1/auth/sso/config
curl -s http://127.0.0.1:8790/api/v1/diagnostics -H "X-Auth-Token: local-dev-token" -H "X-Role: admin"

# 4) 浏览器打开 http://127.0.0.1:8790 → 认证设置 → 「企业登录」
#    预期：跳到贵司 IdP 登录页 → 回跳工作台并自动进入（右上角显示姓名与角色）

# 5) 全量回归（必须全绿）
pwsh -File scripts/d0/run-all-gates.ps1
```

**验收口径（建议写进验收单）**：

1. 用公司账号能登录，且**角色与公司组织架构一致**（改 IdP 组后重新登录即生效）；
2. 在 IdP 停用该账号后，登录被拒且提示明确；
3. 篡改/过期/换 aud 的 ID Token 一律被拒（`tests/test_sso.py` 已覆盖）；
4. 登录事件能在审计台按 `auth.sso_login_ok` / `auth.sso_login_rejected` 查到，含用户、角色、来源 IP；
5. 上述 1–4 均有可重跑证据（pytest 输出 + `docs/evidence/` 下的回归证据）。

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即报 `AUTH_MODE=sso 时必须设置 OIDC_*` | 必填项缺失 | 按提示补齐；这就是"拒绝带病启动"的刻意设计 |
| 回跳报 `state 无效或已过期` | 授权页停留超过 10 分钟，或浏览器禁用了会话存储 | 重新点击登录；检查是否中间有代理改写 URL |
| 报 `OIDC issuer 不匹配` | `OIDC_ISSUER` 与 discovery 里的 `issuer` 不一致（常见于多了一层 `/` 或走内网域名） | 两者改为完全一致 |
| 报 `JWKS 中找不到匹配的 kid` | IdP 轮换了签名密钥且缓存未过期 | 等 `OIDC_JWKS_TTL_S` 过期或重启服务；本实现会在缓存失效后自动重取 |
| 报 `IdP 使用对称签名（HS256）但 OIDC_CLIENT_SECRET 长度不足 32 字节` | 用了 HS256 且密钥太短 | 改用 RS256+JWKS（推荐），或换足够长的强口令 |
| 登录成功但权限不对 | 角色映射没配或组名不匹配 | 打开 `/api/v1/diagnostics` 看 `sso.role_map`；登录响应与审计里有 `role_reason` 说明"命中了哪个组/落到默认角色" |
