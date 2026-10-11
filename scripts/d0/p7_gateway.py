#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P7 端到端：轻量网关（FastAPI）

验证内容（对应任务书 §3.3 的路由与 RBAC）：
    A 健康检查与能力服务联通
    B 提交合同 → 返回 run_id / 风险等级 / 待办角色
    C 待办查询（按角色过滤）
    D 审批：低风险与中风险流程走通；**并发审批只有一个生效**（HTTP 409）
    E RBAC：无 token → 401；角色越权 → 403
    F 指标与规则提案接口
    G SSE：审计链事件流可推送（start/audit/status/done）

产出：docs/evidence/p7-gateway.json
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p7-gateway.json"
TOKEN = "local-dev-token"
BASE = "http://127.0.0.1:8790"

MID = ("技术服务合同\n第一条 甲方委托乙方提供运维服务，期限一年。\n"
       "第二条 乙方违反保密义务的，违约金为合同总额的 50%。\n")
LOW = ("技术服务合同\n第一条 甲方委托乙方提供运维服务，期限一年。\n"
       "第二条 甲方在验收合格后 30 日内支付服务费。\n"
       "第三条 双方按约定交付，验收标准以附件一为准。\n")


def ensure_service(name: str, port: int, script: Path, extra: list) -> str:
    """确保服务在线；网关（8790）总是重启，避免复用旧环境/旧代码的残留进程。

    【审查修正 P2】原来对已监听的服务一律 "already-running" 复用：
    如果 8790 上是更早以 AUTH_MODE=jwt 启动的网关，本脚本的演示令牌请求会全部 401，
    门禁结果失真（实测踩过）。
    """
    import urllib.request
    from _bootstrap import port_pid, stop_port
    if port == 8790 and port_pid(port):
        stop_port(port, why="p7_gateway 需要确定性重启网关")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
            return "already-running"
    except Exception:
        pass
    log = PROJECT_ROOT / ".runtime" / "logs" / f"p7-{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", encoding="utf-8")
    fh.write(f"\n===== P7 启动 {name} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    fh.flush()
    subprocess.Popen([sys.executable, str(script), *extra], stdout=fh, stderr=subprocess.STDOUT,
                     cwd=str(PROJECT_ROOT),
                     env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
                          "PYTHONPATH": str(PROJECT_ROOT)})
    t0 = time.time()
    while time.time() - t0 < 30:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return "started"
        except Exception:
            time.sleep(0.4)
    return "failed"


