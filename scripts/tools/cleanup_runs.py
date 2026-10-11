#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
运行数据清理工具（本项目自有）

为什么需要：D0/P2/P3/P4 的自检会不断产生 run 与待办（用于验证暂停、并发、恢复等），
时间一长会污染 `pending()` 与 `metrics()` 的可读性，也让"试用/演示"打开就是一片历史垃圾。
本工具把"清理测试遗留"变成显式操作，而不是靠手工删库。

用法：
    python scripts/tools/cleanup_runs.py --dry-run            # 只看会清掉什么
    python scripts/tools/cleanup_runs.py --status paused      # 取消所有暂停中的 run
    python scripts/tools/cleanup_runs.py --prefix p3_ wf_run_ # 按 run_id 前缀清理
    python scripts/tools/cleanup_runs.py --all-todos          # 清空待办与审计（谨慎）
    python scripts/tools/cleanup_runs.py --trial-reset        # 试用/演示前重置（见下）

【trial-reset 做什么】把工作台恢复成"可以直接给人看"的状态：
    1) 取消所有 `paused` 的 run（并删掉它们的 checkpoint / writes 与待办）
    2) 清空全部待办（待办是工作队列；证据留在 append-only 的审计表里）
    3) 删除自检产生的测试账号（默认保留 admin / zhangsan / lisi，可用 --keep-user 追加）
    4) **默认不动审计链**（审计是 append-only 的证据，只在显式 --purge-audit 时按 run 删除）
    5) 保留 completed 的 run 作为"系统确实在运行"的历史（可按 --status 另行处理）

【第二次审查修正】本工具原先**只连 SQLite**（`sqlite3.connect(get_db_path())`）：
    `DB_MODE=pg` 时 `compliance_todo / compliance_audit / compliance_user` 在 PostgreSQL 里，
    这里删的是**另一个库** —— 删不掉、也不报错，运维会以为清理成功了。
    现在按表分层连接：框架表（workflow_runs / checkpoints / writes）走 SQLite，
    本项目自有表走 `mcp_server.db.connect()`（按 DB_MODE 分派）。
