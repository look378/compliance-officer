#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一数据访问层（SQLite / PostgreSQL 双后端）

背景（`docs/12-代码审查报告.md` §6 的"数据库"限制）：
    此前 `DB_MODE=pg` 只是配置项，没有任何实现 —— `docker-compose.yml` 却以 PG 交付，
    属于"配置宣称已就绪、实际没有"。本模块把**本项目自有表**（待办/审计/账号/令牌/撤销/
    登录退避）做成双后端，`DB_MODE=pg` 时真正落到 PostgreSQL。

**边界（必须如实说明）**：框架自有的 `workflow_runs` / `checkpoints` / `writes`
由底座 `dao/db.py` 直接以 sqlite3 读写，因此那部分仍在本机
SQLite 文件里。也就是说 `DB_MODE=pg` 迁移的是"业务数据"（人审闭环、审计链、账号体系），
工作流运行态仍随进程所属机器 —— 多实例部署需要框架侧支持，已记入就绪度清单。

设计要点：
    - 一处方言适配：`?` 占位符 → `%s`（PG），`INSERT OR REPLACE` → `ON CONFLICT ... DO UPDATE`
    - 一处 DDL 生成：同一套列定义，SQLite/PG 各自渲染（PG 用 TEXT 存时间串，与既有语义一致）
    - 连接管理：SQLite 用单文件连接；PG 用 `psycopg` 连接池（缺驱动时**明确报错**，不静默降级）
    - 所有 store 通过本模块取连接，因此"换库"不需要改业务代码
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------- 配置

def db_mode() -> str:
    return (os.environ.get("DB_MODE") or "sqlite").strip().lower()


def database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def _sqlite_path() -> Path:
    from mcp_server._dbpath import resolve_db_path
    return resolve_db_path()


class DatabaseUnavailable(RuntimeError):
    """数据库不可用（缺驱动/缺 URL/连不上）——必须让调用方看见，不得静默降级。"""


# ---------------------------------------------------------------- 方言

_PLACEHOLDER = re.compile(r"\?")


def _to_pg_sql(sql: str) -> str:
    """把 SQLite 风格 SQL 转成 PostgreSQL 可执行的形式。

    只做**保守**替换（本项目自有 SQL 均在其中）：
      - 占位符 `?` → `%s`
      - `INSERT OR REPLACE INTO t (...) VALUES (...)` → `INSERT INTO ... ON CONFLICT (主键) DO UPDATE`
      - `INSERT OR IGNORE INTO t` → `INSERT INTO t ... ON CONFLICT DO NOTHING`
    不做通用 SQL 解析：一旦出现本函数不认识的写法，**直接用原文**（宁可报错也不要改错）。
    """
    out = sql
    m = re.match(r"\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                 out, re.I)
    if m:
        table = m.group(1)
        cols = _insert_columns(out)
        conflict = _conflict_target(table, cols)
        out = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", out, count=1, flags=re.I)
        out = out.rstrip().rstrip(";")
        if conflict:
            updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != conflict)
            out += f" ON CONFLICT ({conflict}) DO UPDATE SET {updates}" if updates \
                else f" ON CONFLICT ({conflict}) DO NOTHING"
        else:
            out += " ON CONFLICT DO NOTHING"
    elif re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO", out, re.I):
        out = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", out, count=1, flags=re.I)
        out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return _PLACEHOLDER.sub("%s", out)


def _insert_columns(sql: str) -> list[str]:
    m = re.search(r"\(([^)]*)\)\s*VALUES", sql, re.I | re.S)
    if not m:
        return []
    return [c.strip().strip('"') for c in m.group(1).split(",") if c.strip()]


# 各表的主键/唯一键（用于 ON CONFLICT 目标；PG 要求明确给出冲突列）
_CONFLICT_KEYS = {
    "compliance_todo": "todo_id",
    "compliance_audit": "audit_id",
    "compliance_user": "user_id",
    "compliance_token_revocation": "token_id",
    "compliance_login_fail": "scope",
}


def _conflict_target(table: str, cols: list[str]) -> str:
    key = _CONFLICT_KEYS.get(table, "")
    if key and key in cols:
        return key
    # 回退：若 SQL 里显式写了某列的唯一键，交由调用方保证
    return ""


# ---------------------------------------------------------------- 连接

