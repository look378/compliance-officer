#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
运行态数据访问辅助（本项目自有代码）

D0 实测结论（见 docs/evidence/d0-g6-concurrency.json）：
    框架的 WorkflowManager.resume_run 是「读状态 -> 判断 paused -> 提交后台任务」，
    读取与提交之间没有原子抢占；4 个并发请求会被**全部接受**，随后同一 thread_id
    被并发跑图。因此服务端的恢复入口必须自己做互斥。

本模块提供最小可用的互斥原语：
    claim_run(run_id, owner)  原子抢占（单条 UPDATE ... WHERE status='paused' AND claimed_by IS NULL）
    release_run(run_id)       释放占用
    list_pending(...)         待办/暂停清单（供统一待办队列与启动扫描复用）

为什么用 claimed_by 而不是改 status：
    status 是框架状态机的语义（running/paused/completed/...），复用它会与框架的
    状态判断互相干扰（D0 实测：把 status 改成 claiming 再改回 paused 会立刻失去互斥）。
    另加一列既不侵入框架语义，也不影响框架的既有判断。
"""

import os
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_migrated: set[str] = set()


def _db_path() -> Path:
    # 【审查修正 P1】统一走 _dbpath.resolve_db_path()（原因见该模块 docstring）：
    # 能力服务进程原先因缺框架路径而无法 `import dao`，且 cwd 不同会导致多库并存。
    from mcp_server._dbpath import resolve_db_path
    return resolve_db_path()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _table_ready(conn: sqlite3.Connection) -> bool:
    """框架的 workflow_runs 表是否已存在（能力服务可能先于框架启动）。"""
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_runs'").fetchone())


def ensure_claim_columns() -> None:
    """幂等迁移：给 workflow_runs 增加 claimed_by / claimed_at 两列。"""
    key = str(_db_path())
    with _lock:
        if key in _migrated:
            return
        conn = _connect()
        try:
            # 【审查修正 P2】框架尚未建表时（全新库、或能力服务先于框架启动）
            # ALTER TABLE 会抛 "no such table" —— 这里先探测，避免启动期自检直接崩。
            #
            # 【实跑容器发现·P0】但这个分支**不能**标记 `_migrated`：
            #   框架是在**第一次跑图时**才建 `workflow_runs` 的。全新系统上启动钩子先调到这里，
            #   表还不存在；若此刻永久标记"已迁移"，等框架后来把表建好，
            #   claimed_by / claimed_at 两列就永远不会补上 —— 首次审批直接
            #   `sqlite3.OperationalError: no such column: claimed_by`（已在容器里复现，
            #   本机全新库同样会中招）。不标记即可：下一次调用会重新检查并补列（幂等）。
            if not _table_ready(conn):
                return
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_runs)")}
            if "claimed_by" not in cols:
                conn.execute("ALTER TABLE workflow_runs ADD COLUMN claimed_by TEXT")
            if "claimed_at" not in cols:
                conn.execute("ALTER TABLE workflow_runs ADD COLUMN claimed_at TEXT")
            conn.commit()
        finally:
            conn.close()
        _migrated.add(key)


def claim_run(run_id: str, owner: str, lease_sec: int = 0) -> bool:
    """原子抢占一个暂停中的 run。返回 True 表示本次抢占成功（只有一次会成功）。

    SQLite 依赖单条 UPDATE 的行级原子性；PG 版本是**同一条语句**（见方案设计 §4），
    因此该原语可平移。任务书 §3.2 提到的 advisory lock / 行级乐观锁即此处的最小实现。

    【审查修正 P1】原实现有两个可导致"审批永久锁死"的洞：
      1) owner 为空串时也能抢占，而 release_run(run_id, owner) 用 `claimed_by = owner`
         去释放 —— 空 owner 永远释放不掉，run 再也无法审批；
      2) 抢占没有租约：进程在 finally 之前被 kill（容器重启、OOM），claimed_by 永久残留，
         该 run 从此对所有审批人返回"已被处理中"。
    现在：owner 必填；lease_sec > 0 时，早于租约过期的占用可被接管（见 _claim_is_stale）。
    """
    if not (owner or "").strip():
        raise ValueError("claim_run 的 owner 不能为空（否则占用无法被释放）")
    ensure_claim_columns()
    stale_sec = int(lease_sec or _lease_sec())
    conn = _connect()
    try:
        if not _table_ready(conn):
            return False          # 框架尚未建表：没有任何可认领的 run
        cur = conn.execute(
            "UPDATE workflow_runs SET claimed_by = ?, claimed_at = ? "
            "WHERE run_id = ? AND status = 'paused' "
            "AND (claimed_by IS NULL OR claimed_by = '' OR claimed_at IS NULL"
            "     OR claimed_at < ?)",
            (owner, _now_str(), run_id, _stale_before(stale_sec)),
        )
        conn.commit()
        if cur.rowcount != 1:
            return False
        row = conn.execute("SELECT claimed_by FROM workflow_runs WHERE run_id=?",
                           (run_id,)).fetchone()
        # 极端竞争下理论上仍可能被别人覆盖，这里做一次回读确认（与 queue_store.claim_todo 同风格）
        return bool(row) and row["claimed_by"] == owner
    finally:
        conn.close()


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _lease_sec() -> int:
    try:
        return max(60, int(os.environ.get("RUN_CLAIM_LEASE_SEC", "900")))
    except Exception:
        return 900


def _stale_before(lease_sec: int) -> str:
    """返回"过期阈值"时间串：claimed_at 早于它就视为僵尸占用。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - int(lease_sec)))


