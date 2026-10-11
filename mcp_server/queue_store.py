#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一待办队列与证据链（本项目自有，MCP 服务使用）

为什么需要它（方案设计 §3.3）：
    运行时框架的人工节点只发一条文本通知，run_id 靠用户手动回填；框架本身
    没有待办表、没有自动恢复、没有投递确认。合规审批必须有明确的"谁该做什么"，
    因此本项目自建一张待办表，把「专员初审 / 主管复核 / 规则提案审批 / 低风险抽查」
    收敛到同一套机制，并让服务端用待办里的 run_id 恢复，而不是让模型从聊天里猜。

存储：与框架共用同一个 SQLite 库（single-DB 约定），但表由本项目创建与维护，
     不侵入框架的 workflow_runs 语义。PG 版本是同构 DDL（方案设计 §4）。
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()
_migrated: set[str] = set()

VALID_STATUS = ("open", "claimed", "done", "expired")
VALID_ROLES = ("specialist", "supervisor", "admin")

# 建表语句已收敛到 mcp_server/db.py 的**单一列定义**（同一份定义渲染 SQLite/PG 两套 DDL）。
# 这里不再保留拷贝，避免"改了一处忘了另一处"导致两个后端表结构漂移。
# 表结构查看： python -c "from mcp_server import db; print(db.ddl('compliance_todo'))"


def _db_path() -> Path:
    # 【审查修正 P1】统一走 _dbpath.resolve_db_path()：
    # 原来直接 `from dao.db import get_db_path`，在能力服务进程里（未注入框架路径）
    # 会 ModuleNotFoundError；且各入口 cwd 不同会落到两个不同的库文件。
    from mcp_server._dbpath import resolve_db_path
    return resolve_db_path()


def _connect():
    """取连接。`DB_MODE=pg` 时落到 PostgreSQL（见 mcp_server/db.py）。"""
    from mcp_server import db as _db
    return _db.connect()


def ensure_schema() -> None:
    """幂等建表 + **补列**（待办 + 审计）。两种后端共用一套列定义。

    【真实旧库实测修正】本函数以前"每个进程只跑一次"（`_migrated` 缓存），
    而升级后新增了 `seq` 列（见 db.py 说明：PG 没有 rowid）。**已存在的旧库**
    若恰好在某进程里标记过 migrated，就永远拿不到补列，调用方会撞上
    `sqlite3.OperationalError: no such column: seq`（实测踩过）。
    现在每次调用都执行"补列"（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，廉价且幂等），
    再执行建表 —— 老库因此自动升级，无需人工迁移。
    """
    from mcp_server import db as _db
    # 先补列（针对已存在的旧表），再建表（针对新库）；顺序不可颠倒
    for table in ("compliance_todo", "compliance_audit"):
        for stmt in _db._upgrade_columns(table):
            try:
                conn = _db.connect()
                try:
                    conn.execute(stmt)
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass          # 已存在或该后端不支持 IF NOT EXISTS 时忽略
    _db.ensure_tables(["compliance_todo", "compliance_audit"])


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 待办