_local = threading.local()


def _sqlite_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_sqlite_path()), timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _pg_driver():
    """按可用性挑一个 PG 驱动。

    优先 psycopg（性能与功能最全），其次 **pg8000**（纯 Python，无需编译，
    受限环境/内网离线时最容易装上）。两者都没有时**明确报错**，不静默降级。
    """
    try:
        import psycopg  # psycopg3
        return "psycopg3", psycopg
    except Exception:
        pass
    try:
        import psycopg2  # psycopg2
        return "psycopg2", psycopg2
    except Exception:
        pass
    try:
        import pg8000  # 纯 Python
        return "pg8000", pg8000
    except Exception:
        pass
    raise DatabaseUnavailable(
        "DB_MODE=pg 需要 PostgreSQL 驱动：任选其一安装后重启 —— "
        "`pip install pg8000`（纯 Python，最省事）或 `pip install \"psycopg[binary]\"`")


def _parse_pg_url(url: str) -> dict:
    """把 postgresql:// URL 解析成驱动参数（pg8000 不吃 URL）。"""
    from urllib.parse import unquote, urlparse
    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise DatabaseUnavailable(f"DATABASE_URL 必须是 postgresql:// 开头（实际 {u.scheme!r}）")
    return {"host": u.hostname or "127.0.0.1",
            "port": int(u.port or 5432),
            "user": unquote(u.username or ""),
            "password": unquote(u.password or ""),
            "database": (u.path or "/").lstrip("/") or "postgres"}


def _pg_is_closed(conn) -> bool:
    try:
        if getattr(conn, "closed", False):
            return True
        inner = getattr(conn, "_usock", None)      # pg8000：socket 关掉就是不可用
        return bool(inner is not None and getattr(inner, "closed", False))
    except Exception:
        return False


# 连接级故障的识别：驱动异常类名 + 常见底层错误串（Windows WSA 错误码也列上）
_CONN_ERR_TYPES = ("InterfaceError", "OperationalError", "ConnectionError",
                   "ConnectionException", "ProtocolError")
_CONN_ERR_MARKERS = ("network error", "connection", "closed", "broken pipe", "eof",
                     "10038", "10053", "10054", "10057", "10060", "terminating connection",
                     "server closed", "ssl")


def _is_conn_error(exc: BaseException) -> bool:
    """是否属于"连接本身坏了"（而不是 SQL 写错 / 约束冲突 / 类型不对）。

    宁可误判为连接故障：误判的代价只是多连一次，而漏判的代价是**一直复用死连接**。
    """
    if type(exc).__name__ in _CONN_ERR_TYPES:
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _CONN_ERR_MARKERS)


def _pg_probe(conn) -> bool:
    """连接是否仍然可用（真发一条最轻的语句）。

    【第二次审查修正·真实 PG 实测发现】原先只判断 "驱动认为连接已关闭"：
    但**被服务端断开的连接在驱动看来仍是 open**（PG 重启、连接被网关回收、
    我们主动 pg_terminate_backend 都会这样）。那种连接的 socket 已经死了，
    于是之后每一次调用都复用这条坏连接并持续报
    `[WinError 10038] 在一个非套接字上尝试了一个操作` —— 故障永远不会自愈。

    现在改为按时间窗做**真实探活**（默认每 `PG_PROBE_INTERVAL_S` 秒一次）：
    探活失败就丢弃连接重连，故障自愈只需一次调用。
    """
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        return True
    except Exception:
        return False


def _probe_interval() -> float:
    try:
        return float(os.environ.get("PG_PROBE_INTERVAL_S", "30") or 30)
    except ValueError:
        return 30.0


