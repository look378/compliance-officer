#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
认证与权限（本项目自有，生产化）

两种模式，由 `AUTH_MODE` 决定：
    demo  演示令牌（请求头 X-Auth-Token，固定值）—— 仅用于本机演示
    jwt   账号 + 密码 + JWT 访问令牌 —— 企业部署形态

设计要点：
    - 密码用**标准库 PBKDF2-HMAC-SHA256**（随机 salt + 迭代次数），不引入编译型依赖；
    - JWT 用 PyJWT，HS256，secret 来自 `JWT_SECRET`（未设置时 jwt 模式拒绝启动）；
    - 用户与令牌撤销记录存在自有表（`compliance_user` / `compliance_token_revocation`），
      与业务库同库，便于随数据库一起迁移；
    - 角色仅三种：specialist（专员）/ supervisor（主管）/ admin（管理员），
      权限矩阵集中在 `PERMISSIONS`，便于审计与调整；
    - 登录失败有失败计数（防暴力尝试），超限临时锁定。

对外接口（gateway 调用）：
    ensure_schema() / bootstrap_admin() / login() / verify_token() / logout()
    list_users() / create_user() / set_user_status() / change_password()
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from service.prod_config import settings

PBKDF2_ITERATIONS = 200_000
MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 300
# 登录失败计数行的保留时长（超过即清理；避免被随机用户名灌爆这张表）
LOGIN_FAIL_TTL_SEC = 24 * 3600

ROLES = ("specialist", "supervisor", "admin")

# 权限矩阵：动作 -> 允许的角色
PERMISSIONS = {
    "contract:submit": ("specialist", "admin"),
    "contract:read": ("specialist", "supervisor", "admin"),
    "review:decide_specialist": ("specialist", "admin"),
    "review:decide_supervisor": ("supervisor", "admin"),
    "proposal:review": ("supervisor", "admin"),
    "rules:read": ("specialist", "supervisor", "admin"),
    "audit:read": ("supervisor", "admin"),
    "metrics:read": ("specialist", "supervisor", "admin"),
    "user:manage": ("admin",),
    "system:recover": ("admin",),
    "agent:task": ("specialist", "supervisor", "admin"),
}



# 【审查修正 P2】登录失败计数的"IP 维度"表。
# 原来的失败计数只按 username：攻击者用 5 次错误密码就能把任意已知账号锁 300 秒
# （定向拒绝服务，成本极低），而真正的暴力破解者换账号即可绕过。
# 现在同时按 (username, ip) 与 ip 两个维度计数并退避。

_migrated: set[str] = set()
_lock = threading.Lock()

# 建表语句已收敛到 mcp_server/db.py 的单一列定义（同一份定义渲染 SQLite/PG 两套 DDL），
# 这里不再保留拷贝，避免两个后端的表结构漂移。
# 表结构查看： python -c "from mcp_server import db; print(db.ddl('compliance_user'))"


# ---------------------------------------------------------------- 基础设施

def _db_path() -> Path:
    # 【审查修正 P1】统一走 _dbpath.resolve_db_path()：与 queue_store / 能力服务
    # 收敛到同一个 sqlite 文件（框架内部约定全局只允许一个库文件）。
    from mcp_server._dbpath import resolve_db_path
    return resolve_db_path()


def _connect():
    """取连接。`DB_MODE=pg` 时落到 PostgreSQL（见 mcp_server/db.py）。

    边界说明：本项目自有表（账号/令牌/撤销/登录退避）随 `DB_MODE` 迁移；
    框架自有的 workflow_runs/checkpoints 仍由框架 DAO 写 sqlite（不改框架源码）。
    """
    from mcp_server import db as _db
    return _db.connect()


def ensure_schema() -> None:
    """幂等建表 + 补列。`DB_MODE=pg` 时落到 PostgreSQL。

    与 `queue_store.ensure_schema` 同样保留"每次补列"：升级新增列时老库必须能自动加上，
    否则会因为进程内的一次性缓存而永久拿不到新列（实测踩过 `no such column`）。
    """
    from mcp_server import db as _db
    for table in ("compliance_user", "compliance_token_revocation", "compliance_login_fail"):
        for stmt in _db._upgrade_columns(table):
            try:
                conn = _db.connect()
                try:
                    conn.execute(stmt)
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass
    _db.ensure_tables(["compliance_user", "compliance_token_revocation",
                       "compliance_login_fail"])