def create_todo(run_id: str, node_name: str, role_required: str, title: str,
                summary: str = "", payload: dict | None = None, workflow_id: str = "",
                trace_id: str = "", due_at: str = "", idempotency_key: str = "") -> dict:
    """创建待办（幂等：同一 idempotency_key 只创建一次）。

    【审查修正 P2】原实现是"先查后插"（非原子）：并发下两个请求都能通过 SELECT，
    随后 INSERT 撞 `idempotency_key` 唯一约束直接抛 sqlite3.IntegrityError → 网关 500。
    现在捕获唯一键冲突并返回既有待办（真正的幂等语义）。
    """
    ensure_schema()
    if role_required not in VALID_ROLES:
        return {"ok": False, "errors": [f"role_required 必须是 {VALID_ROLES} 之一"]}
    key = idempotency_key or f"{run_id}:{node_name}:{role_required}"
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM compliance_todo WHERE idempotency_key = ?", (key,)).fetchone()
        if row:
            return {"ok": True, "created": False, "todo": dict(row)}
        todo_id = f"todo_{uuid.uuid4().hex[:10]}"
        ts = _now()
        try:
            conn.execute(
                "INSERT INTO compliance_todo (todo_id, run_id, workflow_id, node_name, role_required,"
                " title, summary, payload_json, status, idempotency_key, trace_id, created_at, due_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?, 'open', ?,?,?,?,?)",
                (todo_id, run_id, workflow_id, node_name, role_required, title, summary,
                 json.dumps(payload or {}, ensure_ascii=False), key, trace_id, ts, due_at or None, ts))
            conn.commit()
        except sqlite3.IntegrityError:
            # 并发下另一个请求刚插入了同一幂等键：返回它，不算失败
            row = conn.execute("SELECT * FROM compliance_todo WHERE idempotency_key = ?",
                               (key,)).fetchone()
            if row:
                return {"ok": True, "created": False, "todo": dict(row), "race": True}
            raise
        row = conn.execute("SELECT * FROM compliance_todo WHERE todo_id = ?", (todo_id,)).fetchone()
        record_audit(run_id, "todo_created", actor="system",
                     detail={"todo_id": todo_id, "node": node_name, "role": role_required},
                     trace_id=trace_id, todo_id=todo_id)
        return {"ok": True, "created": True, "todo": dict(row)}
    finally:
        conn.close()


def list_todos(role: str = "", status: str = "open", run_id: str = "", limit: int = 50) -> list[dict]:
    ensure_schema()
    clauses, params = [], []
    if role:
        clauses.append("role_required = ?")
        params.append(role)
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, int(limit)))
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM compliance_todo{where} ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def claim_todo(todo_id: str, actor: str) -> dict:
    """原子领取待办：只有一次会成功，后到者拿到明确的"已被占用"。

    这是并发审批互斥在**待办层**的实现（与 runtime_store.claim_run 的 run 层互斥互补）。
    """
    ensure_schema()
    if not (actor or "").strip():
        return {"ok": False, "errors": ["actor 不能为空"]}
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE compliance_todo SET status='claimed', claimed_by=?, claimed_at=?, updated_at=? "
            "WHERE todo_id=? AND status='open'", (actor, _now(), _now(), todo_id))
        conn.commit()
        if cur.rowcount != 1:
            row = conn.execute("SELECT claimed_by, status FROM compliance_todo WHERE todo_id=?",
                               (todo_id,)).fetchone()
            if row is None:
                return {"ok": False, "errors": [f"待办不存在：{todo_id}"]}
            return {"ok": False, "claimed": False,
                    "errors": [f"该待办已被 {row['claimed_by'] or '他人'} 处理（状态 {row['status']}）"],
                    "holder": row["claimed_by"], "status": row["status"]}
        row = conn.execute("SELECT * FROM compliance_todo WHERE todo_id=?", (todo_id,)).fetchone()
        record_audit(row["run_id"], "todo_claimed", actor=actor, role=row["role_required"],
                     detail={"todo_id": todo_id}, todo_id=todo_id)
        return {"ok": True, "claimed": True, "todo": dict(row)}
    finally:
        conn.close()


def complete_todo(todo_id: str, actor: str, decision: str, note: str = "",
                  force: bool = False) -> dict:
    """完成待办（写入决策与操作者，供证据链与指标统计）。

    【审查修正 P2】原实现不校验完成者是否就是领取者：
    甲领取待办后，乙可以用同一条待办的 todo_id 直接 complete → 审计链上"谁处理的"
    与"谁领取的"错位（claim_todo 的并发互斥形同虚设）。现在：
      - 待办已被他人领取时，只有该领取者（或 force=True 的管理员补偿）能办结；
      - 已 done 的待办不允许再次办结（避免重复记账）。
    """
    ensure_schema()
    if not (actor or "").strip():
        return {"ok": False, "errors": ["actor 不能为空"]}
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM compliance_todo WHERE todo_id=?", (todo_id,)).fetchone()
        if row is None:
            return {"ok": False, "errors": [f"待办不存在：{todo_id}"]}
        if row["status"] == "done":
            return {"ok": False, "errors": [f"待办已完成（决策 {row['decision']}）"]}
        holder = (row["claimed_by"] or "").strip()
        if row["status"] == "claimed" and holder and holder != actor and not force:
            return {"ok": False, "claimed": False,
                    "errors": [f"该待办已被 {holder} 领取，{actor} 不能代为办结"],
                    "holder": holder}
        conn.execute(
            "UPDATE compliance_todo SET status='done', decided_by=?, decided_at=?, decision=?,"
            " summary = CASE WHEN ? = '' THEN summary ELSE summary || ' | 处理意见: ' || ? END,"
            " updated_at=? WHERE todo_id=?",
            (actor, _now(), decision, note, note, _now(), todo_id))
        conn.commit()
        record_audit(row["run_id"], "todo_completed", actor=actor, role=row["role_required"],
                     detail={"todo_id": todo_id, "decision": decision, "note": note[:200]},
                     todo_id=todo_id)
        return {"ok": True, "todo_id": todo_id, "decision": decision, "run_id": row["run_id"],
                "node_name": row["node_name"]}
    finally:
        conn.close()