"""

import argparse
import importlib.util
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 复用 d0 的引导脚本：它负责把框架源码与项目根加入 path，并把工作目录切到运行时工作区
# （框架的数据库路径是"进程当前工作目录"相对路径，不切目录会连到另一个库）
_boot = PROJECT_ROOT / "scripts" / "d0" / "_bootstrap.py"
_spec = importlib.util.spec_from_file_location("_d0_bootstrap", str(_boot))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_mod.bootstrap()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 试用/演示默认保留的账号（与本地 IdP 的三个演示身份对应）
DEFAULT_KEEP_USERS = ("admin", "zhangsan", "lisi")


def _framework_conn():
    """框架表专用连接（框架 DAO 按设计只写 SQLite）。"""
    from dao.db import get_db_path
    conn = sqlite3.connect(str(get_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _compliance_conn(fw):
    """本项目自有表连接（DB_MODE=sqlite 走 sqlite，=pg 走 PostgreSQL）。

    【实测踩到的坑】sqlite 模式下框架表与自有表**在同一个文件里**：如果另开一条连接，
    两条连接会互相持锁（实测 `sqlite3.OperationalError: database is locked`）。
    因此 sqlite 模式必须**复用同一条连接**；只有 pg 模式才需要单独连（表在不同库）。
    返回 `(conn, owned)`，`owned=False` 表示借用、不要关。
    """
    from mcp_server import db as db_mod
    if db_mod.db_mode() != "pg":
        return fw, False
    return db_mod.connect(), True


def _backend() -> str:
    try:
        from mcp_server import db as db_mod
        return str(db_mod.describe().get("db_mode") or "sqlite")
    except Exception as exc:
        return f"unknown({type(exc).__name__})"


def _cancel_runs(fw, run_ids: list[str], error: str) -> int:
    changed = 0
    for run_id in run_ids:
        fw.execute("UPDATE workflow_runs SET status='cancelled', error=?, "
                   "claimed_by=NULL, claimed_at=NULL WHERE run_id=?", (error, run_id))
        fw.execute("DELETE FROM checkpoints WHERE thread_id=?", (run_id,))
        fw.execute("DELETE FROM writes WHERE thread_id=?", (run_id,))
        changed += 1
    return changed


def _archive_proposals() -> dict:
    """把历史规则提案**归档搬出** `data/proposals`（试用/演示前重置用）。

    为什么是"归档"而不是"删除"：提案是规则演进的历史记录，删掉不可逆。
    移到 `.runtime/archive/proposals-<时间戳>/` 既让工作台恢复干净
    （否则一打开「规则提案」就是上百条自检留下的待批项），又随时能搬回来。
    """
    import shutil
    import time as _time
    src = PROJECT_ROOT / "data" / "proposals"
    files = sorted(src.glob("*.json")) if src.exists() else []
    if not files:
        return {"proposals_archived": 0, "proposals_archive_dir": None}
    dest = PROJECT_ROOT / ".runtime" / "archive" / f"proposals-{_time.strftime('%Y%m%d-%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.move(str(f), str(dest / f.name))
    return {"proposals_archived": len(files), "proposals_archive_dir": str(dest)}


def _trial_reset(fw, cc, keep_users: tuple[str, ...], purge_audit: bool,
                 archive_proposals: bool = True) -> dict:
    """试用前重置：返回各项处理条数。"""
    out: dict = {"backend": _backend(), "keep_users": list(keep_users)}

    rows = fw.execute("SELECT run_id FROM workflow_runs WHERE status='paused'").fetchall()
    paused = [r["run_id"] for r in rows]
    out["paused_runs_cancelled"] = _cancel_runs(fw, paused, "清理：试用前重置（自检遗留）")

    out["todos_deleted"] = cc.execute("DELETE FROM compliance_todo").rowcount or 0

    # 测试账号：保留演示身份，其余删除（含其令牌撤销水位）
    users = [r["username"] for r in cc.execute("SELECT username FROM compliance_user").fetchall()]
    doomed = [u for u in users if u not in keep_users]
    for u in doomed:
        cc.execute("DELETE FROM compliance_token_revocation WHERE token_id=?", (f"all:{u}",))
        # 登录退避表的键是 `scope`（`<username>@<ip>` 或 `ip:<ip>`），不是 username
        cc.execute("DELETE FROM compliance_login_fail WHERE scope LIKE ?", (f"{u}@%",))
        cc.execute("DELETE FROM compliance_user WHERE username=?", (u,))
    out["users_deleted"] = len(doomed)
    out["users_kept"] = [u for u in users if u in keep_users]

    if archive_proposals:
        out.update(_archive_proposals())

    if purge_audit:
        out["audit_deleted"] = cc.execute("DELETE FROM compliance_audit").rowcount or 0
    else:
        out["audit_kept"] = cc.execute(
            "SELECT COUNT(*) c FROM compliance_audit").fetchone()["c"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="清理自检产生的运行数据")
    parser.add_argument("--dry-run", action="store_true", help="只统计不执行")
    parser.add_argument("--status", default="", help="按 run 状态清理（如 paused）")
    parser.add_argument("--prefix", nargs="*", default=[], help="按 run_id 前缀清理")
    parser.add_argument("--all-todos", action="store_true", help="清空全部待办与审计事件")
    parser.add_argument("--purge-audit", action="store_true",
                        help="同时删除这些 run 的审计事件（默认保留：审计链是 append-only 证据）")
    parser.add_argument("--trial-reset", action="store_true",
                        help="试用/演示前重置：取消 paused run + 清空待办 + 删除测试账号")
    parser.add_argument("--keep-user", nargs="*", default=list(DEFAULT_KEEP_USERS),
                        help=f"--trial-reset 时保留的账号（默认 {' '.join(DEFAULT_KEEP_USERS)}）")
    parser.add_argument("--keep-proposals", action="store_true",
                        help="--trial-reset 时**不**归档历史提案（默认归档到 .runtime/archive/）")
    parser.add_argument("--limit", type=int, default=500, help="单次处理上限（0=不限）")
    args = parser.parse_args()

    fw = _framework_conn()
    # 本项目自有表：sqlite 模式下复用同一条连接（同库双连接会互相持锁），pg 模式才单独连
    cc, cc_owned = _compliance_conn(fw)
    try:
        if args.trial_reset:
            if args.dry_run:
                rows = fw.execute("SELECT run_id FROM workflow_runs WHERE status='paused'").fetchall()
                n_todo = cc.execute("SELECT COUNT(*) c FROM compliance_todo").fetchone()["c"]
                users = [r["username"] for r in cc.execute(
                    "SELECT username FROM compliance_user").fetchall()]
                doomed = [u for u in users if u not in tuple(args.keep_user)]
                print(f"[cleanup] 后端={_backend()}（dry-run 未修改）")
                print(f"[cleanup] 将取消 paused run {len(rows)} 个（含其 checkpoint/writes）")
                print(f"[cleanup] 将清空待办 {n_todo} 条")
                print(f"[cleanup] 将删除测试账号 {len(doomed)} 个；保留 "
                      f"{[u for u in users if u in tuple(args.keep_user)]}")
                n_prop = len(list((PROJECT_ROOT / "data" / "proposals").glob("*.json")))
                print(f"[cleanup] 将归档历史规则提案 {n_prop} 条"
                      f"{'（--keep-proposals 已跳过）' if args.keep_proposals else ''}")
                print("[cleanup] 审计链默认保留（append-only 证据）")
                return 0
            res = _trial_reset(fw, cc, tuple(args.keep_user), args.purge_audit,
                               archive_proposals=not args.keep_proposals)
            for k, v in res.items():
                print(f"[cleanup] {k} = {v}")
            fw.commit()
            cc.commit()
            print("[cleanup] 试用前重置完成：工作台待办为空、在办 run 归零、测试账号已清理")
            return 0

        targets: list[str] = []
        limit = args.limit if args.limit and args.limit > 0 else -1
        if args.status:
            rows = fw.execute("SELECT run_id FROM workflow_runs WHERE status = ? LIMIT ?",
                              (args.status, limit)).fetchall()
            targets += [r["run_id"] for r in rows]
        for p in args.prefix:
            rows = fw.execute("SELECT run_id FROM workflow_runs WHERE run_id LIKE ? LIMIT ?",
                              (f"{p}%", limit)).fetchall()
            targets += [r["run_id"] for r in rows]
        targets = sorted(set(targets))

        print(f"[cleanup] 后端={_backend()} 命中 run {len(targets)} 个"
              f"（status={args.status or '-'} prefix={args.prefix or '-'}）")
        for r in targets[:10]:
            print(f"   - {r}")
        if len(targets) > 10:
            print(f"   ... 其余 {len(targets) - 10} 个")

        if args.dry_run:
            print(f"[cleanup] 当前待办 "
                  f"{cc.execute('SELECT COUNT(*) c FROM compliance_todo').fetchone()['c']} 条、"
                  f"审计 "
                  f"{cc.execute('SELECT COUNT(*) c FROM compliance_audit').fetchone()['c']} 条"
                  f"（dry-run 未修改）")
            return 0

        changed = _cancel_runs(fw, targets, "清理：自检遗留")
        for run_id in targets:
            cc.execute("DELETE FROM compliance_todo WHERE run_id=?", (run_id,))
            # 【审查修正 P2】原语句是 `UPDATE compliance_audit SET event = event WHERE run_id=?`
            # —— 空操作（既没删也没改），却仍然取写锁、抬升行版本。
            # 审计链是 append-only 的证据，**默认保留**（这与"证据链不可变"一致）；
            # 只有显式 --purge-audit 时才按 run 删除。
            if args.purge_audit:
                cc.execute("DELETE FROM compliance_audit WHERE run_id=?", (run_id,))
        if args.all_todos:
            t = cc.execute("DELETE FROM compliance_todo").rowcount
            a = cc.execute("DELETE FROM compliance_audit").rowcount
            print(f"[cleanup] 清空待办 {t} 条、审计 {a} 条")
        fw.commit()
        cc.commit()
        print(f"[cleanup] 已取消 {changed} 个 run（含其 checkpoint 与待办）")
        return 0
    finally:
        for conn in ((fw, cc) if cc_owned else (fw,)):
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
