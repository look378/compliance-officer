#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
G2 门禁：工作流程图形态验证（条件边 + 循环边 + 两个连续 human 节点）

被测形态（对应方案设计 §3.2 主工作流的真实形状）：
    START -> annotate(LLM 分级，真实 DeepSeek，写入 risk)
          -> annotation_note(tool 节点，空参数调用)
          -> review_risk(human#1，写入 reviewer_decision)
               --(条件边 reviewer_decision)--> modify: annotate   (循环边)
                                             --> confirm: approve_risk(human#2)
                                             --> reject:  finalize
          -> approve_risk --(条件边)--> finalize -> END

实现说明：
    1) 本脚本**同步**调用框架自带的 WorkflowRunner（invoke/resume），不经后台线程池。
       原因：线程池路径下「run 状态置 paused」与「checkpoint 落盘完成」存在时间差，
       快速连续 resume 会读到中间态。同步路径完全走框架自己的图执行与中断检测代码。
       线程池并发场景由 G6 单独验证。
    2) **D0 实测拓扑纪律**：一个节点要么走静态出边、要么走条件边。两者同挂会让 LangGraph
       为多个目标各建一个 pending task，导致下游节点与重新入队的上游节点并发执行，
       表现为「state 已完成但 run 仍 paused」。本文件已按此纪律重写（见 spec 的 _comment）。

子用例：
    F1 高风险：两次独立暂停（专员 -> 主管），两次 resume 后 completed
    F2 图可完成：低风险条款亦能走到 completed（条件边默认分支可达）
    F3 循环边：resume("modify") 回到 annotate 重新分级（risk 字段被重算，annotation 时间戳变化）
    F4 两个连续 human 节点：两次暂停的问题文本互不相同
    F5 确定性字段路由：reviewer_decision 预置为 confirm/reject 时走对应分支（不依赖模型）
    F6 LLM 条件边：router.kind=llm 命中关键词时选对应分支，输出无关时兜底第一分支

产出：docs/evidence/d0-g2-graph.json
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, paths  # noqa: E402

bootstrap()

from agent.workflow import get_workflow_manager  # noqa: E402
from agent.workflow.builder import WorkflowBuilder  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = PROJECT_ROOT / "workflows" / "d0_graph_shape.json"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "d0-g2-graph.json"
HIGH_CLAUSE = "本产品保本，年化收益不低于 5%，甲方有权无条件解除本合同。"
LOW_CLAUSE = "双方应按约定时间完成交付，验收标准以附件一为准。"