# ------------------------------------------------------------------ 证据链

def record_audit(run_id: str, event: str, actor: str = "", role: str = "",
                 detail: dict | None = None, trace_id: str = "", todo_id: str = "") -> dict:
    """追加一条审计事件（append-only）。"""
    ensure_schema()
    audit_id = f"aud_{uuid.uuid4().hex[:10]}"
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO compliance_audit (audit_id, run_id, todo_id, event, actor, role,"
            " detail_json, trace_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (audit_id, run_id, todo_id or None, event, actor or "", role or "",
             json.dumps(detail or {}, ensure_ascii=False), trace_id, _now()))
        conn.commit()
        return {"ok": True, "audit_id": audit_id}
    finally:
        conn.close()


def list_audit(run_id: str = "", newest_first: bool = False, limit: int = 100) -> list[dict]:
    """返回审计事件（默认按时间正序 = 证据链顺序）。

    【审查修正 P2】"全部 run"分支原先是 `ORDER BY created_at DESC LIMIT ?`，
    调用方若再切片（网关审计台 `rows[:limit]`）就会把**最新事件丢掉**、只显示最旧的。
    现在统一按 `created_at, seq` 稳定排序（seq 兜住同秒事件），并给需要
    "最新优先"的界面调用方一个显式开关，不再靠 SQL 方向隐式决定语义。
    """
    ensure_schema()
    conn = _connect()
    try:
        if run_id:
            rows = conn.execute("SELECT * FROM compliance_audit WHERE run_id=? "
                                "ORDER BY created_at ASC, seq ASC LIMIT ?",
                                (run_id, limit)).fetchall()
            out = [dict(r) for r in rows]
        else:
            rows = conn.execute("SELECT * FROM compliance_audit "
                                "ORDER BY created_at ASC, seq ASC LIMIT ?",
                                (limit,)).fetchall()
            out = [dict(r) for r in rows]
        return list(reversed(out)) if newest_first else out
    finally:
        conn.close()


def list_audit_recent(limit: int = 100) -> list[dict]:
    """最新的 limit 条审计事件（时间正序返回，便于直接渲染）。"""
    ensure_schema()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM compliance_audit "
                            "ORDER BY created_at DESC, seq DESC LIMIT ?",
                            (max(1, int(limit)),)).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        conn.close()


def list_audit_since(run_id: str, cursor: str = "", limit: int = 200) -> list[dict]:
    """增量拉取审计事件（游标 = 上一条 audit_id）。

    【审查修正 P2】SSE 事件流原实现用定长切片 `events[seen:]` 做增量，
    单 run 审计超过 limit 条后新事件永远不会再推送。游标式查询没有这个上限。
    由于 created_at 只有秒精度，游标比较用 (created_at, seq) 复合条件保证不漏不重。
    """
    ensure_schema()
    conn = _connect()
    try:
        if cursor:
            base = conn.execute("SELECT created_at, seq FROM compliance_audit WHERE audit_id=?",
                                (cursor,)).fetchone()
            if base is not None:
                rows = conn.execute(
                    "SELECT * FROM compliance_audit WHERE run_id=? AND "
                    "(created_at > ? OR (created_at = ? AND COALESCE(seq,0) > COALESCE(?,0))) "
                    "ORDER BY created_at ASC, seq ASC LIMIT ?",
                    (run_id, base["created_at"], base["created_at"], base["seq"],
                     max(1, int(limit)))).fetchall()
                return [dict(r) for r in rows]
        rows = conn.execute("SELECT * FROM compliance_audit WHERE run_id=? "
                            "ORDER BY created_at ASC, seq ASC LIMIT ?",
                            (run_id, max(1, int(limit)))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_audit(run_id: str, event: str = "") -> int:
    """统计某 run 的审计事件数（可按事件名过滤）。

    用途：为"第 N 轮待办"生成**稳定**幂等键（不含时间戳），
    使客户端重试不会绕过幂等（见 review_orchestrator.decide 的审查修正说明）。
    """
    ensure_schema()
    conn = _connect()
    try:
        if event:
            row = conn.execute("SELECT COUNT(*) c FROM compliance_audit WHERE run_id=? AND event=?",
                               (run_id, event)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) c FROM compliance_audit WHERE run_id=?",
                               (run_id,)).fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()


