#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据访问层单元测试（SQLite / PostgreSQL 双后端）

运行：
    .runtime\\venv\\Scripts\\python.exe -m pytest tests -q

说明（如实交代验证边界）：
    - 本机**没有** PostgreSQL 实例，因此这里不假装跑通了 PG 连接；
      真正被严格验证的是：① SQLite 参考后端行为不变；② PG 方言转换正确；
      ③ PG 缺驱动/缺 URL 时**明确报错**而不是静默退回 SQLite（这是最危险的失败模式）。
    - 真库上的端到端验证步骤写在 `docs/16-PostgreSQL部署.md`，需要贵司/CI 环境执行。
"""

import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, os.environ.get("LEMONCLAW_HOME", str(PROJECT_ROOT.parent / "lemonclaw-main")))

from mcp_server import db as D  # noqa: E402


@pytest.fixture()
def workdir():
    base = PROJECT_ROOT / ".runtime" / "testtmp"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"d{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def sqlite_backend(workdir, monkeypatch):
    """把库切到临时文件，并确保走 SQLite 参考后端。"""
    monkeypatch.setenv("LEMONCLAW_DB_PATH", str(workdir / "test.db"))
    monkeypatch.delenv("DB_MODE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from mcp_server import queue_store
    queue_store._migrated.clear()
    D._local.__dict__.pop("pg_conn", None)
    yield
    queue_store._migrated.clear()


# ============================================================ 后端选择

class TestBackendSelection:
    def test_default_is_sqlite(self, monkeypatch):
        monkeypatch.delenv("DB_MODE", raising=False)
        assert D.db_mode() == "sqlite"
        info = D.describe()
        assert info["db_mode"] == "sqlite" and info["driver"] == "sqlite3"
        assert info["driver_ok"] is True

    def test_sqlite_connection_returns_dict_like_rows(self, sqlite_backend):
        conn = D.connect()
        try:
            conn.execute("CREATE TABLE t (a TEXT, b INTEGER)")
            conn.execute("INSERT INTO t (a, b) VALUES (?, ?)", ("x", 1))
            conn.commit()
            row = conn.execute("SELECT a, b FROM t").fetchone()
            assert row["a"] == "x" and row["b"] == 1
            assert row[0] == "x" and row[1] == 1        # 位置取值也要支持
        finally:
            conn.close()

    def test_pg_without_url_is_explicit_error(self, monkeypatch):
        """绝不能静默退回 SQLite —— 那会让"以为上了 PG"变成最危险的误解。"""
        monkeypatch.setenv("DB_MODE", "pg")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(D.DatabaseUnavailable) as ei:
            D.connect()
        assert "DATABASE_URL" in str(ei.value)

    def test_pg_missing_driver_is_explicit_error(self):
        """驱动缺失时必须明确报"装哪个包"，而不是崩得莫名其妙或悄悄退回 SQLite。

        本机装了 pg8000 时这条走"驱动可用"分支（断言优先级链正确）。
        """
        try:
            kind, _ = D._pg_driver()
        except D.DatabaseUnavailable as exc:
            assert "pg8000" in str(exc) and "psycopg" in str(exc), str(exc)
            return
        assert kind in ("psycopg3", "psycopg2", "pg8000"), kind

    def test_describe_reports_pg_problems(self, monkeypatch):
        monkeypatch.setenv("DB_MODE", "pg")
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/db")
        info = D.describe()
        assert info["db_mode"] == "pg" and info["database_url_set"] is True
        assert "migrated_tables" in info and "workflow_runs" in info["note"]


# ============================================================ 方言转换

class TestDialect:
    def test_placeholder_conversion(self):
        sql = "SELECT * FROM compliance_todo WHERE run_id=? AND status=?"
        assert D._to_pg_sql(sql) == "SELECT * FROM compliance_todo WHERE run_id=%s AND status=%s"

    def test_insert_or_replace_becomes_upsert(self):
        sql = ("INSERT OR REPLACE INTO compliance_login_fail "
               "(scope, fail_count, locked_until, updated_at) VALUES (?,?,?,?)")
        out = D._to_pg_sql(sql)
        assert out.startswith("INSERT INTO compliance_login_fail")
        assert "ON CONFLICT (scope) DO UPDATE SET" in out
        assert "fail_count=EXCLUDED.fail_count" in out
        assert "?" not in out and "%s" in out

    def test_insert_or_ignore_becomes_do_nothing(self):
        sql = "INSERT OR IGNORE INTO compliance_token_revocation (token_id) VALUES (?)"
        out = D._to_pg_sql(sql)
        assert out.startswith("INSERT INTO compliance_token_revocation")
        assert out.endswith("ON CONFLICT DO NOTHING")
        assert "%s" in out

    def test_unknown_sql_is_left_alone(self):
        """不认识的写法保持原样：宁可报错也不要"猜着改"把语义改坏。"""
        sql = "UPDATE compliance_todo SET status='claimed' WHERE todo_id=? AND status='open'"
        assert D._to_pg_sql(sql) == sql.replace("?", "%s")

    def test_ddl_generated_for_both_dialects(self):
        """两套方言由同一份列定义渲染。

        【真实 PG 实测修正】主键不再是业务 ID，而是自增 `seq`：
        SQLite 有隐式 rowid 可稳定排序同秒写入的行，PostgreSQL 没有
        （实测报 `column "rowid" does not exist`），因此显式加自增列。
        业务 ID（audit_id / todo_id）仍由 UNIQUE 约束保证唯一。
        """
        sqlite_ddl = D.ddl("compliance_audit", sqlite=True)
        pg_ddl = D.ddl("compliance_audit", sqlite=False)
        assert "CREATE TABLE IF NOT EXISTS compliance_audit" in sqlite_ddl
        assert "audit_id TEXT NOT NULL UNIQUE" in sqlite_ddl
        assert "audit_id TEXT NOT NULL UNIQUE" in pg_ddl
        # 自增列：SQLite 用 INTEGER PRIMARY KEY AUTOINCREMENT，PG 用 BIGSERIAL
        assert "INTEGER PRIMARY KEY AUTOINCREMENT" in sqlite_ddl
        assert "BIGSERIAL" in pg_ddl
        assert "detail_json" in pg_ddl and "created_at" in pg_ddl

    def test_all_project_tables_have_ddl(self):
        for table in ("compliance_todo", "compliance_audit", "compliance_user",
                      "compliance_token_revocation", "compliance_login_fail"):
            sql = D.ddl(table)
            assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
            assert "PRIMARY KEY" in sql

    def test_conflict_target_known_for_tables(self):
        assert D._conflict_target("compliance_todo", ["todo_id", "run_id"]) == "todo_id"
        assert D._conflict_target("compliance_audit", ["audit_id"]) == "audit_id"
        assert D._conflict_target("unknown_table", ["x"]) == ""


# ============================================================ SQLite 参考后端行为

class TestSqliteReferenceBehaviour:
    """这些行为是"换库后必须一模一样"的基准（PG 分支复用同一套 SQL 与语义）。"""

    def test_ensure_tables_is_idempotent(self, sqlite_backend):
        D.ensure_tables()
        D.ensure_tables()
        conn = D.connect()
        try:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert set(D._TABLES).issubset(names)

    def test_todo_idempotency_and_atomic_claim(self, sqlite_backend):
        from mcp_server import queue_store as Q
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        first = Q.create_todo(run_id=run_id, node_name="review", role_required="specialist",
                              title="t", idempotency_key=f"{run_id}:k")
        again = Q.create_todo(run_id=run_id, node_name="review", role_required="specialist",
                              title="t", idempotency_key=f"{run_id}:k")
        assert first["created"] is True and again["created"] is False
        tid = first["todo"]["todo_id"]
        c1 = Q.claim_todo(tid, "alice")
        c2 = Q.claim_todo(tid, "bob")
        assert c1["ok"] is True and c2["ok"] is False and c2["holder"] == "alice"
        # 非领取者不得办结；领取者可以
        assert Q.complete_todo(tid, "bob", "confirm")["ok"] is False
        assert Q.complete_todo(tid, "alice", "confirm", "同意")["ok"] is True
        assert Q.complete_todo(tid, "alice", "confirm")["ok"] is False

    def test_audit_append_and_cursor(self, sqlite_backend):
        from mcp_server import queue_store as Q
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        for i in range(5):
            Q.record_audit(run_id, f"ev{i}", actor="a")
        events = Q.list_audit(run_id=run_id, limit=10)
        assert [e["event"] for e in events] == [f"ev{i}" for i in range(5)]
        assert Q.count_audit(run_id) == 5
        cursor = events[1]["audit_id"]
        rest = Q.list_audit_since(run_id, cursor=cursor)
        assert [e["event"] for e in rest] == ["ev2", "ev3", "ev4"]
        # 游标不重不漏
        assert len({e["audit_id"] for e in events}) == 5

    def test_metrics_shape(self, sqlite_backend):
        from mcp_server import queue_store as Q
        m = Q.metrics()
        for key in ("todo_total", "todo_done", "todo_open", "todo_claimed",
                    "human_todo_completion_rate", "human_todo_per_finished_run"):
            assert key in m

    def test_user_table_roundtrip(self, sqlite_backend, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "jwt")
        monkeypatch.setenv("JWT_SECRET", "unit-test-secret-key-0123456789-abcdef")
        from service import auth as A
        A._migrated.clear()
        res = A.create_user("pg_probe", "Passw0rd@123", "specialist", actor="test")
        assert res["ok"], res
        u = A.get_user("pg_probe")
        assert u and u["role"] == "specialist"
        assert A._verify_password("Passw0rd@123", u["salt"], u["password_hash"], u["iterations"])
        A._migrated.clear()

    def test_single_sqlite_file_rule(self, workdir, monkeypatch):
        """框架约定：全局只能有一个 sqlite 文件。本项目的解析必须是"绝对且与 cwd 无关"。

        【第二次审查修正】原用例用了 sqlite_backend 夹具，而它已设置 LEMONCLAW_DB_PATH ——
        resolve_db_path 会直接返回该绝对路径，三次比较恒等，等于没测到"与 cwd 无关"。
        现在显式清掉该环境变量再比较。
        """
        monkeypatch.delenv("LEMONCLAW_DB_PATH", raising=False)
        from mcp_server._dbpath import resolve_db_path
        cwd = os.getcwd()
        try:
            p1 = resolve_db_path()
            os.chdir(PROJECT_ROOT / "gateway")
            p2 = resolve_db_path()
            os.chdir(PROJECT_ROOT)
            p3 = resolve_db_path()
        finally:
            os.chdir(cwd)
        assert p1 == p2 == p3, f"库路径随 cwd 变化：{p1} / {p2} / {p3}"