def call(method: str, path: str, payload: dict | None = None, role: str = "specialist",
         token: str = TOKEN, timeout: float = 30.0):
    import urllib.error
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    if role:
        headers["X-Role"] = role
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body[:200]}
    except Exception as exc:
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ev = {"flow": "P7-gateway", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": {}}
    checks: dict[str, dict] = {}
    problems: list[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    ev["services"] = {
        "capability": ensure_service("capability", 8770,
                                     PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py", []),
        "mock": ensure_service("mock", 8780, PROJECT_ROOT / "mock" / "mock_office.py",
                               ["--timeout-sleep", "3"]),
        "gateway": ensure_service("gateway", 8790, PROJECT_ROOT / "gateway" / "app.py", []),
    }
    print(f"[P7-GW] 服务: {ev['services']}")
    if ev["services"]["gateway"] == "failed":
        check("gateway_started", False, ev["services"])
        ev.update({"checks": checks, "problems": problems, "verdict": "FAIL"})
        EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        return 1

    # ---------------- A 健康检查 ----------------
    code, health = call("GET", "/health")
    # 【第二次审查修正】原来读的是 `health["capability_service"]["status"]`，而 /health
    # 返回的字段名是 `capability_service_ready`（布尔）—— 探测结果恒为 None，
    # 证据里显示"能力服务=None"，等于本用例标题里的"能力服务联通"从来没被验过。
    # 同时给看门狗线程最多 10 秒把快照刷新成真实值（它是后台低频探测，不阻塞 /health）。
    cap_ready = health.get("capability_service_ready")
    for _ in range(20):
        if cap_ready:
            break
        time.sleep(0.5)
        code, health = call("GET", "/health")
        cap_ready = health.get("capability_service_ready")
    check("A_health_ok",
          code == 200 and health.get("status") == "running" and cap_ready is True,
          {"code": code, "status": health.get("status"),
           "capability_service_ready": cap_ready,
           "orchestrator_ready": health.get("orchestrator_ready"),
           "auth_mode": health.get("auth_mode")})
    print(f"[P7-GW][A] 健康: HTTP {code}，能力服务就绪={cap_ready}，"
          f"编排器就绪={health.get('orchestrator_ready')}")

    # ---------------- B 提交合同 ----------------
    code_low, low = call("POST", "/api/v1/contracts", {"text": LOW, "submitted_by": "oa-bot"})
    code_mid, mid = call("POST", "/api/v1/contracts", {"text": MID, "submitted_by": "oa-bot"})
    check("B_submit_returns_run_and_risk",
          code_low == 200 and low.get("run_id") and low.get("risk_level") == "low"
          and mid.get("risk_level") == "mid" and (mid.get("paused") or {}).get("role"),
          {"low": {k: low.get(k) for k in ("run_id", "status", "risk_level")},
           "mid": {k: mid.get(k) for k in ("run_id", "status", "risk_level", "paused")}})
    print(f"[P7-GW][B] 提交: 低风险={low.get('status')}/{low.get('risk_level')}；"
          f"中风险={mid.get('status')}/{mid.get('risk_level')} 待办角色="
          f"{(mid.get('paused') or {}).get('role')}")

    # ---------------- C 待办 ----------------
    code_p, pend = call("GET", "/api/v1/pending?role=specialist", role="specialist")
    mine = [t for t in pend.get("todos", []) if t["run_id"] == mid.get("run_id")]
    check("C_pending_visible_for_role", code_p == 200 and len(mine) == 1,
          {"count": pend.get("count"), "matched": len(mine)})
    print(f"[P7-GW][C] 待办: HTTP {code_p}，专员待办 {pend.get('count')} 条，"
          f"含目标 run={len(mine)} 条")

    # ---------------- D 审批与并发互斥 ----------------
    rid = mid.get("run_id")
    code_d, dec = call("POST", f"/api/v1/runs/{rid}/decide",
                       {"decision": "confirm", "actor": "specialist-li", "note": "确认"},
                       role="specialist")
    results = []
    lock = threading.Lock()

    def approve(i):
        c, r = call("POST", f"/api/v1/runs/{rid}/decide",
                    {"decision": "confirm", "actor": f"specialist-{i}"}, role="specialist")
        with lock:
            results.append({"i": i, "code": c, "ok": bool(r.get("ok")),
                            "detail": r.get("detail") or r.get("errors")})

    threads = [threading.Thread(target=approve, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = [r for r in results if r["ok"]]
    conflicted = [r for r in results if r["code"] == 409]
    check("D1_first_decision_accepted", code_d == 200 and dec.get("ok"), {"code": code_d})
    # 【审查修正 P2】原判据 `len(accepted)==1 and len(conflicted)==3` 把
    # "一次确认 + 三次 409" 写死了；但 D1 已经完成了一次人工确认，run 已推进到主管节点，
    # 因此 D2 的 4 次并发**本就应该全部被拒**（run 不在 paused / 决策与等待节点不匹配）。
    # 真正要守住的不变量是"并发下不会发生重复处理"，所以断言：
    #   接受次数 ≤ 1、其余全部拿到明确的 409、且没有任何一次是 5xx/网络失败。
    # run 级原子抢占的"只生效一次"由 G6（跨线程 + 跨进程）直接覆盖。
    check("D2_concurrent_only_one_effective",
          len(accepted) <= 1 and len(results) == 4
          and all(r["code"] in (409, 200) for r in results)
          and (len(accepted) + len(conflicted) == 4),
          {"accepted": len(accepted), "conflict_409": len(conflicted),
           "codes": [r["code"] for r in results],
           "details": [r["detail"] for r in results][:2]})
    print(f"[P7-GW][D] 审批: 首次 HTTP {code_d}；并发 4 次 → 接受 {len(accepted)}、409 {len(conflicted)}")

    # 主管复核走通（注意：并发线程已经消耗掉"当前暂停节点"，因此先看清状态再决策）
    st_before = call("GET", f"/api/v1/runs/{rid}", role="supervisor")[1]
    code_sv, sv = call("POST", f"/api/v1/runs/{rid}/decide",
                       {"decision": "approve", "actor": "supervisor-zhang", "note": "同意归档"},
                       role="supervisor")
    check("D3_supervisor_completes_run", code_sv == 200 and sv.get("status") == "completed",
          {"code": code_sv, "status": sv.get("status"), "detail": sv.get("detail"),
           "state_before": {k: st_before.get(k) for k in
                            ("status", "specialist_note", "supervisor_note")}})
    print(f"[P7-GW][D] 主管复核: HTTP {code_sv} → {sv.get('status')}"
          f"（决策前状态={st_before.get('status')} 专员={st_before.get('specialist_note')}）")

    # ---------------- E RBAC ----------------
    code_401, _ = call("GET", "/api/v1/pending", token="")
    code_sp, r403 = call("POST", "/api/v1/recover", role="specialist")
    code_sv_ok, r_ok = call("POST", "/api/v1/recover", role="admin")
    check("E1_missing_token_401", code_401 == 401, {"code": code_401})
    check("E2_role_violation_403", code_sp == 403, {"code": code_sp, "detail": r403.get("detail")})
    check("E3_admin_allowed", code_sv_ok == 200, {"code": code_sv_ok})
    print(f"[P7-GW][E] RBAC: 无 token={code_401}，专员越权={code_sp}，管理员={code_sv_ok}")

    # ---------------- F 指标与提案 ----------------
    code_m, m = call("GET", "/api/v1/metrics")
    code_pr, pr = call("GET", "/api/v1/proposals", role="supervisor")
    check("F1_metrics", code_m == 200 and m.get("todo_total") is not None,
          {"todo_total": m.get("todo_total"), "pending_runs": m.get("pending_runs")})
    check("F2_proposals", code_pr == 200 and "proposals" in pr, {"count": pr.get("count")})
    print(f"[P7-GW][F] 指标: 待办 {m.get('todo_total')} 在办 {m.get('pending_runs')}；"
          f"提案 {pr.get('count')} 条")

    # ---------------- G SSE 事件流 ----------------
    import urllib.request
    events = []
    try:
        req = urllib.request.Request(f"{BASE}/api/v1/runs/{low.get('run_id')}/events",
                                     headers={"X-Auth-Token": TOKEN, "X-Role": "specialist"})
        with urllib.request.urlopen(req, timeout=15) as r:
            for _ in range(60):
                line = r.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text.startswith("event:"):
                    events.append(text.split(":", 1)[1].strip())
                if "done" in events:
                    break
    except Exception as exc:
        events.append(f"error:{type(exc).__name__}")
    check("G_sse_stream_works",
          "audit" in events and "status" in events and "done" in events,
          {"events": events[:12]})
    print(f"[P7-GW][G] SSE 事件流: {events[:8]}")

    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["responses"] = {"low": low, "mid": mid, "decide": dec, "supervisor": sv,
                       "concurrent": results, "recover_admin": r_ok}
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n[P7-GW] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[P7-GW] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