def metrics() -> dict:
    """指标快照：任务量、人审率、平均办结轮次、驳回原因分布。"""
    ensure_schema()
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM compliance_todo").fetchone()["c"]
        done = conn.execute("SELECT COUNT(*) c FROM compliance_todo WHERE status='done'").fetchone()["c"]
        open_cnt = conn.execute("SELECT COUNT(*) c FROM compliance_todo WHERE status='open'").fetchone()["c"]
        claimed = conn.execute("SELECT COUNT(*) c FROM compliance_todo WHERE status='claimed'").fetchone()["c"]
        by_decision = {r["decision"]: r["c"] for r in conn.execute(
            "SELECT decision, COUNT(*) c FROM compliance_todo WHERE decision IS NOT NULL "
            "GROUP BY decision")}
        by_role = {r["role_required"]: r["c"] for r in conn.execute(
            "SELECT role_required, COUNT(*) c FROM compliance_todo GROUP BY role_required")}
        # 驳回原因分布：从审计事件里取
        reject_reasons = []
        for r in conn.execute("SELECT detail_json FROM compliance_audit WHERE event IN "
                              "('todo_completed','rule_proposal_created') ORDER BY created_at DESC LIMIT 200"):
            try:
                d = json.loads(r["detail_json"])
            except Exception:
                continue
            if d.get("decision") == "reject" or d.get("reason"):
                reject_reasons.append(str(d.get("reason") or d.get("note") or "")[:60])
        return {
            "todo_total": total, "todo_done": done, "todo_open": open_cnt, "todo_claimed": claimed,
            # 【审查修正 P2】原式 `total / done` 在 done>total 不可能的前提下恒 ≥1
            # （实测 evidence 记到 1.2857），既不是"率"也无法解释。
            # 现在给出两个口径清晰的字段：
            #   human_todo_completion_rate = 已办结 / 全部待办（0~1 的完成率）
            #   human_todo_per_finished_run = 每条办结需几条人工待办（≥1，衡量人工介入强度）
            "human_todo_completion_rate": round(done / total, 4) if total else None,
            "human_todo_per_finished_run": round(total / done, 4) if done else None,
            "by_decision": by_decision, "by_role": by_role,
            "reject_reason_samples": reject_reasons[:10],
            "generated_at": _now(),
        }
    finally:
        conn.close()


def notify_pending_on_startup() -> dict:
    """启动时扫描暂停中的 run，为缺失的待办补建（框架本身没有这个能力）。

    返回补建清单，便于把"重启后重新通知"写进日志与证据。
    """
    from mcp_server import runtime_store
    pending = runtime_store.list_pending()
    created = []
    for run in pending:
        info = {}
        try:
            info = json.loads(run.get("interrupt_info") or "{}")
        except Exception:
            pass
        question = info.get("question") or "工作流已暂停，等待人工处理"
        node = info.get("kind") or run.get("loop_kind") or "human"
        role = "supervisor" if "主管" in str(question) else "specialist"
        res = create_todo(run_id=run["run_id"], node_name=str(node), role_required=role,
                          title=f"待处理：{str(question)[:40]}", summary=str(question),
                          payload=info, workflow_id=run.get("workflow_id", ""))
        if res.get("created"):
            created.append(res["todo"]["todo_id"])
    return {"pending_runs": len(pending), "todos_created": created}