def _pg_connection():
    """取一个 PG 连接（进程内按线程缓存 + 健康探活；不做连接池以保持依赖最小）。"""
    url = database_url()
    if not url:
        raise DatabaseUnavailable("DB_MODE=pg 必须设置 DATABASE_URL")
    cached = getattr(_local, "pg_conn", None)
    kind, driver = _pg_driver()
    if cached is not None:
        age = time.time() - float(getattr(_local, "pg_checked_at", 0))
        if not _pg_is_closed(cached) and age < _probe_interval():
            return _PgConn(cached, kind)
        if not _pg_is_closed(cached) and _pg_probe(cached):
            _local.pg_checked_at = time.time()
            return _PgConn(cached, kind)
        # 不可用：丢弃并从缓存里移除，下面重连
        try:
            cached.close()
        except Exception:
            pass
        _local.pg_conn = None
    try:
        if kind in ("psycopg3", "psycopg2"):
            conn = driver.connect(url, autocommit=False) if kind == "psycopg3" \
                else driver.connect(url)
        else:                                   # pg8000：用解析后的参数
            conn = driver.connect(**_parse_pg_url(url))
    except Exception as exc:
        raise DatabaseUnavailable(f"无法连接 PostgreSQL：{type(exc).__name__}: {exc}") from exc
    _local.pg_conn = conn
    _local.pg_checked_at = time.time()
    return _PgConn(conn, kind)


