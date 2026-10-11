#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P4 端到端：人工闭环服务端化

把 P3 里"脚本扮演服务端"的部分，改为调用正式服务层 `service/review_orchestrator.py`，
并验证 P4 的四项核心承诺：

    1) 两段式执行：判定（能力服务）→ 编排（工作流），低风险自动办结
    2) 待办闭环：人工节点暂停 → 按角色待办 → 认领 → 决策 → 完结 → 恢复
    3) 并发互斥：两个入口同时审批同一 run，**只有一次生效**，后到者得到明确提示
    4) 启动补偿：重启后扫描暂停中的 run，为缺失角色补建待办并留通知审计

额外验证：modify 路径由服务层重新标注并留痕；全程审计事件可回溯。

产出：docs/evidence/p4-human-loop.json

----------------------------------------------------------------------------
【第二次审查修正】本脚本原先有 8 个问题，已全部修：
  1. **假绿的角色越权断言**：B 用例里"专员不得批主管节点"的检查被放在整条 run
     已经 completed **之后**才做 —— 此时 decide 失败的原因是"run 已结束/没有等待中节点"，
     跟角色权限毫无关系（把角色校验整段删掉，这条断言照样通过）。
     现在把该检查移到 dec1 之后、run 正停在主管节点的时刻。
  2. 同一条断言里 `orch.decide(...)` 被调用了**两次**（一次取 ok、一次取 errors）：
     决策是有副作用的操作，重复调用会让证据与真实执行不一致。现在只调一次。
  3. 跨进程互斥（E）只看子进程 stdout 里有没有 `"accepted": true`：
     某个子进程**崩溃**（rc≠0、无输出）时另一进程照样"恰好一次"，判定 PASS。
     现在要求全部子进程 rc=0、输出是合法 JSON，且被拒的那次带可解释的冲突理由。
  4. 本脚本自己起来的 8770 能力服务**从不关闭**（进程泄漏，且影响后续门禁）。
     现在收尾时只在"确实是本脚本起的"前提下停掉，并记入证据。
  5. 全程没有异常保护：中途抛异常就既不停服务、也不落证据。
     现在主体包在 try/except/finally 里。
  6. 子进程日志句柄泄漏；`p.wait(timeout=180)` 超时直接抛异常且**不杀子进程**。
     现在用 with 打开、超时则 kill 并记 `timeout`。
  7. F 用例直接 `sqlite3.connect(get_db_path())` 删待办 —— `DB_MODE=pg` 时待办在
     PostgreSQL 里，删的是另一个库（删不掉也不会报错），用例形同虚设。
     现在统一走 `mcp_server.db.connect()`（按后端分派）。
  8. 指标断言只要求"库里有待办/有在办 run"：本机共享库里已有上千条历史 run，
     **本轮什么都没写进去也成立**（假绿）。现在要求本轮那个暂停 run 确实在待办列表里；
     并在收尾时把本轮创建的 run 数据（含框架运行表与 langgraph 检查点）回收掉。
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, paths, port_pid, stop_port  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p4-human-loop.json"

CAP_PORT = 8770
CAP_HEALTH = f"http://127.0.0.1:{CAP_PORT}/health"

LOW = ("技术服务合同\n第一条 甲方委托乙方提供运维服务，期限一年。\n"
       "第二条 甲方在验收合格后 30 日内支付服务费。\n"
       "第三条 双方按约定时间交付，验收标准以附件一为准。\n")
MID = ("技术服务合同\n第一条 甲方委托乙方提供运维服务，期限一年。\n"
       "第二条 乙方违反保密义务的，违约金为合同总额的 50%。\n"
       "第三条 甲方在验收合格后 30 日内支付服务费。\n")
HIGH = ("劳动合同\n第一条 本合同为固定期限劳动合同，期限三年。\n"
        "第二条 乙方月工作时长不超过 280 小时，甲方可安排加班。\n"
        "第三条 甲方有权无条件解除本合同，最终解释权归甲方所有。\n"
        "第四条 本产品保本，年化收益不低于 5%。\n")