def purge_expired_revocations() -> int:
    """清理已过期的令牌撤销记录（登出只增不删会让表无界增长）。

    【审查修正 P2】原实现每次登出写一条、永不清理；本函数在启动与登录时各跑一次。
    【第二次审查修正】同时清理**登录失败计数表**中过期的行：
    该表按 (账号@IP) / (IP) 维度计行，只在"同名账号登录成功"时删除，
    于是"随机用户名刷失败"就能让它无界增长（每行虽小，但可被持续灌入）。
    """
    ensure_schema()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM compliance_token_revocation "
                           "WHERE expires_at IS NOT NULL AND expires_at < ?", (_now(),))
        removed = int(cur.rowcount or 0)
        # 失败计数行：锁定已过期且一段时间没再失败 → 删除（TTL 由 LOGIN_FAIL_TTL_SEC 控制）
        cutoff = (datetime.now() - timedelta(seconds=LOGIN_FAIL_TTL_SEC)).strftime(
            "%Y-%m-%d %H:%M:%S")
        cur2 = conn.execute(
            "DELETE FROM compliance_login_fail WHERE COALESCE(locked_until, updated_at) < ?",
            (cutoff,))
        removed += int(cur2.rowcount or 0)
        conn.commit()
        return removed
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_epoch(ts: str) -> int:
    """把 "YYYY-MM-DD HH:MM:SS" 转成 epoch 秒（解析失败按 0，等价于"不撤销"）。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime((ts or "").strip(), fmt).timestamp())
        except Exception:
            continue
    return 0


# ---------------------------------------------------------------- 密码

def _hash_password(password: str, salt: str | None = None,
                    iterations: int = PBKDF2_ITERATIONS) -> tuple[str, str, int]:
    salt = salt or base64.b16encode(secrets.token_bytes(16)).decode()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), iterations)
    return base64.b64encode(dk).decode(), salt, iterations


def _verify_password(password: str, salt: str, expected: str, iterations: int) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), int(iterations))
    return hmac.compare_digest(base64.b64encode(dk).decode(), expected)


# ---------------------------------------------------------------- 用户

# 用户名允许字符：字母数字与 . _ - @（与 SSO 建档同口径）。
# 【第二次审查修正·P2】原来只校验长度不校验字符集：
#   ① 用户 "bob" 与 "bob-2" 都是合法用户名，而 Agent 会话号按 `wb-<username>` 前缀归属判定，
#      "bob" 就能读写/清空 "bob-2" 的会话；
#   ② 用户名被原样拼进管理面板的 HTML/onclick，含引号即可注入标记。
# 现在两处一起收口：用户名限定字符集 + 会话号精确匹配。
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{2,64}$")


def create_user(username: str, password: str, role: str, display_name: str = "",
                actor: str = "system") -> dict:
    ensure_schema()
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        return {"ok": False, "errors": [
            "用户名需 2–64 个字符，且只能包含字母、数字与 . _ - @（避免会话归属歧义与界面注入）"]}
    if not password or len(password) < 6:
        return {"ok": False, "errors": ["密码至少 6 位"]}
    if role not in ROLES:
        return {"ok": False, "errors": [f"角色必须是 {ROLES} 之一"]}
    pw_hash, salt, iters = _hash_password(password)
    conn = _connect()
    try:
        if conn.execute("SELECT 1 FROM compliance_user WHERE username = ?", (username,)).fetchone():
            return {"ok": False, "errors": [f"用户名已存在：{username}"]}
        user_id = f"u_{secrets.token_hex(6)}"
        ts = _now()
        conn.execute(
            "INSERT INTO compliance_user (user_id, username, display_name, role, password_hash,"
            " salt, iterations, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'active', ?,?)",
            (user_id, username, display_name or username, role, pw_hash, salt, iters, ts, ts))
        conn.commit()
        record_auth_audit("user_created", actor=actor, target=username,
                          detail={"role": role, "display_name": display_name})
        return {"ok": True, "user_id": user_id, "username": username, "role": role}
    finally:
        conn.close()


def list_users() -> list[dict]:
    ensure_schema()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, username, display_name, role, status, created_at, last_login_at,"
            " fail_count, locked_until FROM compliance_user ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user(username: str) -> dict | None:
    ensure_schema()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM compliance_user WHERE username = ?",
                           (username,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_user_status(username: str, status: str, actor: str = "system") -> dict:
    ensure_schema()
    if status not in ("active", "disabled"):
        return {"ok": False, "errors": ["状态必须是 active 或 disabled"]}
    conn = _connect()
    try:
        cur = conn.execute("UPDATE compliance_user SET status = ?, updated_at = ? WHERE username = ?",
                           (status, _now(), username))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "errors": [f"用户不存在：{username}"]}
        record_auth_audit("user_status_changed", actor=actor, target=username,
                          detail={"status": status})
    finally:
        conn.close()
    if status != "active":
        # 停用即止损：撤销该用户全部令牌，避免"已停用但令牌仍可用到过期"
        revoke_user_tokens(username, reason=f"status_{status}")
    return {"ok": True, "username": username, "status": status}


def change_password(username: str, old_password: str, new_password: str,
                    actor: str = "", actor_role: str = "") -> dict:
    """修改密码。

    【审查修正 P2】原实现用"actor 非空"作为"管理员代改"的唯一判据，
    权限判断完全外置在调用点：任何调用方只要传一个非空 actor 就能改任意用户密码，
    而函数本身不做任何身份校验（当前唯一调用点的 is_admin 判断是**唯一**屏障，
    新增调用点就会静默越权）。现在：
      - 自助改密（actor 为空或 actor == username）：必须校验原密码；
      - 管理员代改：必须显式传 actor_role == "admin"；
      - 改密成功后**撤销该用户全部未过期令牌**（否则旧令牌在 TTL 内仍然可用，
        改密码这一"止损动作"形同虚设）。
    """
    ensure_schema()
    user = get_user(username)
    if not user:
        return {"ok": False, "errors": [f"用户不存在：{username}"]}
    impersonating = bool(actor) and actor != username
    if impersonating and actor_role != "admin":
        return {"ok": False, "errors": [
            "代他人修改密码需要管理员身份（actor_role=admin）；当前请求缺少该身份"]}
    if not impersonating:
        if not _verify_password(old_password or "", user["salt"], user["password_hash"],
                                user["iterations"]):
            return {"ok": False, "errors": ["原密码不正确"]}
    if not new_password or len(new_password) < 6:
        return {"ok": False, "errors": ["新密码至少 6 位"]}
    pw_hash, salt, iters = _hash_password(new_password)
    conn = _connect()
    try:
        conn.execute("UPDATE compliance_user SET password_hash=?, salt=?, iterations=?,"
                     " updated_at=? WHERE username=?", (pw_hash, salt, iters, _now(), username))
        conn.execute("UPDATE compliance_user SET fail_count=0, locked_until=NULL WHERE username=?",
                     (username,))
        conn.commit()
        record_auth_audit("password_changed", actor=actor or username, target=username,
                          detail={"by_admin": impersonating})
    finally:
        conn.close()
    # 改密即止损：撤销该用户此前的全部令牌（否则旧令牌在 TTL 内仍有效）
    revoke_user_tokens(username, reason="password_changed")
    return {"ok": True, "username": username}


def revoke_user_tokens(username: str, reason: str = "password_changed") -> int:
    """撤销某用户**全部**未过期令牌（无法枚举 jti，因此用"整账号撤销水位"实现）。

    实现方式：把 username 记入撤销表（token_id 用 `user:<username>` 前缀键），
    verify_token 除了查 jti，还会查该键的撤销时间，早于它的令牌一律拒绝。
    """
    ensure_schema()
    conn = _connect()
    try:
        conn.execute("INSERT OR REPLACE INTO compliance_token_revocation"
                     " (token_id, username, reason, revoked_at, expires_at) VALUES (?,?,?,?,NULL)",
                     (f"all:{username}", username, reason, _now()))
        conn.commit()
        return 1
    finally:
        conn.close()


def sso_login(username: str, role: str, display_name: str = "", email: str = "",
              subject: str = "", auto_provision: bool = True,
              ip: str = "", detail: dict | None = None) -> dict:
    """企业 SSO 登录后的本地落地：按需开户 + 同步角色 + 签发本项目 JWT。

    【SSO 对接要点】
      - 已停用账号**不得**通过 SSO 复活（否则 IdP 里还在、这边已开除的人能重新登录）
      - 已存在账号的角色以 IdP 为准同步（企业里"调岗"必须立刻生效），并留审计
      - 首次登录自动开户（`auto_provision=True`），密码字段写入不可用占位值：
        该账号只能走 SSO，不能用密码登录（避免出现"谁都不知道的密码"）
    """
    ensure_schema()
    ensure_schema_ext()
    username = (username or "").strip()
    if not username:
        return {"ok": False, "errors": ["SSO 身份缺少用户名"]}
    # 与本地建号同口径校验字符集：IdP 的声明可能含任意字符，
    # 过宽字符集会带来"会话归属歧义"与"界面注入"两个问题（见 USERNAME_RE 说明）。
    if not USERNAME_RE.match(username):
        return {"ok": False, "errors": [
            f"IdP 返回的用户名不符合规则（{username!r}）：需 2–64 个字符，仅限字母数字与 . _ - @。"
            "请把 OIDC_USERNAME_CLAIM 指向合规的声明字段"]}
    if role not in ROLES:
        return {"ok": False, "errors": [f"SSO 角色非法：{role}（应为 {list(ROLES)}）"]}

    user = get_user(username)
    created = False
    if user is None:
        if not auto_provision:
            record_auth_audit("sso_login_rejected", actor=username, target=username,
                              detail={**(detail or {}), "reason": "账号不存在且未开启自动开户",
                                      "ip": ip})
            return {"ok": False, "errors": ["账号不存在，请联系管理员开通（自动开户已关闭）"]}
        # 占位口令：随机且不可知，等价于"该账号不能用密码登录"
        placeholder = secrets.token_urlsafe(32)
        res = create_user(username, placeholder, role, display_name=display_name or username,
                          actor=f"sso:{subject or 'unknown'}")
        if not res.get("ok"):
            return {"ok": False, "errors": res.get("errors") or ["SSO 自动开户失败"]}
        created = True
        user = get_user(username)
        if user is None:
            return {"ok": False, "errors": ["SSO 自动开户后仍读不到账号"]}
        # 清掉口令并标记只能 SSO 登录
        _set_sso_only(username)
    else:
        if user["status"] != "active":
            record_auth_audit("sso_login_rejected", actor=username, target=username,
                              detail={**(detail or {}), "reason": "账号已停用", "ip": ip})
            return {"ok": False, "errors": ["账号已停用，请联系管理员"]}
        if user["role"] != role:
            conn = _connect()
            try:
                conn.execute("UPDATE compliance_user SET role=?, updated_at=? WHERE username=?",
                             (role, _now(), username))
                conn.commit()
            finally:
                conn.close()
            # 【第二次审查修正·P1】角色变化必须**立即失效已签发令牌**：
            # 权限来自令牌 payload（verify_token 只校验账号状态与撤销水位），
            # 不撤销的话"被移出 admin 组"的人还能用旧令牌以 admin 行权，最长 TOKEN_TTL_MIN。
            revoke_user_tokens(username, reason=f"sso_role_changed:{user['role']}->{role}")
            record_auth_audit("sso_role_synced", actor=username, target=username,
                              detail={**(detail or {}), "from": user["role"], "to": role,
                                      "ip": ip, "tokens_revoked": True})

    sso_only = _is_sso_only(username)
    token_res = issue_token_for(username, ip=ip, via="sso",
                               extra_detail={"sso_only": sso_only, "created": created,
                                             **(detail or {})})
    if not token_res.get("ok"):
        return token_res
    token_res.update({"created": created, "sso_only": sso_only, "role": role})
    return token_res


def _sso_only_path() -> Path:
    return _db_path().parent / "sso_only_users.json"


def _set_sso_only(username: str) -> None:
    """把账号标记为"仅 SSO 登录"（本地不存可用口令）。"""
    p = _sso_only_path()
    data: list[str] = []
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = []
    if username not in data:
        data.append(username)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_sso_only(username: str) -> bool:
    p = _sso_only_path()
    if not p.exists():
        return False
    try:
        return username in json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False


def issue_token_for(username: str, ip: str = "", via: str = "jwt",
                    extra_detail: dict | None = None) -> dict:
    """为已存在且启用的账号签发本项目 JWT（登录与 SSO 共用）。"""
    ensure_schema()
    s = settings()
    user = get_user(username)
    if user is None:
        return {"ok": False, "errors": [f"用户不存在：{username}"]}
    if user["status"] != "active":
        return {"ok": False, "errors": ["账号已停用，请联系管理员"]}
    import jwt as pyjwt
    now = datetime.now()
    exp = now + timedelta(minutes=s.token_ttl_min)
    token_id = secrets.token_hex(8)
    payload = {
        "sub": user["username"], "role": user["role"], "name": user["display_name"],
        "iss": s.jwt_issuer, "iat": int(now.timestamp()), "exp": int(exp.timestamp()),
        "jti": token_id, "via": via,
    }
    token = pyjwt.encode(payload, s.jwt_secret, algorithm="HS256")
    conn = _connect()
    try:
        conn.execute("UPDATE compliance_user SET last_login_at=? WHERE username=?",
                     (_now(), username))
        conn.commit()
    finally:
        conn.close()
    record_auth_audit("sso_login_ok" if via == "sso" else "login_success",
                      actor=username, target=username,
                      detail={"role": user["role"], "ip": ip, "jti": token_id,
                              "via": via, **(extra_detail or {})})
    return {"ok": True, "access_token": token, "token_type": "Bearer",
            "expires_at": exp.strftime("%Y-%m-%d %H:%M:%S"),
            "username": user["username"], "display_name": user["display_name"],
            "role": user["role"], "via": via}


def ensure_schema_ext() -> None:
    """SSO 相关的最小扩展（当前无新表，保留钩子以便后续加映射表）。"""
    ensure_schema()


def bootstrap_admin(username: str = "admin", password: str = "admin12345") -> dict:
    """首次启动时若没有任何用户，创建一个管理员（密码需尽快修改）。

    SSO 模式下同样会建这个本地管理员作为**应急/兜底账号**
    （IdP 不可用时仍能登入管理），但会给出更明确的提示。
    """
    ensure_schema()
    conn = _connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM compliance_user").fetchone()["c"]
    finally:
        conn.close()
    if n:
        return {"ok": True, "created": False, "existing_users": n}
    res = create_user(username, password, "admin", display_name="系统管理员", actor="bootstrap")
    res["created"] = True
    if settings().auth_mode == "sso":
        res["warning"] = ("已创建本地兜底管理员（SSO 模式下保留，用于 IdP 不可用时应急登录）；"
                          "请立即修改初始密码")
    else:
        res["warning"] = "请立即修改初始管理员密码"
    return res


# ---------------------------------------------------------------- 登录 / 令牌

def _record_failure(username: str, ip: str = "") -> None:
    """记录一次登录失败。

    【审查修正 P2】原来只按 username 计数：5 次错误密码即可把任意已知账号锁 300 秒
    （定向拒绝服务，攻击者无需知道密码）。而攻击者自己可以换账号绕过。
    现在同时维护两个维度：
      - 账号维度：仅作为"该账号近期有失败"的提示，不再单独触发锁定；
      - (账号, IP) 与 IP 维度：达到阈值即对**该来源**退避锁定。
    """
    conn = _connect()
    try:
        row = conn.execute("SELECT fail_count FROM compliance_user WHERE username=?",
                           (username,)).fetchone()
        if row:
            conn.execute("UPDATE compliance_user SET fail_count=fail_count+1 WHERE username=?",
                         (username,))
        for scope in (f"{username}@{ip}" if ip else "", f"ip:{ip}" if ip else ""):
            if not scope:
                continue
            cur = conn.execute("SELECT fail_count FROM compliance_login_fail WHERE scope=?",
                               (scope,)).fetchone()
            fails = int(cur["fail_count"] if cur else 0) + 1
            locked = None
            if fails >= MAX_LOGIN_FAILURES:
                locked = (datetime.now() + timedelta(seconds=LOCKOUT_SECONDS)).strftime(
                    "%Y-%m-%d %H:%M:%S")
                fails = 0
            conn.execute("INSERT OR REPLACE INTO compliance_login_fail"
                         " (scope, fail_count, locked_until, updated_at) VALUES (?,?,?,?)",
                         (scope, fails, locked, _now()))
        conn.commit()
    finally:
        conn.close()


def _failure_lockout(username: str, ip: str = "") -> str:
    """该来源是否处于失败退避锁定中；返回锁定到期时间（空表示未锁定）。"""
    scopes = [s for s in (f"{username}@{ip}" if ip else "", f"ip:{ip}" if ip else "") if s]
    if not scopes:
        return ""
    conn = _connect()
    try:
        for scope in scopes:
            row = conn.execute("SELECT locked_until FROM compliance_login_fail WHERE scope=?",
                               (scope,)).fetchone()
            until = (row["locked_until"] if row else "") or ""
            if until and until > _now():
                return until
        return ""
    finally:
        conn.close()


def _clear_failures(username: str, ip: str = "") -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE compliance_user SET fail_count=0, locked_until=NULL WHERE username=?",
                     (username,))
        for scope in (f"{username}@{ip}" if ip else "", f"ip:{ip}" if ip else ""):
            if scope:
                conn.execute("DELETE FROM compliance_login_fail WHERE scope=?", (scope,))
        conn.commit()
    finally:
        conn.close()


def login(username: str, password: str, ip: str = "") -> dict:
    """账号密码登录，返回访问令牌。

    【第二次审查修正·P1】`sso` 模式下也允许**本地账号**登录：
    `bootstrap_admin()` 特意在 sso 模式保留 `admin` 作为"IdP 故障时的应急登入"，
    但原实现只在 `AUTH_MODE=jwt` 时放行本地登录 —— 文档承诺的兜底入口实际不可用，
    IdP 一挂就完全没有管理入口（连改初始口令、停用账号都做不到）。
    现在：`jwt` / `sso` 均可本地登录；`demo` 模式仍明确提示改用令牌头。
    """
    ensure_schema()
    s = settings()
    if s.auth_mode not in ("jwt", "sso"):
        return {"ok": False, "errors": [
            f"当前 AUTH_MODE={s.auth_mode}，未启用账号登录（演示模式请使用令牌头）"]}
    # 来源退避锁定（防暴力破解；不会让第三方用"错误密码"锁定别人的账号）
    backoff_until = _failure_lockout(username, ip)
    if backoff_until:
        record_auth_audit("login_failed", actor=username, target=username,
                          detail={"reason": "来源退避锁定中", "ip": ip})
        return {"ok": False, "errors": [f"失败次数过多，请于 {backoff_until} 后重试"]}
    purge_expired_revocations()
    user = get_user(username)
    if not user:
        _record_failure(username, ip)
        record_auth_audit("login_failed", actor=username, target=username,
                          detail={"reason": "用户不存在", "ip": ip})
        return {"ok": False, "errors": ["用户名或密码不正确"]}
    if user["status"] != "active":
        record_auth_audit("login_failed", actor=username, target=username,
                          detail={"reason": "账号已停用", "ip": ip})
        return {"ok": False, "errors": ["账号已停用，请联系管理员"]}
    if user["locked_until"] and user["locked_until"] > _now():
        record_auth_audit("login_failed", actor=username, target=username,
                          detail={"reason": "账号锁定中", "ip": ip})
        return {"ok": False, "errors": [f"账号已锁定，请于 {user['locked_until']} 后重试"]}
    if not _verify_password(password or "", user["salt"], user["password_hash"],
                            user["iterations"]):
        _record_failure(username, ip)
        record_auth_audit("login_failed", actor=username, target=username,
                          detail={"reason": "密码错误", "ip": ip})
        return {"ok": False, "errors": ["用户名或密码不正确"]}

    import jwt as pyjwt
    # 【审查修正 P2】原实现用 datetime.utcnow() 生成 iat/exp，而所有库内时间戳
    # （revoked_at / locked_until / _now()）都是**本地时间**字符串 —— 两者混用会让
    # "账号级撤销水位"比较整体偏移时区差值（本机为 +8h），导致刚签发的令牌被判失效。
    # 统一用本地时间。
    now = datetime.now()
    exp = now + timedelta(minutes=s.token_ttl_min)
    token_id = secrets.token_hex(8)
    payload = {
        "sub": user["username"], "role": user["role"], "name": user["display_name"],
        "iss": s.jwt_issuer, "iat": int(now.timestamp()), "exp": int(exp.timestamp()),
        "jti": token_id,
    }
    token = pyjwt.encode(payload, s.jwt_secret, algorithm="HS256")
    _clear_failures(username, ip)
    conn = _connect()
    try:
        conn.execute("UPDATE compliance_user SET last_login_at=? WHERE username=?",
                     (_now(), username))
        conn.commit()
    finally:
        conn.close()
    record_auth_audit("login_success", actor=username, target=username,
                      detail={"role": user["role"], "ip": ip, "jti": token_id})
    return {"ok": True, "access_token": token, "token_type": "Bearer",
            "expires_at": exp.strftime("%Y-%m-%d %H:%M:%S"),
            "username": user["username"], "display_name": user["display_name"],
            "role": user["role"]}


def verify_token(token: str) -> dict:
    """校验访问令牌，返回 {ok, username, role, name} 或 {ok: False, errors}。"""
    ensure_schema()
    s = settings()
    if not token:
        return {"ok": False, "errors": ["缺少访问令牌"]}
    if s.auth_mode == "demo":
        # 演示模式仍允许固定令牌，便于本机脚本继续可用
        if token == s.demo_token:
            return {"ok": True, "username": "demo", "role": "admin",
                    "name": "演示账号（管理员）", "demo": True}
        return {"ok": False, "errors": ["演示模式令牌不正确"]}
    try:
        import jwt as pyjwt
        payload = pyjwt.decode(token, s.jwt_secret, algorithms=["HS256"],
                               issuer=s.jwt_issuer)
    except Exception as exc:
        return {"ok": False, "errors": [f"令牌无效或已过期：{type(exc).__name__}"]}
    conn = _connect()
    try:
        revoked = conn.execute("SELECT 1 FROM compliance_token_revocation WHERE token_id=?",
                               (payload.get("jti"),)).fetchone()
        if revoked:
            return {"ok": False, "errors": ["令牌已被登出/撤销"]}
        # 【审查修正 P2】账号级撤销水位：改密/停用后签发的令牌才有效。
        # 没有这一步，"管理员重置了被接管账号的密码"之后旧令牌仍能用满 TTL（最长 8 小时）。
        all_rev = conn.execute("SELECT revoked_at FROM compliance_token_revocation "
                               "WHERE token_id=?", (f"all:{payload.get('sub')}",)).fetchone()
        if all_rev and int(payload.get("iat") or 0) <= _to_epoch(all_rev["revoked_at"]):
            return {"ok": False, "errors": ["令牌已因账号安全事件（改密/停用/强制下线）失效，请重新登录"]}
        user = conn.execute("SELECT status FROM compliance_user WHERE username=?",
                            (payload.get("sub"),)).fetchone()
        if not user or user["status"] != "active":
            return {"ok": False, "errors": ["账号不存在或已停用"]}
    finally:
        conn.close()
    return {"ok": True, "username": payload["sub"], "role": payload.get("role", "specialist"),
            "name": payload.get("name", ""), "jti": payload.get("jti")}


def logout(token: str, reason: str = "user_logout") -> dict:
    """登出：把令牌 jti 记入撤销表（立即失效）。"""
    ensure_schema()
    info = verify_token(token)
    if not info.get("ok"):
        return {"ok": False, "errors": info.get("errors")}
    if info.get("demo"):
        return {"ok": True, "note": "演示令牌无需登出"}
    import jwt as pyjwt
    try:
        payload = pyjwt.decode(token, settings().jwt_secret, algorithms=["HS256"],
                               issuer=settings().jwt_issuer)
    except Exception:
        payload = {}
    conn = _connect()
    try:
        # 【审查修正 P2】原来无条件 `datetime.utcfromtimestamp(payload.get("exp", 0))`：
        # 令牌缺 exp 时记为 1970（记录永不清理），且 utcfromtimestamp 已废弃。
        exp = payload.get("exp")
        expires_at = ""
        if isinstance(exp, (int, float)) and exp > 0:
            expires_at = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT OR REPLACE INTO compliance_token_revocation"
                     " (token_id, username, reason, revoked_at, expires_at) VALUES (?,?,?,?,?)",
                     (payload.get("jti"), payload.get("sub", ""), reason, _now(), expires_at))
        conn.commit()
    finally:
        conn.close()
    record_auth_audit("logout", actor=payload.get("sub", ""), detail={"reason": reason})
    return {"ok": True, "username": payload.get("sub", "")}


def has_permission(role: str, action: str) -> bool:
    return role in PERMISSIONS.get(action, ())


# ---------------------------------------------------------------- 审计

def record_auth_audit(event: str, actor: str = "", target: str = "", detail: dict | None = None,
                      role: str = "") -> None:
    """认证/账号类事件写入与业务同一张审计表（run_id 留空表示系统级事件）。"""
    try:
        from mcp_server import queue_store
        queue_store.record_audit(run_id="", event=f"auth.{event}" if not event.startswith("auth.")
                                 else event, actor=actor, role=role, detail=detail or {})
    except Exception:
        pass


def auth_events(limit: int = 100) -> list[dict]:
    """查询认证类审计事件（供管理员查看登录/账号变更）。"""
    ensure_schema()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM compliance_audit WHERE event LIKE 'auth.%' "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
