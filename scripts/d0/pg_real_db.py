#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**真实 PostgreSQL 端到端验证**（一次性编排：起库 → 真跑业务 → 落证据 → 停库）

为什么写成"一个脚本跑完全部"：
    本机受限环境会在每条命令之间回收子进程，因此"先起库、后另开命令去连"是行不通的。
    这里在**同一次进程执行内**完成：start → 建表 → 真实读写 → 校验 → 落证据 → stop。

被测对象（全部真实代码，无替身）：
    1) `mcp_server/db.py` 的 PG 连接层（pg8000 驱动 + 方言转换 + upsert + 断线自愈）
    2) `mcp_server/queue_store.py` 的待办/审计（幂等创建、原子领取、游标查询、指标）
    3) `service/auth.py` 的账号体系（建号、验密、令牌撤销水位、登录退避）

产出：docs/evidence/pg-real-db.json
用法：
    pwsh -File scripts/pg-local-ascii.ps1 -Action init      # 首次：解压 + initdb + 起库
    .\\.runtime\\venv\\Scripts\\python.exe scripts\\d0\\pg_real_db.py

环境变量：
    PG_LOCAL_ROOT        PostgreSQL 程序与数据目录（默认 C:\\co-pg）
    PG_LOCAL_PORT        端口（默认 55432）
    PG_REQUIRE=1         实例缺失时判为失败（供门禁/CI 使用；默认只跳过并告警）

----------------------------------------------------------------------------
【第二次审查修正】本脚本原先有 6 个问题，全部已修：
  1. 主体没有任何 try/except/finally —— 中途抛异常就**既不落证据也不停库**，
     留下一个后台 postgres.exe，而证据文件仍停留在上一次运行的内容（假绿）。
     现在主体包在 try/except/finally 里，异常也留痕、也停库。
  2. `pg_terminate_backend` 终止的是该库上**所有**其它后端 —— 若同一实例上跑着
     应用/网关，会被强制断连并丢弃进行中的事务。现在先用
     `SELECT pg_backend_pid()` 取到本脚本自己那条连接，只终止它。
  3. `seq` 断言只检查"首行 seq 非 NULL" —— 常量 seq、复用同一 seq 都能通过，
     而"同秒事件稳定排序"恰恰依赖它**严格递增**。现在断言本轮多行严格递增。
  4. 证据在"停库/清理"**之前**写盘，`cleanup` 的结果永远进不了证据文件
     （实测 evidence 里根本没有 cleanup 键）。现在收尾完成后**补写一次**。
  5. `database_url` 用 `DB_URL.replace(DB_USER, DB_USER)` 假装脱敏（恒等操作），
     `createdb`/`psql` 的返回码从不检查 —— 建库失败会一路走到"连不上"的误导性报错。
     现在如实（按密码脱敏）写出 URL，并逐个检查返回码。
  6. 停库排在"清理本轮数据"之前 —— 自己起的实例上清理必然失败（连不上）。
     现在**先清理、后停库**。

  另外两处是本轮**真机重跑时才暴露**的：
  7. 判活用 `pg_ctl status`：受限/受管环境里 pg_ctl 要向 postmaster 发信号，
     被拒时返回非 0（实测 `could not send stop signal ... Operation not permitted`），
     于是"明明在跑"被判成"没在跑"，脚本又去 start，撞端口占用而失败。
     现在判活改成"**能不能连上**"（psql SELECT 1），顺带覆盖"在跑但不接受连接"。
  8. 服务端日志写在项目目录下 —— 而 `pg_ctl` 是 ANSI 程序，路径含中文目录名时
     打不开日志文件（实测 Permission denied 且路径显示为乱码），start 直接失败。
     现在日志固定写到工作区根的 ASCII 目录（`<workspace>/.pglogs/`，可用 PG_LOG_FILE 覆盖）。