# 并发/越权类拒绝必须是**可解释的冲突**，而不是"碰巧失败"
CONFLICT_HINTS = ("已被", "不匹配", "没有等待中", "状态为")
PERMISSION_HINTS = ("角色", "不匹配", "无权", "需要", "没有等待中", "已被")


def ensure_capability_service() -> tuple[subprocess.Popen | None, dict]:
    """确保岗位能力服务在线（图里的 MCP 工具依赖它，顺序不能颠倒）。

    返回 (本脚本启动的进程或 None, 健康信息)。
    """
    import urllib.request
    try:
        with urllib.request.urlopen(CAP_HEALTH, timeout=2) as r:
            return None, json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    log = PROJECT_ROOT / ".runtime" / "logs" / "p4-mcp-server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:        # with：不留句柄
        fh.write(f"\n===== P4 启动能力服务 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        fh.flush()
        proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py"),
             "--port", str(CAP_PORT)],
            stdout=fh, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8",
                 "PYTHONPATH": str(PROJECT_ROOT)})
    t0 = time.time()
    while time.time() - t0 < 25:
        try:
            with urllib.request.urlopen(CAP_HEALTH, timeout=2) as r:
                return proc, json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("能力服务启动失败，见 .runtime/logs/p4-mcp-server.log")


def cleanup_runs(run_ids: list[str]) -> dict:
    """回收本轮创建的运行数据（尽力而为，结果记入证据）。

    为什么做：本脚本会在**共享工作区库**上创建 run/待办/审计（本机该库已有 1500+
    条历史 run）。断言现已收敛到"只看本轮数据"，这里再把数据收掉，
    既少污染演示数据，也让"重复运行"不会逐轮累积。
    框架的 langgraph 检查点表按 `thread_id = run_id` 清理。
    """
    out: dict = {"requested_runs": len(run_ids), "deleted": {}, "notes": []}
    if not run_ids:
        out["notes"].append("本轮没有创建 run，无需清理")
        return out
    try:                                   # 本项目自有表：按 DB_MODE 分派（sqlite / pg）
        from mcp_server import db as db_mod
        conn = db_mod.connect()
        try:
            for tbl in ("compliance_audit", "compliance_todo"):
                n = 0
                for rid in run_ids:
                    cur = conn.execute(f"DELETE FROM {tbl} WHERE run_id=?", (rid,))
                    n += getattr(cur, "rowcount", 0) or 0
                out["deleted"][tbl] = n
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        out["notes"].append(f"compliance 表清理失败：{type(exc).__name__}: {exc}")
    try:                                   # 框架表 / langgraph 检查点（框架按设计只写 sqlite）
        import sqlite3
        from dao.db import get_db_path
        c = sqlite3.connect(str(get_db_path()), timeout=30.0)
        try:
            for tbl, col in (("writes", "thread_id"), ("checkpoints", "thread_id"),
                             ("workflow_runs", "run_id")):
                n = 0
                for rid in run_ids:
                    cur = c.execute(f"DELETE FROM {tbl} WHERE {col}=?", (rid,))
                    n += cur.rowcount or 0
                out["deleted"][tbl] = n
            c.commit()
        finally:
            c.close()
    except Exception as exc:
        out["notes"].append(f"框架表清理失败：{type(exc).__name__}: {exc}")
    return out


def _finalize(ev: dict, checks: dict, problems: list) -> None:
    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ev["paths"] = paths()
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")


