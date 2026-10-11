#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
G6 门禁：并发恢复互斥（两个入口同时 resume 同一 run）

背景（实读结论）：框架的 WorkflowManager.resume_run 是
    get_run -> 判断 status == "paused" -> executor.submit
（agent/workflow/manager.py:199-214），读取与提交之间**没有原子抢占**。
两个并发请求都可能通过检查并各自提交，随后用同一个 thread_id 并发跑同一张图。

本脚本用一个真实复现来量化这个窗口，并验证「原子抢占」这一补救措施是否有效：
    A 组：并发（多线程 + 进程级）直接调用 resume_run，观察是否出现多次执行
    B 组：加入原子抢占（UPDATE ... WHERE status='paused'），观察是否只有一次生效

注意：B 组的原子抢占是**本项目在自身代码里**实现的（不改框架源码），
      D0 只验证该机制在 SQLite 下可用；PG 版本在 P2 接入。

产出：docs/evidence/d0-g6-concurrency.json
"""

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, paths  # noqa: E402

bootstrap()

from agent.workflow import get_workflow_manager  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = PROJECT_ROOT / "workflows" / "d0_restart.json"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "d0-g6-concurrency.json"
DB = Path(paths()["db"])


def claim_run(run_id: str, owner: str) -> bool:
    """原子抢占（调用本项目自有的互斥原语，见 mcp_server/runtime_store.py）。"""
    from mcp_server.runtime_store import claim_run as _claim
    return _claim(run_id, owner)


def release_claim(run_id: str, owner: str = "") -> None:
    from mcp_server.runtime_store import release_run
    release_run(run_id, owner)


def prepare_run(manager, tag: str) -> str:
    """用框架的 manager 正常开一个 run（会产生 DB 记录），等它在 human 节点暂停。"""
    run_id, info = manager.start_run("d0_restart", {"clause_text": "并发用例"}, {})
    assert run_id, f"启动失败: {info}"
    t0 = time.time()
    while time.time() - t0 < 60:
        run = manager.dao.get_run(run_id)
        if run and run.status != "running":
            break
        time.sleep(0.2)
    run = manager.dao.get_run(run_id)
    assert run and run.status == "paused", f"未能暂停: {run}"
    time.sleep(0.5)   # 等后台线程彻底退出
    return run_id


def main() -> int:
    ev = {"gate": "G6", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    manager = get_workflow_manager()
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    wf = manager.define_workflow("d0_restart", "D0 断点续接", spec)
    assert not wf.last_error, wf.last_error
    graph = manager._get_graph(wf)

    # ---------------- A 组：无抢占，并发 resume（预期暴露竞态） ----------------
    run_a = prepare_run(manager, "raw")
    results_a = []
    lock = threading.Lock()

    def hit_raw(i):
        r = manager.resume_run(run_a, f"continue-{i}", {})
        with lock:
            results_a.append({"thread": i, "result": r})

    threads = [threading.Thread(target=hit_raw, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(3)
    accepting = [x for x in results_a if "error" not in x["result"]]
    ev["group_A_no_claim"] = {
        "run_id": run_a,
        "attempts": len(results_a),
        "accepted": len(accepting),
        "rejected": len(results_a) - len(accepting),
        "raw": results_a,
        "run_status_after": manager.dao.get_run(run_a).status,
        "note": "accepted > 1 即证明存在竞态窗口（多个请求都通过了 paused 检查）",
    }
    print(f"[G6][A] 无抢占：{len(results_a)} 次并发 resume，被接受 {len(accepting)} 次")

    # ---------------- B 组：原子抢占（本项目实现，不改框架） ----------------
    run_b = prepare_run(manager, "claim")
    results_b = []

    def claim_and_resume(i):
        """原子抢占 + 在进程内同步恢复（等价于服务端的恢复路径）。

        说明：本组**不使用** manager.resume_run，因为它经后台线程池异步执行，
        状态更新滞后，会污染并发测量；这里直接调用框架的 WorkflowRunner.resume，
        与 manager 内部实际执行的代码路径一致（agent/workflow/manager.py:224-230）。
        """
        owner = f"thread-{i}"
        if not claim_run(run_b, owner):
            with lock:
                results_b.append({"thread": i, "outcome": "rejected(已被其他审批人占用)"})
            return
        try:
            r = manager.runner.resume(graph, run_b, "d0_restart", f"continue-{i}", {})
            with lock:
                results_b.append({"thread": i, "outcome": "accepted",
                                  "runner_status": r.get("status")})
        finally:
            release_claim(run_b, owner)

    threads = [threading.Thread(target=claim_and_resume, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(3)
    accepted_b = [x for x in results_b if x["outcome"] == "accepted"]
    ev["group_B_atomic_claim"] = {
        "run_id": run_b,
        "attempts": len(results_b),
        "accepted": len(accepted_b),
        "rejected": len(results_b) - len(accepted_b),
        "raw": results_b,
        "run_status_after": manager.dao.get_run(run_b).status,
        "note": "accepted == 1 即证明原子抢占有效",
    }
    print(f"[G6][B] 原子抢占：{len(results_b)} 次并发，被接受 {len(accepted_b)} 次")

    # ---------------- C 组：进程级并发（两个独立进程同时 resume） ----------------
    ev["group_C_multiprocess"] = {"skipped": False}
    run_c = prepare_run(manager, "mp")
    script = Path(__file__).resolve()
    import subprocess
    outdir = PROJECT_ROOT / ".runtime" / "logs"
    outdir.mkdir(parents=True, exist_ok=True)
    procs, files = [], []
    for i in range(2):
        # 用文件而非管道承接子进程输出（沙箱下管道捕获子进程输出会被拒）
        fp = outdir / f"g6-mp-{i}.json"
        fh = open(fp, "w", encoding="utf-8")
        files.append((fp, fh))
        procs.append(subprocess.Popen(
            [sys.executable, str(script), "--mp-resume", run_c, f"mp-{i}"],
            stdout=fh, stderr=subprocess.STDOUT,
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        ))
    for p in procs:
        p.wait(timeout=120)
    for fp, fh in files:
        fh.close()
    outs = []
    for (fp, _), p in zip(files, procs):
        outs.append({"rc": p.returncode, "out": fp.read_text(encoding="utf-8").strip()[-400:]})
    accepted_c = [o for o in outs if '"accepted": true' in o["out"]]
    ev["group_C_multiprocess"] = {
        "run_id": run_c, "processes": len(outs), "accepted": len(accepted_c),
        "raw": outs, "run_status_after": manager.dao.get_run(run_c).status,
    }
    print(f"[G6][C] 跨进程并发：{len(outs)} 个进程，被接受 {len(accepted_c)} 次")

    verdicts = {
        "A_race_observed": len(accepting) >= 2,          # 暴露竞态（这是"发现问题"的证据）
        "B_claim_enforces_single": len(accepted_b) == 1,  # 补救有效
        "C_multiprocess_single": len(accepted_c) <= 1,
    }
    ev["verdicts"] = verdicts
    # G6 的通过标准：A 暴露竞态 + B 证明补救有效
    ev["verdict"] = "PASS" if verdicts["A_race_observed"] and verdicts["B_claim_enforces_single"] else "FAIL"
    ev["conclusion"] = (
        "框架原生的 resume_run 在并发下会被多个请求同时接受（竞态真实存在）；"
        "本项目在自身代码里用「状态原子抢占」把并发恢复收敛为一次生效。"
        "该机制须在 P4 正式接入统一待办队列的服务端恢复路径。"
    )
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print("\n[G6] 子项判定:")
    for k, v in verdicts.items():
        print(f"   {k}: {'PASS' if v else 'FAIL'}")
    print(f"[G6] 总判定: {ev['verdict']}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[G6] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


def mp_resume(run_id: str, value: str) -> int:
    """供 C 组调用的子进程入口：原子抢占 + 同步恢复，输出 JSON 结果。"""
    manager = get_workflow_manager()
    owner = f"pid-{__import__('os').getpid()}"
    if not claim_run(run_id, owner):
        print(json.dumps({"accepted": False, "reason": "已被其他进程占用"}, ensure_ascii=False))
        return 0
    try:
        wf = manager.get_workflow("d0_restart")
        graph = manager._get_graph(wf)
        r = manager.runner.resume(graph, run_id, wf.workflow_id, value, {})
        print(json.dumps({"accepted": True, "runner_status": r.get("status")}, ensure_ascii=False))
    finally:
        release_claim(run_id, owner)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--mp-resume":
        sys.exit(mp_resume(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "continue"))
    sys.exit(main())