def claim_is_stale(run_id: str, lease_sec: int = 0) -> bool:
    """该 run 的占用是否已过期（供启动补偿与管理员释放使用）。"""
    info = get_claim(run_id) or {}
    at = (info.get("claimed_at") or "").strip()
    if not at:
        return bool((info.get("claimed_by") or "").strip())   # 有占用但无时间戳 → 视为异常占用
    return at < _stale_before(int(lease_sec or _lease_sec()))


def release_stale(lease_sec: int = 0) -> list[str]:
    """释放所有已过期的占用，返回被释放的 run_id 列表（启动补偿时调用）。

    这是"进程崩溃导致审批永久锁死"的兜底：不需要人工介入即可自愈。
    """
    ensure_claim_columns()
    threshold = _stale_before(int(lease_sec or _lease_sec()))
    conn = _connect()
    try:
        if not _table_ready(conn):
            return []
        rows = conn.execute(
            "SELECT run_id FROM workflow_runs WHERE claimed_by IS NOT NULL AND claimed_by != '' "
            "AND status='paused' AND (claimed_at IS NULL OR claimed_at < ?)",
            (threshold,)).fetchall()
        ids = [r["run_id"] for r in rows]
        if ids:
            conn.execute(
                "UPDATE workflow_runs SET claimed_by = NULL, claimed_at = NULL "
                "WHERE claimed_by IS NOT NULL AND claimed_by != '' AND status='paused' "
                "AND (claimed_at IS NULL OR claimed_at < ?)", (threshold,))
            conn.commit()
        return ids
    finally:
        conn.close()


def release_run(run_id: str, owner: str = "") -> None:
    """释放占用（恢复流程结束后调用）。owner 非空时仅释放自己的占用。"""
    ensure_claim_columns()
    conn = _connect()
    try:
        if not _table_ready(conn):
            return
        if owner:
            conn.execute("UPDATE workflow_runs SET claimed_by = NULL, claimed_at = NULL "
                         "WHERE run_id = ? AND claimed_by = ?", (run_id, owner))
        else:
            conn.execute("UPDATE workflow_runs SET claimed_by = NULL, claimed_at = NULL "
                         "WHERE run_id = ?", (run_id,))
        conn.commit()
    finally:
        conn.close()


def get_claim(run_id: str) -> dict | None:
    ensure_claim_columns()
    conn = _connect()
    try:
        if not _table_ready(conn):
            return None
        row = conn.execute("SELECT claimed_by, claimed_at FROM workflow_runs WHERE run_id = ?",
                           (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_pending(limit: int = 100) -> list[dict]:
    """列出暂停中的 run（含占用信息）。供启动扫描补发通知与统一待办队列使用。"""
    ensure_claim_columns()
    conn = _connect()
    try:
        if not _table_ready(conn):
            return []
        rows = conn.execute(
            "SELECT run_id, workflow_id, status, loop_kind, interrupt_info, origin_context, "
            "claimed_by, claimed_at, created_at, updated_at FROM workflow_runs "
            "WHERE status = 'paused' ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