def main() -> int:
    ev: dict = {"flow": "P4-human-loop", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cases": {}}
    checks: dict[str, dict] = {}
    problems: list[str] = []
    cap_proc: subprocess.Popen | None = None
    run_ids: list[str] = []
    rc = 1

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    def track(result: dict) -> dict:
        """登记本轮创建的 run（收尾时回收）。"""
        rid = (result or {}).get("run_id")
        if rid:
            run_ids.append(rid)
        return result

    def _is_conflict(r, hints=CONFLICT_HINTS) -> bool:
        msg = " ".join(r.get("errors") or [])
        return any(h in msg for h in hints)

    try:
        cap_proc, health = ensure_capability_service()
        ev["capability_service"] = {"started_by_this_run": cap_proc is not None,
                                    "pid": cap_proc.pid if cap_proc else port_pid(CAP_PORT),
                                    "health": health}
        print(f"[P4] 能力服务: {health.get('status')} v{health.get('version')}"
              f"（本脚本启动={cap_proc is not None}）")

        from service.review_orchestrator import ReviewOrchestrator
        orch = ReviewOrchestrator()
        compiled = orch.ensure_compiled()
        ev["compiled"] = compiled
        check("P4_compiled_with_capability_service",
              compiled["capability_service"].get("status") == "running",
              compiled)
        print(f"[P4] 工作流 {compiled['workflow_id']} v{compiled['version']} 编译完成")

        # ---------------- A 低风险自动办结 ----------------
        ra = track(orch.submit(LOW, submitted_by="oa-bot"))
        st_a = orch.status(ra["run_id"])
        check("A_low_auto_archive",
              ra["ok"] and ra["status"] == "completed" and ra["risk_level"] == "low"
              and not ra["paused"]
              and any(e["event"] == "review_archived" for e in st_a["audit"]),
              {"status": ra["status"], "risk": ra["risk_level"], "paused": ra["paused"],
               "audit_events": [e["event"] for e in st_a["audit"]]})
        print(f"[P4][A] 低风险: {ra['status']} 风险={ra['risk_level']} 待办={ra['paused']} "
              f"审计={[e['event'] for e in st_a['audit']]}")
        ev["cases"]["A_low"] = {"submit": ra, "status": st_a}

        # ---------------- B 中风险：待办 → 专员 → 主管 ----------------
        rb = track(orch.submit(MID, submitted_by="oa-bot"))
        pending_sp = orch.pending(role="specialist", status="open")
        todo_sp = [t for t in pending_sp["todos"] if t["run_id"] == rb["run_id"]]
        dec1 = orch.decide(rb["run_id"], "confirm", "specialist-li", "specialist",
                           note="中风险确认")
        # 【第二次审查修正】越权断言必须在"run 正停在主管节点"时做。
        # 原来放在 dec2（run 已 completed）之后：失败原因是"run 已结束"，
        # 与角色权限无关 —— 等于这条安全断言从来没被真正验过。
        dec_deny = orch.decide(rb["run_id"], "approve", "specialist-li", "specialist")
        pending_sv = orch.pending(role="supervisor", status="open")
        todo_sv = [t for t in pending_sv["todos"] if t["run_id"] == rb["run_id"]]
        dec2 = orch.decide(rb["run_id"], "approve", "supervisor-zhang", "supervisor",
                           note="同意归档")
        st_b = orch.status(rb["run_id"])
        events_b = [e["event"] for e in st_b["audit"]]
        check("B_todo_closed_loop",
              rb["paused"] and rb["paused"]["role"] == "specialist" and len(todo_sp) == 1
              and dec1["ok"] and dec1["status"] == "paused"
              and len(todo_sv) == 1 and dec2["ok"] and dec2["status"] == "completed",
              {"paused_role": (rb["paused"] or {}).get("role"), "sp_todo": len(todo_sp),
               "dec1": {k: dec1.get(k) for k in ("ok", "status")},
               "sv_todo": len(todo_sv), "dec2": {k: dec2.get(k) for k in ("ok", "status")}})
        check("B_audit_chain",
              all(e in events_b for e in ("review_submitted", "human_decision",
                                          "review_archived")),
              {"events": events_b})
        check("B_role_permission_enforced",
              dec_deny.get("ok") is False and bool(dec_deny.get("errors"))
              and _is_conflict(dec_deny, PERMISSION_HINTS),
              {"denied_when_supervisor_node_waiting": True,
               "ok": dec_deny.get("ok"), "errors": dec_deny.get("errors"),
               "run_status_after_deny": dec_deny.get("status")})
        print(f"[P4][B] 中风险: 专员待办={len(todo_sp)} 主管待办={len(todo_sv)} "
              f"越权拒绝={dec_deny.get('errors')} 最终={dec2['status']} 审计={events_b}")
        ev["cases"]["B_mid"] = {"submit": rb, "decisions": [dec1, dec2],
                               "denied_cross_node": dec_deny, "status": st_b,
                               "todo_specialist": todo_sp, "todo_supervisor": todo_sv}

        # ---------------- C 并发审批互斥（同一 run 两个入口） ----------------
        rcv = track(orch.submit(MID, submitted_by="oa-bot"))
        results = []
        lock = threading.Lock()

        def approve(i):
            r = orch.decide(rcv["run_id"], "confirm", f"specialist-{i}", "specialist",
                            note=f"并发提交 {i}")
            with lock:
                results.append({"i": i, "ok": r.get("ok"), "claimed": r.get("claimed"),
                                "errors": r.get("errors"), "status": r.get("status")})

        threads = [threading.Thread(target=approve, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        accepted = [r for r in results if r["ok"]]
        rejected = [r for r in results if not r["ok"]]
        # 真正要守住的不变量：并发下**只有一次生效**，其余都拿到"可解释的拒绝"。
        # 【审查修正说明】跨节点决策绑定上线后，先到的那次已经推进了图，
        # 后到的 3 次可能在"认领"之前就被节点绑定拦下，拒绝理由是"与当前等待节点不匹配"
        # （比"已被占用"更准确）。因此这里断言"拒绝理由属于已知的冲突类"，而不是死抠某一句话。
        check("C_concurrent_single_accept",
              len(accepted) == 1 and len(rejected) == 3
              and all(_is_conflict(r) for r in rejected),
              {"accepted": len(accepted), "rejected": len(rejected),
               "reject_msgs": [r["errors"] for r in rejected]})
        print(f"[P4][C] 并发审批: 接受 {len(accepted)} 次，拒绝 {len(rejected)} 次"
              f"（拒绝理由示例：{(rejected[0]['errors'] or [''])[0] if rejected else 'n/a'}）")
        ev["cases"]["C_concurrency"] = {"run_id": rcv["run_id"], "results": results,
                                        "accepted": len(accepted), "rejected": len(rejected)}

        # ---------------- D modify：服务层重新标注 ----------------
        rd = track(orch.submit(HIGH, submitted_by="oa-bot"))
        dec_m = orch.decide(rd["run_id"], "modify", "specialist-li", "specialist",
                            note="要求重新标注收益条款")
        st_d = orch.status(rd["run_id"])
        events_d = [e["event"] for e in st_d["audit"]]
        dec_ok = orch.decide(rd["run_id"], "confirm", "specialist-li", "specialist", note="确认")
        dec_sup = orch.decide(rd["run_id"], "approve", "supervisor-zhang", "supervisor",
                              note="签署")
        check("D_modify_reannotates",
              dec_m["ok"] and dec_m["status"] == "paused" and "reannotated" in events_d
              and dec_ok["ok"] and dec_sup["ok"] and dec_sup["status"] == "completed",
              {"modify_status": dec_m["status"], "events": events_d,
               "final": dec_sup.get("status")})
        print(f"[P4][D] modify: 重新标注事件={'reannotated' in events_d} "
              f"最终={dec_sup.get('status')} 审计={events_d}")
        ev["cases"]["D_modify"] = {"submit": rd, "modify": dec_m, "confirm": dec_ok,
                                   "supervisor": dec_sup, "status": st_d}

        # ---------------- E 并发跨进程互斥（子进程各抢一次） ----------------
        re_ = track(orch.submit(MID, submitted_by="oa-bot"))
        script = Path(__file__).resolve()
        outdir = PROJECT_ROOT / ".runtime" / "logs"
        outdir.mkdir(parents=True, exist_ok=True)
        procs, files = [], []
        for i in range(2):
            fp = outdir / f"p4-mp-{i}.json"
            fh = open(fp, "w", encoding="utf-8")
            files.append((fp, fh))
            procs.append(subprocess.Popen(
                [sys.executable, str(script), "--mp-decide", re_["run_id"], f"mp-{i}"],
                stdout=fh, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
                env={**os.environ, "PYTHONIOENCODING": "utf-8",
                     "PYTHONPATH": str(PROJECT_ROOT)}))
        outs = []
        for (fp, fh), p in zip(files, procs):
            # 【第二次审查修正】原来 `p.wait(timeout=180)` 超时直接抛异常：
            # 既不杀子进程也不落证据。现在超时就 kill，并把它记成一次明确失败。
            timed_out = False
            try:
                p.wait(timeout=180)
            except subprocess.TimeoutExpired:
                timed_out = True
                p.kill()
                try:
                    p.wait(timeout=30)
                except Exception:
                    pass
            fh.close()
            raw = fp.read_text(encoding="utf-8", errors="replace").strip()
            parsed = None
            for line in reversed(raw.splitlines()):
                try:
                    parsed = json.loads(line)
                    break
                except Exception:
                    continue
            outs.append({"rc": p.returncode, "timeout": timed_out, "parsed": parsed,
                         "out": raw[-300:]})
        # 【第二次审查修正】原来只扫 stdout 里有没有 "accepted": true：
        # 某个子进程崩溃（rc≠0、没有输出）时，另一个照样"恰好一次"→ PASS。
        # 现在要求：两个子进程都正常退出、都给出合法 JSON、恰好一个 accepted、
        # 另一个带可解释的冲突理由。
        oks = [o for o in outs if o["parsed"] is not None and o["parsed"].get("accepted") is True]
        rejects = [o for o in outs if o["parsed"] is not None
                   and o["parsed"].get("accepted") is not True]
        all_healthy = all(o["rc"] == 0 and not o["timeout"] and o["parsed"] is not None
                          for o in outs)
        rejects_explained = all(_is_conflict(o["parsed"]) for o in rejects)
        check("E_multiprocess_single_accept",
              all_healthy and len(oks) == 1 and len(rejects) == 1 and rejects_explained,
              {"processes": len(outs), "accepted": len(oks), "all_healthy": all_healthy,
               "rejects_explained": rejects_explained,
               "outs": [{"rc": o["rc"], "timeout": o["timeout"], "parsed": o["parsed"]}
                        for o in outs]})
        print(f"[P4][E] 跨进程并发: {len(outs)} 进程，接受 {len(oks)} 次，"
              f"子进程全部正常={all_healthy}")
        ev["cases"]["E_multiprocess"] = {"run_id": re_["run_id"], "accepted": len(oks),
                                         "all_healthy": all_healthy, "outs": outs}

        # ---------------- F 启动补偿（重启后补发通知） ----------------
        rf = track(orch.submit(MID, submitted_by="oa-bot"))
        # 清掉该 run 的待办，模拟"重启前待办丢失/未生成"
        # 【第二次审查修正】原来直接 sqlite3.connect(get_db_path())：
        # DB_MODE=pg 时待办在 PostgreSQL 里，这行删的是另一个库（删不掉也不报错），
        # 整个 F 用例在 PG 后端下形同虚设。现在走统一连接层。
        from mcp_server import db as db_mod
        conn = db_mod.connect()
        try:
            conn.execute("DELETE FROM compliance_todo WHERE run_id = ?", (rf["run_id"],))
            conn.commit()
        finally:
            conn.close()
        rec = orch.recover_on_startup()
        st_f = orch.status(rf["run_id"])
        events_f = [e["event"] for e in st_f["audit"]]
        rebuilt = [n for n in rec["notified"] if n["run_id"] == rf["run_id"]]
        check("F_startup_recovery_notifies",
              rf["paused"] and len(rebuilt) == 1 and "notified_on_startup" in events_f
              and rebuilt[0]["role"] == "specialist",
              {"rebuilt": rebuilt, "events": events_f,
               "pending_total": rec["pending_runs"]})
        print(f"[P4][F] 启动补偿: 扫描 {rec['pending_runs']} 个暂停 run，"
              f"为该 run 补建待办={len(rebuilt)} 条，"
              f"审计含通知事件={'notified_on_startup' in events_f}")
        ev["cases"]["F_startup"] = {"recovery": rec, "rebuilt": rebuilt,
                                    "status": {k: st_f[k] for k in ("status", "risk_level")}}

        # ---------------- G 指标（必须体现**本轮**的数据） ----------------
        m = orch.metrics()
        pending_open = orch.pending(role="specialist", status="open", limit=200)
        rf_pending = [t for t in pending_open["todos"] if t["run_id"] == rf["run_id"]]
        # 【第二次审查修正】原来只要求"库里有待办、有在办 run"：共享库里已有上千条
        # 历史 run/待办，本轮一行不写也能通过（假绿）。现在要求本轮那个暂停 run
        # 确实出现在待办列表里。
        check("G_metrics_available",
              bool(m.get("ok")) and m.get("todo_total", 0) >= 1
              and m.get("pending_runs", 0) >= 1 and len(rf_pending) == 1,
              {"todo_total": m.get("todo_total"), "pending_runs": m.get("pending_runs"),
               "by_decision": m.get("by_decision"),
               "this_run_pending_todo": [t["run_id"] for t in rf_pending]})
        print(f"[P4][G] 指标: 待办 {m.get('todo_total')} 条（在办 run {m.get('pending_runs')}），"
              f"决策分布 {m.get('by_decision')}，本轮待办在列={len(rf_pending) == 1}")
        ev["metrics"] = m
        rc = 0
    except Exception as exc:
        import traceback
        ev["exception"] = f"{type(exc).__name__}: {exc}"
        ev["traceback_tail"] = traceback.format_exc()[-2000:]
        if "unexpected_exception" not in problems:
            problems.append("unexpected_exception")
        print(f"[P4] 未预期异常：{ev['exception']}")
    finally:
        # 收尾：回收本轮 run 数据 + 只关掉"本脚本起的"能力服务
        ev["cleanup"] = cleanup_runs(run_ids)
        if cap_proc is not None:
            ev["capability_service"]["stopped_by_this_run"] = bool(
                stop_port(CAP_PORT, why="p4 收尾（本脚本启动的服务）").get("stopped"))

    _finalize(ev, checks, problems)

    print(f"\n[P4] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")
    print(f"[P4] 回收: {ev.get('cleanup', {}).get('deleted')}")
    print(f"[P4] 证据: {EVIDENCE}")
    return 0 if rc == 0 and not problems else 1


def mp_decide(run_id: str, actor: str) -> int:
    """子进程入口：独立进程里尝试审批同一 run，输出 JSON 结果。"""
    from service.review_orchestrator import ReviewOrchestrator
    orch = ReviewOrchestrator()
    r = orch.decide(run_id, "confirm", actor, "specialist", note=f"跨进程 {actor}")
    print(json.dumps({"accepted": bool(r.get("ok")), "errors": r.get("errors"),
                      "status": r.get("status")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--mp-decide":
        sys.exit(mp_decide(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "mp"))
    sys.exit(main())