class _PgConn:
    """把 PG 连接包装成与 sqlite3.Connection 相同的调用面：
    `execute(sql, params)` 支持 `?` 占位符、行按列名/下标取值。"""

    def __init__(self, conn, kind: str):
        self._conn = conn
        self._kind = kind

    def _rollback_quietly(self) -> None:
        try:
            self._conn.rollback()
        except Exception:
            pass

    def _invalidate(self, exc: BaseException) -> None:
        """连接级故障：剔除本线程缓存的连接，让**下一次**调用重连（故障自愈）。

        【第二次审查修正·真机实测发现】`_pg_connection` 为了省探活开销，在
        `PG_PROBE_INTERVAL_S`（默认 30s）窗口内**直接复用缓存连接、完全不探活**。
        于是连接被服务端断开之后，窗口内的每一次调用都拿到同一条死连接并全部失败
        （实测击杀后端后连续 3 次都是 `InterfaceError: network error`），
        "自愈"要干等到窗口过期为止 —— 上一版把探活加上、却没处理这条路径。

        这里只**剔除 + 关闭**，不自动重试：网络错误下语句是否已在服务端生效无法确定，
        盲目重试可能把非幂等写入做两次；剔除之后调用方按正常流程重试即可成功。
        """
        if not _is_conn_error(exc):
            return
        try:
            self._conn.close()
        except Exception:
            pass
        if getattr(_local, "pg_conn", None) is self._conn:
            _local.pg_conn = None
        _local.pg_checked_at = 0.0

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self._conn.cursor()
        try:
            cur.execute(_to_pg_sql(sql), tuple(params))
        except Exception as exc:
            # 【第二次审查修正】语句失败会让事务进入 aborted 状态，此后**同一连接上的
            # 所有后续语句都会报 "current transaction is aborted"**。业务代码
            # （queue_store/auth）里大量使用 `try: ... except Exception: pass` 的容错写法，
            # 如果不回滚，一次失败就会把整条链路带崩。这里统一在同一连接上回滚，
            # 让"单条失败不阻塞批次"的语义在 PG 后端同样成立。
            self._rollback_quietly()
            self._invalidate(exc)
            raise
        return _PgCursor(cur)

    def executemany(self, sql: str, seq):
        cur = self._conn.cursor()
        try:
            cur.executemany(_to_pg_sql(sql), [tuple(p) for p in seq])
        except Exception as exc:
            self._rollback_quietly()
            self._invalidate(exc)
            raise
        return _PgCursor(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        # 线程缓存复用连接，这里的 close 是空操作（避免业务代码把连接关掉）
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount
        # 把列名固定下来：pg8000 的 cursor.description 在 fetch 之后可能被清空
        self._cols = [d[0] for d in (cur.description or [])]

    def fetchone(self):
        row = self._cur.fetchone()
        return _DictRow(row, self._cols) if row is not None else None

    def fetchall(self):
        return [_DictRow(r, self._cols) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _DictRow(dict):
    """支持 `row["col"]` 与 `row[0]` 两种取值（与 sqlite3.Row 对齐）。"""

    def __init__(self, values, cols):
        super().__init__(zip(cols or [], values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def connect():
    """取连接（按 DB_MODE 分派）。调用方负责 commit/close。"""
    if db_mode() == "pg":
        return _pg_connection()
    return _sqlite_connect()


# ---------------------------------------------------------------- DDL

def _cols_sql(cols: list[tuple[str, str]], pk: str, sqlite: bool) -> str:
    body = []
    for name, spec in cols:
        body.append(f"    {name} {spec}")
    # SQLite 的 `INTEGER PRIMARY KEY AUTOINCREMENT` 必须是该表唯一的主键声明，
    # 此时不能再追加表级 PRIMARY KEY（实测报 "more than one primary key"）。
    inline_pk = any(n == pk and "PRIMARY KEY" in s.upper() for n, s in cols)
    if not inline_pk:
        body.append(f"    PRIMARY KEY ({pk})")
    return ",\n".join(body)


# 本项目自有表：列定义一份，两套 DDL 由同一份定义渲染
#
# 【真实 PG 实测修正】`seq` 列：SQLite 有隐式 `rowid` 可稳定排序**同秒写入**的行，
#   PostgreSQL 没有（实测报 `column "rowid" does not exist`），因此显式加自增列：
#     PostgreSQL = BIGSERIAL（服务端自增）
#     SQLite     = INTEGER PRIMARY KEY AUTOINCREMENT（SQLite 要求自增列必须是该表主键）
#   ⚠ 代价（必须知情）：SQLite 下 `seq` 成了该表主键，业务唯一键（audit_id/todo_id）
#     由 UNIQUE 约束继续保证"同一 ID 不重复"。业务代码一律按业务 ID 查询/更新，
#     不依赖主键选择，因此行为不变（pytest 全绿即为证）。
_SEQ_DECL = {"pg": "BIGSERIAL", "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT"}


def _table_defs(dialect: str = "") -> dict[str, tuple[list[tuple[str, str]], str]]:
    """返回表定义。`dialect` 显式给出 'sqlite'|'pg'（默认取当前 DB_MODE），
    以便在任意后端下都能渲染出另一套方言的 DDL（自检与测试需要）。"""
    mode = dialect or db_mode()
    seq = _SEQ_DECL.get(mode, _SEQ_DECL["sqlite"])
    return {
        "compliance_todo": ([
            ("todo_id", "TEXT NOT NULL UNIQUE"), ("run_id", "TEXT NOT NULL DEFAULT ''"),
            ("workflow_id", "TEXT NOT NULL DEFAULT ''"), ("node_name", "TEXT NOT NULL DEFAULT ''"),
            ("role_required", "TEXT NOT NULL"), ("title", "TEXT NOT NULL DEFAULT ''"),
            ("summary", "TEXT NOT NULL DEFAULT ''"), ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("status", "TEXT NOT NULL DEFAULT 'open'"), ("claimed_by", "TEXT"),
            ("claimed_at", "TEXT"), ("decided_by", "TEXT"), ("decided_at", "TEXT"),
            ("decision", "TEXT"), ("idempotency_key", "TEXT UNIQUE"),
            ("trace_id", "TEXT DEFAULT ''"),
            ("created_at", "TEXT NOT NULL"), ("due_at", "TEXT"), ("updated_at", "TEXT NOT NULL"),
            ("seq", seq),
        ], "seq"),
        "compliance_audit": ([
            ("audit_id", "TEXT NOT NULL UNIQUE"), ("run_id", "TEXT NOT NULL DEFAULT ''"),
            ("todo_id", "TEXT"), ("event", "TEXT NOT NULL"), ("actor", "TEXT NOT NULL DEFAULT ''"),
            ("role", "TEXT NOT NULL DEFAULT ''"), ("detail_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("trace_id", "TEXT NOT NULL DEFAULT ''"), ("created_at", "TEXT NOT NULL"),
            ("seq", seq),
        ], "seq"),
        "compliance_user": ([
            ("user_id", "TEXT NOT NULL"), ("username", "TEXT NOT NULL UNIQUE"),
            ("display_name", "TEXT NOT NULL DEFAULT ''"), ("role", "TEXT NOT NULL"),
            ("password_hash", "TEXT NOT NULL"), ("salt", "TEXT NOT NULL"),
            ("iterations", "INTEGER NOT NULL"), ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("created_at", "TEXT NOT NULL"), ("updated_at", "TEXT NOT NULL"),
            ("last_login_at", "TEXT"), ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
            ("locked_until", "TEXT"),
        ], "user_id"),
        "compliance_token_revocation": ([
            ("token_id", "TEXT NOT NULL"), ("username", "TEXT NOT NULL"),
            ("reason", "TEXT NOT NULL DEFAULT ''"), ("revoked_at", "TEXT NOT NULL"),
            ("expires_at", "TEXT"),
        ], "token_id"),
        "compliance_login_fail": ([
            ("scope", "TEXT NOT NULL"), ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
            ("locked_until", "TEXT"), ("updated_at", "TEXT NOT NULL"),
        ], "scope"),
    }


_TABLES: dict[str, tuple[list[tuple[str, str]], str]] = {}

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_todo_status_role ON compliance_todo(status, role_required)",
    "CREATE INDEX IF NOT EXISTS idx_todo_run ON compliance_todo(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_run ON compliance_audit(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_status ON compliance_user(status)",
]


def ddl(table: str, sqlite: bool | None = None) -> str:
    """生成建表 SQL（两套方言由同一份列定义渲染）。"""
    is_sqlite = db_mode() != "pg" if sqlite is None else sqlite
    cols, pk = _table_defs("sqlite" if is_sqlite else "pg")[table]
    body = _cols_sql(cols, pk, is_sqlite)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n{body}\n)"


def _column_exists(conn, table: str, column: str) -> bool:
    """列是否存在（两端各用一句元数据查询）。"""
    try:
        if db_mode() == "pg":
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?",
                (table, column)).fetchone()
            return bool(row)
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any((r["name"] if not isinstance(r, tuple) else r[1]) == column for r in rows)
    except Exception:
        return False


def _table_exists(conn, table: str) -> bool:
    try:
        if db_mode() == "pg":
            row = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?",
                               (table,)).fetchone()
            return bool(row)
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone()
        return bool(row)
    except Exception:
        return False


def upgrade_columns(table: str) -> list[str]:
    """返回"需要补的列"对应的 ALTER 语句（**已存在则返回空**）。

    【实测踩过】不能依赖 `ADD COLUMN IF NOT EXISTS`：
      - SQLite **不支持**该语法（整个语句报语法错误 → 补列静默失败）
      - 老版本 PostgreSQL 同样不支持
    因此这里先查元数据判断列是否存在，再决定是否 ALTER，两个后端都稳。
    """
    seq = "BIGSERIAL" if db_mode() == "pg" else "INTEGER"
    wanted = {"compliance_todo": [("seq", seq)], "compliance_audit": [("seq", seq)]}
    out: list[str] = []
    if table not in wanted:
        return out
    conn = connect()
    try:
        if not _table_exists(conn, table):
            return out                     # 表还不存在：交给 CREATE TABLE 一步到位
        for col, decl in wanted[table]:
            if not _column_exists(conn, table, col):
                out.append(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    finally:
        conn.close()
    return out


# 兼容旧名字（此前叫 _upgrade_columns，语义相同但实现有缺陷）
_upgrade_columns = upgrade_columns


def ensure_tables(tables: list[str] | None = None) -> None:
    """幂等建表 + 补列 + 建索引（启动/首次使用调用）。"""
    defs = _table_defs()
    conn = connect()
    try:
        for name in (tables or list(defs)):
            conn.execute(ddl(name))
            for stmt in upgrade_columns(name):
                try:
                    conn.execute(stmt)
                    conn.commit()
                except Exception:
                    pass          # 并发下可能已被别的进程补上
        for idx in _INDEXES:
            try:
                conn.execute(idx)
            except Exception:
                pass          # 索引失败不影响主流程（表已建）
        conn.commit()
    finally:
        conn.close()


def describe() -> dict:
    """当前数据后端（供诊断端点展示）。"""
    mode = db_mode()
    info: dict = {"db_mode": mode,
                  "sqlite_path": str(_sqlite_path()) if mode != "pg" else None,
                  "database_url_set": bool(database_url())}
    if mode == "pg":
        try:
            kind, _ = _pg_driver()
            info["driver"] = kind
            info["driver_ok"] = True
        except DatabaseUnavailable as exc:
            info["driver"] = None
            info["driver_ok"] = False
            info["error"] = str(exc)
    else:
        info["driver"] = "sqlite3"
        info["driver_ok"] = True
    info["migrated_tables"] = sorted(_table_defs())
    info["note"] = ("框架自有的 workflow_runs/checkpoints 由框架 DAO 直接写 sqlite，"
                    "不在本迁移范围内（见 docs/16-PostgreSQL部署.md）")
    return info
