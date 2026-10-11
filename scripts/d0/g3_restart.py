#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
G3 门禁：断点续接（跨进程 / 跨重启）

真实场景：run 在人工节点暂停 -> 进程被杀 -> 重启后仍能恢复。
本脚本用「三个独立进程」证明这一点，而不是同进程内的伪恢复：
    phase=start   进程1：定义工作流、执行、在 human 节点暂停、记录 run_id、退出（模拟被杀）
    phase=resume  进程2：全新进程 + 全新数据库连接，从 SqliteSaver 读取暂停点并恢复 -> 完成
    phase=verify  进程3：再次全新进程，确认终态可从持久层读到

产出：docs/evidence/d0-g3-restart.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, paths  # noqa: E402

bootstrap()

from agent.workflow import get_workflow_manager  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = PROJECT_ROOT / "workflows" / "d0_restart.json"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "d0-g3-restart.json"
WF_NAME = "d0_restart"


def _published_events():
    """读取总线里本进程收到的 LangGraphLoop 事件（证明暂停通知确实发出）。"""
    from channels.bus import get_bus
    bus = get_bus()
    out = []
    while True:
        msg = bus.get_nowait()
        if msg is None:
            break
        out.append({"type": getattr(msg.event_type, "value", str(msg.event_type)),
                    "content": str(msg.content)[:400]})
    return out


def phase_start(ev):
    manager = get_workflow_manager()
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    wf = manager.define_workflow(WF_NAME, "D0 断点续接", spec)
    assert not wf.last_error, f"编译失败: {wf.last_error}"
    run_id, info = manager.start_run(WF_NAME, {"clause_text": "示例条款"}, {})
    assert run_id, f"启动失败: {info}"

    # 等它暂停
    t0 = time.time()
    while time.time() - t0 < 60:
        run = manager.dao.get_run(run_id)
        if run and run.status != "running":
            break
        time.sleep(0.2)
    run = manager.dao.get_run(run_id)
    events = _published_events()
    ev["phase_start"] = {
        "pid": __import__("os").getpid(),
        "run_id": run_id,
        "status": run.status,
        "loop_kind": run.loop_kind,
        "interrupt_kind": (run.interrupt_info or {}).get("kind"),
        "question": (run.interrupt_info or {}).get("question"),
        "bus_events": events,
        "db_path": paths()["db"],
    }
    print(f"[G3][start] pid={ev['phase_start']['pid']} run_id={run_id} status={run.status} "
          f"interrupt={ev['phase_start']['interrupt_kind']}")
    print(f"[G3][start] 暂停通知事件 {len(events)} 条")
    # 硬退出：不等待后台线程，模拟进程被杀
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.flush()
    __import__("os")._exit(0)