"""

import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "pg-real-db.json"
PG_ROOT = Path(os.environ.get("PG_LOCAL_ROOT", r"C:\co-pg"))
PG_HOME = PG_ROOT / "pgsql"
PG_DATA = PG_ROOT / "data"
PORT = int(os.environ.get("PG_LOCAL_PORT", "55432"))
DB_NAME = "compliance"
DB_USER = "compliance"
DB_URL = f"postgresql://{DB_USER}@127.0.0.1:{PORT}/{DB_NAME}"

# 【第二次审查修正·本机实测发现】`pg_ctl` 是 ANSI 程序，**日志路径里不能有非 ASCII 字符**：
# 本项目路径含中文目录名时它打不开日志文件（实测
# `could not open log file "D:\...\<乱码>\...\pg-server.log": Permission denied`），
# 而 `pg_ctl start` 会在"写日志"这一步就整体失败 —— 表现为"库起不来"。
# 因此服务端日志固定放在工作区根的 ASCII 目录下（可用 PG_LOG_FILE 覆盖）。
SERVER_LOG = Path(os.environ.get("PG_LOG_FILE")
                  or (PROJECT_ROOT.parents[1] / ".pglogs" / "pg-server.log"))


def pg_bin(name: str) -> Path:
    return PG_HOME / "bin" / f"{name}.exe"


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def pg_ctl_status() -> bool:
    code, _ = run([pg_bin("pg_ctl"), "-D", str(PG_DATA), "status"], timeout=30)
    return code == 0


def pg_ping() -> bool:
    """实例是否**可用** —— 直接连一次，而不是只问 `pg_ctl status`。

    【第二次审查修正·本机实测发现】原来只用 `pg_ctl status` 判断存活：
    pg_ctl 需要向 postmaster 发信号来确认，在受限/受管环境里这一步会被拒绝
    （实测 `pg_ctl: could not send stop signal ... Operation not permitted`），
    于是"明明在跑"被判成"没在跑"，脚本接着去 start，撞上端口占用而失败。
    判活的标准应该是"能不能连上"，顺带也覆盖了"库在跑但不接受连接"这种半死状态。
    """
    return _psql("SELECT 1")[0] == 0


def masked_url(url: str) -> str:
    """证据文件会被提交/传阅，URL 里若带密码必须脱敏后再写。"""
    try:
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(url)
        if p.password:
            host = p.hostname or ""
            if p.port:
                host = f"{host}:{p.port}"
            return urlunsplit((p.scheme, f"{p.username}:***@{host}", p.path, p.query, p.fragment))
    except Exception:
        pass
    return url


def _psql(sql: str, db: str = DB_NAME, timeout: int = 60) -> tuple[int, str]:
    return run([pg_bin("psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", DB_USER,
                "-d", db, "-tAc", sql], timeout=timeout)


# ------------------------------------------------------------------ 各阶段

def _phase_start(ev: dict, check, st: dict) -> bool:
    """起库（若不可用）。返回实例是否可用。"""
    if not pg_ping():
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        code, out = run([pg_bin("pg_ctl"), "-D", str(PG_DATA), "-l", str(SERVER_LOG),
                         "-o", f"-p {PORT} -c listen_addresses=127.0.0.1", "start"], timeout=120)
        st["started_here"] = True
        ev["start"] = {"code": code, "output_tail": out[-300:], "log_file": str(SERVER_LOG)}
        for _ in range(20):                     # 最多等 20s，靠"能不能连"判就绪
            if pg_ping():
                break
            time.sleep(1)
    ev["started_by_this_run"] = st["started_here"]
    ok = pg_ping()
    check("postgres_running", ok,
          {"ping_ok": ok, "pg_ctl_status_ok": pg_ctl_status(), "start": ev.get("start")})
    return ok


def _phase_connect_db(ev: dict, check, ) -> bool:
    """建库（不存在则建）+ 版本探测。每一步都检查返回码。"""
    code, out = _psql(f"SELECT 1 FROM pg_database WHERE datname='{DB_NAME}'", db="postgres")
    if code != 0:
        # 【审查修正】原来不看返回码：psql 连不上/权限不足时 out 为空，
        # 会被误判成"库不存在"，接着去 createdb，报出与真实原因无关的错。
        ev["db_lookup"] = {"code": code, "output_tail": out[-200:]}
        check("database_exists_or_created", False, ev["db_lookup"])
        return False
    if not (out or "").strip():
        ccode, cout = run([pg_bin("createdb"), "-h", "127.0.0.1", "-p", str(PORT),
                           "-U", DB_USER, DB_NAME], timeout=60)
        ev["createdb"] = {"code": ccode, "output_tail": cout[-200:]}
        if ccode != 0:
            check("database_exists_or_created", False, ev["createdb"])
            return False
    code, ver = _psql("select version();")
    ev["server_version"] = (ver or "").strip()[:120]
    check("database_exists_or_created", code == 0 and "PostgreSQL" in (ver or ""),
          {"code": code, "version": ev["server_version"]})
    check("connected_to_postgres", "PostgreSQL" in (ver or ""), {"version": ev["server_version"]})
    return "PostgreSQL" in (ver or "")


def _phase_business(ev: dict, check, st: dict) -> None:
    """切到 PG 后端，跑真实业务：待办 / 审计 / 账号 / 本轮数据可见性 / seq。"""
    os.environ["DB_MODE"] = "pg"
    os.environ["DATABASE_URL"] = DB_URL
    from mcp_server import db as db_mod
    st["db_mod"] = db_mod
    info = db_mod.describe()
    ev["db_describe"] = info
    check("driver_is_pg8000_or_psycopg", info.get("driver") in ("pg8000", "psycopg3", "psycopg2"),
          info)

    db_mod.ensure_tables()
    from mcp_server import queue_store as Q
    Q._migrated.clear()
    from service import auth as A
    A._migrated.clear()

    # 3.1 待办：幂等创建 + 原子领取 + 非领取者不得办结
    run_id = f"pg_run_{uuid.uuid4().hex[:10]}"
    st["run_id"] = run_id
    t1 = Q.create_todo(run_id=run_id, node_name="review", role_required="specialist",
                       title="PG 验证待办", summary="S", payload={"k": "v"},
                       idempotency_key=f"{run_id}:k")
    t2 = Q.create_todo(run_id=run_id, node_name="review", role_required="specialist",
                       title="PG 验证待办", summary="S", idempotency_key=f"{run_id}:k")
    check("todo_idempotent_on_pg", t1.get("created") is True and t2.get("created") is False,
          {"first": t1.get("created"), "second": t2.get("created"), "todo_id": t1["todo"]["todo_id"]})
    tid = t1["todo"]["todo_id"]
    c1 = Q.claim_todo(tid, "alice")
    c2 = Q.claim_todo(tid, "bob")
    check("todo_atomic_claim_on_pg", c1.get("ok") is True and c2.get("ok") is False
          and c2.get("holder") == "alice", {"first": c1.get("claimed"), "second": c2.get("holder")})
    check("todo_claim_owner_enforced_on_pg", Q.complete_todo(tid, "bob", "confirm").get("ok") is False,
          {"attempt": "bob 非领取者办结"})
    done = Q.complete_todo(tid, "alice", "confirm", "同意")
    check("todo_completed_on_pg", done.get("ok") is True, done)

    # 3.2 审计：追加 + 正序 + 游标不重不漏
    # 注意：同一 run 下还有待办生命周期事件（todo_created/claimed/completed），
    # 因此断言要针对本次写入的 pg_ev* 子集，不要把整个 run 的事件当成全部（实测踩过）。
    for i in range(4):
        Q.record_audit(run_id, f"pg_ev{i}", actor="tester", role="specialist")
    events = Q.list_audit_since(run_id, cursor="", limit=100)
    st["expected_rows"] = len(events)
    ev_seq = [e["event"] for e in events if e["event"].startswith("pg_ev")]
    all_seq = [e["event"] for e in events]
    check("audit_order_on_pg", ev_seq == [f"pg_ev{i}" for i in range(4)]
          and "todo_completed" in all_seq,
          {"pg_events": ev_seq, "all_events": all_seq})
    # 游标：以第 2 条 pg_ev 为界，之后的 pg_ev 必须正好是 pg_ev2/pg_ev3
    mark = next(e["audit_id"] for e in events if e["event"] == "pg_ev1")
    rest = [e["event"] for e in Q.list_audit_since(run_id, cursor=mark)]
    check("audit_cursor_on_pg",
          [x for x in rest if x.startswith("pg_ev")] == ["pg_ev2", "pg_ev3"]
          and len({e["audit_id"] for e in events}) == len(events),
          {"after_cursor_all": rest, "unique_ids": len({e["audit_id"] for e in events}) == len(events)})
    m = Q.metrics()
    check("metrics_on_pg", "todo_total" in m and "human_todo_completion_rate" in m,
          {k: m[k] for k in ("todo_total", "todo_done", "human_todo_completion_rate")})

    # 3.3 账号：真建号 + 真验密 + 撤销水位
    uname = f"pguser_{uuid.uuid4().hex[:6]}"
    st["uname"] = uname
    cu = A.create_user(uname, "PgPass@2026x", "specialist", actor="pg-verify")
    check("user_created_on_pg", cu.get("ok") is True, cu)
    u = A.get_user(uname)
    check("password_verify_on_pg",
          bool(u) and A._verify_password("PgPass@2026x", u["salt"], u["password_hash"],
                                         u["iterations"]),
          {"username": uname, "role": u and u["role"]})
    A.revoke_user_tokens(uname, reason="pg-verify")
    A.ensure_schema()
    conn = db_mod.connect()
    try:
        row = conn.execute("SELECT revoked_at FROM compliance_token_revocation WHERE token_id=?",
                           (f"all:{uname}",)).fetchone()
    finally:
        conn.close()
    check("token_revocation_watermark_on_pg", bool(row and row["revoked_at"]),
          {"revoked_at": row and row["revoked_at"]})

    # 3.4 直查 PG 确认**本轮写入的数据真的在库里**（不是"表里有历史数据就算过"）
    # 【第二次审查修正】原来断言的是"四张表行数都 > 0"：脚本不清理历史数据，
    # 第二次运行哪怕一行都没写进去也会 PASS（假绿）。现在改为按本轮唯一标识精确查。
    conn = db_mod.connect()
    try:
        counts = {}
        for tbl in ("compliance_todo", "compliance_audit", "compliance_user",
                    "compliance_token_revocation"):
            r = conn.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()
            counts[tbl] = r["c"] if r else None
        this_run = {
            "todo_rows": conn.execute("SELECT COUNT(*) AS c FROM compliance_todo WHERE run_id=?",
                                      (run_id,)).fetchone()["c"],
            "audit_rows": conn.execute("SELECT COUNT(*) AS c FROM compliance_audit WHERE run_id=?",
                                       (run_id,)).fetchone()["c"],
            "user_rows": conn.execute("SELECT COUNT(*) AS c FROM compliance_user WHERE username=?",
                                      (uname,)).fetchone()["c"],
            "revocation_rows": conn.execute(
                "SELECT COUNT(*) AS c FROM compliance_token_revocation WHERE token_id=?",
                (f"all:{uname}",)).fetchone()["c"],
        }
    finally:
        conn.close()
    ev["table_counts"] = counts
    ev["this_run_rows"] = this_run
    check("this_run_rows_visible_in_postgres", all(v and v > 0 for v in this_run.values()),
          this_run)

    # 3.5 自增列 seq 真的在自增（PG 用 BIGSERIAL）——这是"同秒事件稳定排序"的基础
    conn = db_mod.connect()
    try:
        seq_rows = conn.execute("SELECT seq FROM compliance_audit WHERE run_id=? "
                                "ORDER BY seq ASC", (run_id,)).fetchall()
    finally:
        conn.close()
    seqs = [r["seq"] for r in seq_rows]
    # 【第二次审查修正】原来只断言"首行 seq 非 NULL"：常量 seq、或所有行复用同一 seq
    # 都能通过，而"同秒事件稳定排序"恰恰依赖它**严格递增**。现在断言多行严格递增。
    monotonic = len(seqs) >= 2 and all(
        seqs[i] is not None and seqs[i + 1] is not None and seqs[i] < seqs[i + 1]
        for i in range(len(seqs) - 1))
    ev["seq_values"] = seqs
    check("seq_autoincrement_works_on_pg", monotonic, {"seq": seqs})


def _phase_reconnect(ev: dict, check, st: dict) -> None:
    """连接被服务端断开后必须能自愈（生产会遇到：PG 重启 / 连接被网关回收）。"""
    if run([pg_bin("psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", DB_USER, "-d", DB_NAME,
            "-tAc", "SELECT 1"], timeout=60)[0] != 0:
        ev["reconnect"] = "skipped（psql 不可用）"
        return
    db_mod = st["db_mod"]
    # 【第二次审查修正】原来终止该库上**所有**其它后端（实测 killed=2）：
    # 若同一实例上跑着应用/网关，会被强制断连、丢弃进行中的事务。
    # 现在只精确终止本脚本自己那条连接的后端。
    try:
        own = db_mod.connect().execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        own = int(own)
    except Exception as exc:
        ev["reconnect"] = f"skipped（取不到本连接后端 pid）：{type(exc).__name__}: {exc}"
        return
    code, out = _psql(f"SELECT pg_terminate_backend({own})")
    terminated = (out or "").strip().lower().startswith("t")
    # 顺带记录：该库上还剩多少其它后端（证明我们没波及别人）
    _, others = _psql("SELECT count(*) FROM pg_stat_activity "
                      f"WHERE datname='{DB_NAME}' AND pid <> pg_backend_pid()")
    ev["reconnect"] = {"terminated_pid": own, "terminate_code": code, "terminated": terminated,
                       "other_backends_left": (others or "").strip()}
    # 被踢掉之后第一次调用可能失败（取决于驱动状态机），关键是**下一次必须成功**
    first_after = None
    for _attempt in range(3):
        try:
            row = db_mod.connect().execute("SELECT 2 AS ok").fetchone()
            first_after = row["ok"] if row else None
            break
        except Exception as exc:
            first_after = f"{type(exc).__name__}"
    ev["reconnect"]["value_after_kill"] = first_after
    check("connection_recovers_after_server_side_kill",
          terminated and first_after == 2, ev["reconnect"])


def _phase_psql_crosscheck(ev: dict, check, st: dict) -> None:
    """用 psql 交叉验证（换个客户端看同一份数据）。"""
    # 该 run 的事件 = 3 条待办生命周期 + 4 条 pg_ev = 7 条
    run_id = st["run_id"]
    code, psql_out = _psql(f"SELECT count(*) FROM compliance_audit WHERE run_id='{run_id}'")
    expected_rows = st.get("expected_rows")
    ev["psql_crosscheck"] = {"code": code, "audit_rows_for_run": (psql_out or "").strip(),
                             "expected": expected_rows}
    check("psql_crosscheck_sees_same_rows",
          code == 0 and expected_rows is not None
          and (psql_out or "").strip() == str(expected_rows),
          ev["psql_crosscheck"])


def _teardown(ev: dict, st: dict) -> None:
    """收尾：先清理本轮数据，再停库（顺序反了就会"自己起的库清不掉"）。"""
    db_mod = st.get("db_mod")
    run_id, uname = st.get("run_id"), st.get("uname")
    if db_mod is not None and run_id and uname:
        try:
            conn = db_mod.connect()
            try:
                for sql, params in (
                        ("DELETE FROM compliance_audit WHERE run_id=?", (run_id,)),
                        # 【第二次审查修正】账号类审计事件的 run_id 为空串（不挂在某个
                        # 工作流运行上），只按 run_id 清理会**每次留下一条**
                        # （实测 `auth.user_created` / run_id='' 逐轮累积）。
                        # 本脚本的写入统一带 actor='pg-verify'，按它精确回收。
                        ("DELETE FROM compliance_audit WHERE (run_id='' OR run_id IS NULL) "
                         "AND actor=?", ("pg-verify",)),
                        ("DELETE FROM compliance_todo WHERE run_id=?", (run_id,)),
                        ("DELETE FROM compliance_token_revocation WHERE token_id=?",
                         (f"all:{uname}",)),
                        ("DELETE FROM compliance_user WHERE username=?", (uname,))):
                    conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()
            ev["cleanup"] = "本轮验证数据已清理"
        except Exception as exc:
            ev["cleanup"] = f"清理失败（不影响判定）：{type(exc).__name__}: {exc}"
    if st.get("started_here"):
        code, out = run([pg_bin("pg_ctl"), "-D", str(PG_DATA), "-m", "fast", "stop"], timeout=90)
        ev["stopped_by_this_run"] = True
        ev["stop"] = {"code": code, "output_tail": out[-200:]}
        if code == 0:
            print("[pg] 已停止本机实例（本脚本自己起的）")
        else:
            # 停不掉不代表验证失败，但必须**说清楚**，不能让"遗留一个后台实例"被静默吞掉
            print(f"[pg][WARN] 停库失败（code={code}）：{(out or '').strip()[-160:]}")
            print("[pg][WARN] 本机 55432 上可能仍留着一个 postgres 实例，请手工确认。")


def _finalize(ev: dict, checks: dict, problems: list, force_verdict: str | None = None) -> None:
    """把状态落盘。异常路径上也要被调用（否则证据文件停留在上一次内容 → 假绿）。"""
    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = force_verdict or ("PASS" if not problems else "FAIL")
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")


def main() -> int:
    ev: dict = {"check": "PG-real-db", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "pg_root": str(PG_ROOT), "port": PORT, "database_url": masked_url(DB_URL)}
    checks: dict = {}
    problems: list[str] = []
    st: dict = {"started_here": False, "run_id": None, "uname": None, "db_mod": None}
    skipped = False
    rc = 1

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    try:
        if not (PG_HOME / "bin" / "postgres.exe").exists():
            skipped = True
            ev.update({"skipped": True, "reason": f"未找到 PostgreSQL 程序文件：{PG_HOME}"})
        elif not _phase_start(ev, check, st):
            pass
        elif not _phase_connect_db(ev, check):
            pass
        else:
            _phase_business(ev, check, st)
            _phase_reconnect(ev, check, st)
            _phase_psql_crosscheck(ev, check, st)
            rc = 0
    except Exception as exc:
        # 【第二次审查修正】原来 main() 里没有任何异常保护：中途抛异常就既不落证据
        # 也不停库。现在异常也留痕、也走 finally 收尾。
        ev["exception"] = f"{type(exc).__name__}: {exc}"
        ev["traceback_tail"] = traceback.format_exc()[-2000:]
        if "unexpected_exception" not in problems:
            problems.append("unexpected_exception")
        print(f"[pg] 未预期异常：{ev['exception']}")
    finally:
        if not skipped:
            _teardown(ev, st)

    _finalize(ev, checks, problems, force_verdict="SKIP" if skipped else None)

    if skipped:
        print(f"[pg][WARN] 跳过：{ev['reason']}")
        print("[pg][WARN] 本次**没有**验证 PostgreSQL —— 判定为 SKIP，不是 PASS。")
        print("[pg][WARN] 先跑 scripts/pg-local-ascii.ps1 -Action init 建好实例；"
              "门禁/CI 请设 PG_REQUIRE=1 让跳过变成失败。")
        return 1 if os.environ.get("PG_REQUIRE") == "1" else 0

    print(f"\n[pg] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")
    print(f"[pg] 服务端版本: {ev.get('server_version')}")
    print(f"[pg] 表行数: {ev.get('table_counts')}")
    print(f"[pg] 清理: {ev.get('cleanup')}")
    print(f"[pg] 证据: {EVIDENCE}")
    return 0 if rc == 0 and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