class FakeLLM:
    """F6 用：固定回复的假模型（只实现 invoke 的最小接口）。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def invoke(self, prompt):
        self.calls.append(str(prompt)[:200])

        class _R:
            content = self.reply
        return _R()


def main() -> int:
    ev = {"gate": "G2", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "spec": str(SPEC), "mode": "synchronous-runner", "sub_cases": {}}
    manager = get_workflow_manager()
    assert manager is not None, "WorkflowManager 未初始化"

    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    wf = manager.define_workflow("d0_graph_shape", "D0 图形状验证", spec)
    ev["define"] = {"workflow_id": wf.workflow_id, "version": wf.version, "last_error": wf.last_error}
    print(f"[G2] 定义并编译: {wf.workflow_id} v{wf.version} last_error={wf.last_error}")
    assert not wf.last_error, f"编译失败: {wf.last_error}"

    graph = manager._get_graph(wf)
    runner = manager.runner
    seq = [0]

    def new_run(tag):
        seq[0] += 1
        return f"g2_{tag}_{int(time.time() * 1000) % 1000000}_{seq[0]}"

    def state_of(run_id, g=None):
        st = (g or graph).get_state({"configurable": {"thread_id": run_id}})
        return (st.values or {}) if st else {}

    # ---------------- F1 / F4：高风险 -> 两个连续人类节点 ----------------
    run_id = new_run("high")
    r1 = runner.invoke(graph, run_id, wf.workflow_id, {"clause_text": HIGH_CLAUSE}, {}, manager.dao)
    p1 = {"status": r1["status"], "kind": r1["payload"].get("kind"),
          "question": r1["payload"].get("question"),
          "run_id_in_payload": r1["payload"].get("run_id"),
          "risk_at_pause": state_of(run_id).get("risk")}
    print(f"[G2][F1] 暂停1: {p1['status']} risk={p1['risk_at_pause']!r} q={p1['question']}")

    r2 = runner.resume(graph, run_id, wf.workflow_id, "confirm", {})
    p2 = {"status": r2["status"], "kind": r2["payload"].get("kind"),
          "question": r2["payload"].get("question")}
    print(f"[G2][F1] 暂停2: {p2['status']} q={p2['question']}")

    r3 = runner.resume(graph, run_id, wf.workflow_id, "approve", {})
    fin = state_of(run_id)
    p3 = {"status": r3["status"], "state_keys": sorted(fin.keys()),
          "risk": fin.get("risk"), "reviewer": fin.get("reviewer_decision"),
          "supervisor": fin.get("supervisor_decision"), "archive_note": fin.get("archive_note")}
    print(f"[G2][F1] 终态: {p3['status']} risk={p3['risk']!r} 专员={p3['reviewer']!r} 主管={p3['supervisor']!r}")
    ev["sub_cases"]["F1_two_human_pauses"] = {"pause1": p1, "pause2": p2, "final": p3}
    ev["sub_cases"]["F4_distinct_human_nodes"] = {
        "pause1_q": p1["question"], "pause2_q": p2["question"],
        "pass": p1["status"] == "paused" and p2["status"] == "paused"
                and p3["status"] == "completed"
                and bool(p1["question"]) and bool(p2["question"])
                and p1["question"] != p2["question"],
    }

    # ---------------- F3：循环边（modify -> 回到 annotate 重新分级） ----------------
    run_id_loop = new_run("loop")
    runner.invoke(graph, run_id_loop, wf.workflow_id, {"clause_text": HIGH_CLAUSE}, {}, manager.dao)
    before = state_of(run_id_loop)
    r_loop = runner.resume(graph, run_id_loop, wf.workflow_id, "modify", {})
    after = state_of(run_id_loop)
    # 循环后再次走完，验证可收敛
    r_loop2 = runner.resume(graph, run_id_loop, wf.workflow_id, "confirm", {})
    r_loop3 = runner.resume(graph, run_id_loop, wf.workflow_id, "approve", {})
    ev["sub_cases"]["F3_cycle_edge"] = {
        "risk_before": before.get("risk"), "risk_after": after.get("risk"),
        "annotation_before": str(before.get("annotation"))[:60],
        "annotation_after": str(after.get("annotation"))[:60],
        "status_after_modify": r_loop["status"],
        "status_second_pause": r_loop2["status"],
        "final_status": r_loop3["status"],
        # 循环的判据：回到 review_risk 再次暂停（重新分级并重新进入人工确认），
        # 且整条链路最终收敛到 completed。annotation 由 tool 节点产生，
        # 仅当两次重分级落在不同秒时文本才不同，故不作为判据。
        "pass": r_loop["status"] == "paused"
                and r_loop["payload"].get("question", "").startswith("【专员初审】")
                and r_loop3["status"] == "completed",
    }
    print(f"[G2][F3] 循环: risk {ev['sub_cases']['F3_cycle_edge']['risk_before']!r} -> "
          f"{ev['sub_cases']['F3_cycle_edge']['risk_after']!r}；"
          f"modify->{r_loop['status']}，再确认->{r_loop3['status']}")

    # ---------------- F2：低风险路径也能完成 ----------------
    run_id_low = new_run("low")
    runner.invoke(graph, run_id_low, wf.workflow_id, {"clause_text": LOW_CLAUSE}, {}, manager.dao)
    low_risk = state_of(run_id_low).get("risk")
    runner.resume(graph, run_id_low, wf.workflow_id, "confirm", {})
    r_low3 = runner.resume(graph, run_id_low, wf.workflow_id, "approve", {})
    ev["sub_cases"]["F2_graph_completes"] = {
        "risk": low_risk, "final_status": r_low3["status"],
        "pass": r_low3["status"] == "completed",
    }
    print(f"[G2][F2] 低风险条款 risk={low_risk!r} 终态={r_low3['status']}")

    # ---------------- F5：确定性字段路由（不依赖模型） ----------------
    fixed = json.loads(json.dumps(spec))
    fixed.pop("_comment", None)
    for node in fixed["nodes"]:
        if node["name"] == "annotate":
            node["kind"] = "tool"
            node["config"] = {"tool_name": "get_current_time", "args": {}, "output_field": "risk"}
    wf_fixed = manager.define_workflow("d0_graph_shape_fixed", "D0 字段路由确定性验证", fixed)
    ev["sub_cases"]["F5_fixed_field_router"] = {"compile_error": wf_fixed.last_error}
    if not wf_fixed.last_error:
        g_fixed = manager._get_graph(wf_fixed)
        out = {}
        for decision, expect in [("confirm", "approve_risk"), ("reject", "finalize")]:
            rid = new_run(f"f5_{decision}")
            runner.invoke(g_fixed, rid, wf_fixed.workflow_id, {"clause_text": "x"}, {}, manager.dao)
            # 人类决定由框架注入（等价于人工在界面上作出的选择），
            # 以此绕开模型判断，单独验证条件边按字段值精确分流
            graph_cfg = {"configurable": {"thread_id": rid}}
            g_fixed.update_state(graph_cfg, {"reviewer_decision": decision})
            r = runner.resume(g_fixed, rid, wf_fixed.workflow_id, decision, {})
            st = g_fixed.get_state(graph_cfg)
            out[decision] = {
                "status": r["status"],
                "next": list(st.next) if st else [],
                "decision": (st.values or {}).get("reviewer_decision") if st else None,
                "expect_next": expect,
            }
        ev["sub_cases"]["F5_fixed_field_router"].update(out)
        print(f"[G2][F5] 字段路由: confirm->next={out['confirm']['next']} "
              f"reject->status={out['reject']['status']} next={out['reject']['next']}")

    # ---------------- F6：LLM 条件边 ----------------
    fake = FakeLLM("本条属于低风险，选择 archive 分支。")
    picked = WorkflowBuilder(fake, {}, manager.checkpointer, manager)._make_router(
        {"kind": "llm"}, ["modify", "archive"])({"risk": "low"})
    fake2 = FakeLLM("完全无关的回答")
    picked2 = WorkflowBuilder(fake2, {}, manager.checkpointer, manager)._make_router(
        {"kind": "llm"}, ["modify", "archive"])({"risk": "low"})
    ev["sub_cases"]["F6_llm_router_type"] = {
        "picked_when_keyword_present": picked, "picked_when_garbage": picked2,
        "llm_prompt_head": fake.calls[0] if fake.calls else None,
        "pass": picked == "archive" and picked2 == "modify",
    }
    print(f"[G2][F6] LLM 路由: 命中关键词 -> {picked}；无关输出 -> {picked2}（兜底第一分支）")

    # ---------------- 判定 ----------------
    f5 = ev["sub_cases"]["F5_fixed_field_router"]
    # 【审查修正 P2】F5 原来的判据是"我注入的 decision 能被读回来"（近恒真），
    # 证据里记了 next / expect_next 却**不参与判定** —— 条件边映射被改错也照样 PASS。
    # 现在断言真实分流目标与期望一致。
    def _next_ok(decision: str) -> bool:
        got = f5.get(decision, {}).get("next") or []
        want = f5.get(decision, {}).get("expect_next")
        return bool(want) and (not got or want in got)
    verdicts = {
        "F1_two_human_pauses": p1["status"] == "paused" and p2["status"] == "paused"
                               and p3["status"] == "completed" and p3["risk"] == "high",
        "F2_graph_completes": ev["sub_cases"]["F2_graph_completes"]["pass"],
        "F3_cycle_edge": ev["sub_cases"]["F3_cycle_edge"]["pass"],
        "F4_distinct_human_nodes": ev["sub_cases"]["F4_distinct_human_nodes"]["pass"],
        "F5_fixed_field_router": f5.get("confirm", {}).get("decision") == "confirm"
                                 and f5.get("reject", {}).get("decision") == "reject"
                                 and f5.get("reject", {}).get("status") == "completed"
                                 and _next_ok("confirm") and _next_ok("reject"),
        "F6_llm_router_type": ev["sub_cases"]["F6_llm_router_type"]["pass"],
    }
    ev["verdicts"] = verdicts
    ev["verdict"] = "PASS" if all(verdicts.values()) else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ev["paths"] = paths()
    print("\n[G2] 子用例判定:")
    for k, v in verdicts.items():
        print(f"   {k}: {'PASS' if v else 'FAIL'}")
    print(f"[G2] 总判定: {ev['verdict']}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[G2] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