def phase_resume(ev, run_id):
    """在全新进程中从持久层恢复暂停的 run。

    注意：manager.resume_run 经后台线程池执行，因此这里必须等后台真正跑完
    再退出（不能用 os._exit 直接砍掉，否则进程被杀在恢复完成之前，
    DB 里的 status 会停留在 paused，看起来像"恢复失败"）。
    """
    os_mod = __import__("os")
    manager = get_workflow_manager()
    run_row = manager.dao.get_run(run_id)
    ev["phase_resume"] = {"pid": os_mod.getpid(), "run_id_before": run_id,
                          "status_before": run_row.status if run_row else None,
                          "workflow_id": run_row.workflow_id if run_row else None}
    print(f"[G3][resume] pid={ev['phase_resume']['pid']} 重启后读到 run 状态={run_row.status}")

    # 走「原子抢占 + 服务端恢复」这条本项目要正式采用的路子（见 mcp_server/runtime_store.py）
    from mcp_server.runtime_store import claim_run, release_run, get_claim
    owner = f"g3-resume-{os_mod.getpid()}"
    claimed = claim_run(run_id, owner)
    ev["phase_resume"]["claimed"] = claimed
    if not claimed:
        ev["phase_resume"]["error"] = "抢占失败（可能被其他审批人占用）"
        EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        os_mod._exit(2)

    info = manager.resume_run(run_id, "continue", {})
    ev["phase_resume"]["resume_info"] = info

    # 等后台线程把 run 推到终态
    t0 = time.time()
    while time.time() - t0 < 60:
        run = manager.dao.get_run(run_id)
        if run and run.status != "running":
            break
        time.sleep(0.3)
    run = manager.dao.get_run(run_id)
    ev["phase_resume"]["manager_path_status"] = run.status
    ev["phase_resume"]["manager_path_error"] = run.error

    # 若后台线程池路径失败，则改用「同进程内同步恢复」——
    # 这正是管理器内部实际执行的代码路径（WorkflowRunner.resume），
    # 只是不经过线程池调度，因此可以拿到真实异常并留证。
    if run.status != "completed":
        print(f"[G3][resume] 线程池路径未完成（status={run.status}），改用同步路径并留证异常")
        from mcp_server.runtime_store import release_run as _rel
        _rel(run_id, owner)
        wf = manager.get_workflow("d0_restart")
        g = manager._get_graph(wf)
        try:
            r = manager.runner.resume(g, run_id, wf.workflow_id, "continue", {})
            ev["phase_resume"]["sync_path_result"] = {"status": r.get("status"),
                                                     "kind": r.get("payload", {}).get("kind")}
            manager._update_run_from_result(run_id, r, wf.workflow_id)
            print(f"[G3][resume] 同步路径结果: {r.get('status')}")
        except Exception as exc:
            import traceback
            ev["phase_resume"]["sync_path_exception"] = {
                "type": type(exc).__name__, "msg": str(exc),
                "trace": traceback.format_exc()[-1500:],
            }
            print(f"[G3][resume] 同步路径异常: {type(exc).__name__}: {exc}")
            manager.dao.update_run_status(run_id, "error", error=str(exc))
    # 终态一律以「最后重新读一次数据库」为准。
    # 【D0 实测重要发现】manager.resume_run 的线程池路径在跨进程恢复时，
    # run 记录可能长时间停留在 paused（_update_run_from_result 的写入观测不到），
    # 而同一进程内的同步路径（WorkflowRunner.resume + _update_run_from_result）
    # 能可靠把记录推到 completed。因此服务端恢复入口应采用「原子抢占 + 同步恢复」，
    # 这也正是 mcp_server/runtime_store.py 与 P4 待办队列的设计依据。
    run = manager.dao.get_run(run_id)
    ev["phase_resume"].update({
        "status_after": run.status,
        "output": run.output,
        "error": run.error,
        "claim_during": get_claim(run_id),
        "bus_events": _published_events(),
    })
    release_run(run_id, owner)
    ev["phase_resume"]["claim_after_release"] = get_claim(run_id)

    # 同进程内的 checkpoint 复核（在关闭线程池之前做）
    wf = manager.get_workflow("d0_restart")
    g = manager._get_graph(wf)
    st = g.get_state({"configurable": {"thread_id": run_id}})
    ev["phase_resume"]["checkpoint_after"] = {
        "next": list(st.next) if st else [],
        "values_keys": sorted((st.values or {}).keys()) if st else [],
    }
    print(f"[G3][resume] 恢复后状态={run.status} output_keys={sorted((run.output or {}).keys())} "
          f"checkpoint_next={ev['phase_resume']['checkpoint_after']['next']}")

    ev["verdict"] = "PASS" if run.status == "completed" and not (st.next if st else None) else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.flush()
    manager.executor.shutdown()
    print(f"[G3] 判定: {ev['verdict']}")
    return


def phase_verify(ev, run_id):
    manager = get_workflow_manager()
    if not run_id:
        # 【审查修正 P2】原实现直接用命令行传入的 run_id（默认空串）：
        # 单独执行 `--phase verify` 时会拿到 None 并抛
        # `AttributeError: 'NoneType' object has no attribute 'workflow_id'`，
        # 让人误以为框架/服务有问题。现在缺省回退到本次 start 阶段记录的 run_id，
        # 并在确实找不到时给出明确错误（写进证据 + 非 0 退出）。
        run_id = ((ev.get("phase_start") or {}).get("run_id") or "")
    run = manager.dao.get_run(run_id) if run_id else None
    if run is None:
        ev["phase_verify"] = {
            "pid": __import__("os").getpid(),
            "run_id": run_id,
            "error": "run 记录不存在（可能已被清理，或未先执行 --phase start）",
        }
        ev["verdict"] = "FAIL"
        print(f"[G3][verify] 失败：run 记录不存在（run_id={run_id or '未提供'}）")
        EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    wf = manager.get_workflow(run.workflow_id)
    graph = manager._get_graph(wf)
    state = graph.get_state({"configurable": {"thread_id": run_id}})
    ev["phase_verify"] = {
        "pid": __import__("os").getpid(),
        "run_id": run_id,
        "status": run.status,
        "output": run.output,
        "checkpoint_values_keys": sorted((state.values or {}).keys()) if state else [],
        "checkpoint_next": list(state.next) if state else [],
        "checkpoint_config_present": bool(state and state.config),
        "completed_at": str(run.completed_at),
    }
    print(f"[G3][verify] pid={ev['phase_verify']['pid']} 状态={run.status} "
          f"next={ev['phase_verify']['checkpoint_next']} 已完成时间={run.completed_at}")
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["start", "resume", "verify"])
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    ev_path = EVIDENCE
    ev = json.loads(ev_path.read_text(encoding="utf-8")) if ev_path.exists() else {
        "gate": "G3", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    if args.phase == "start":
        # 每次从干净状态开始：清掉上一次的 run 记录，避免混淆
        manager = get_workflow_manager()
        if manager:
            for r in manager.dao.list_runs(workflow_id=WF_NAME, status=None, limit=100):
                manager._cleanup_run(r.run_id)
        phase_start(ev)
        return 0
    if args.phase == "resume":
        assert args.run_id, "resume 需要 --run-id"
        phase_resume(ev, args.run_id)
        # phase_resume 内部已写入 verdict 并以自身返回码体现
        return 0 if ev.get("verdict") == "PASS" else 1
    phase_verify(ev, args.run_id)
    return 0 if ev.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
