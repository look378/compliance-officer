#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
第八组自检：**容器交付形态端到端**（docker compose 起的四个服务）

为什么单独一组：
    前面所有自检跑的都是"本机形态"（`.runtime/venv` + SQLite）。交付形态是 docker compose：
    能力服务 + Mock 外部系统 + 网关 + PostgreSQL 四个容器。两者在**进程环境、数据库后端、
    文件路径（cwd）、依赖集合**上都不同 —— 实测正是这些差异藏着 7 个只有真跑容器才会暴露的缺陷
    （缺 PG 驱动、缺底座依赖、构建上下文 3 GB、中文字体路径、新库建表被回滚、
    postgres 健康检查无 start_period、框架库与运行态库分裂），详见 docs/17「容器形态」。

前置：
    1) 本机已装 Docker 并把引擎跑起来
    2) 已按 README §4.2 起好 compose 栈（默认网关在宿主 8790）
    3) 若 8790 已被"本机形态"占用，用端口覆盖文件起在别的端口，然后：
       python scripts/d0/p8_docker_e2e.py --gateway http://127.0.0.1:18790

产出：docs/evidence/p8-docker-e2e.json
用法：python scripts/d0/p8_docker_e2e.py [--gateway URL] [--quiet]
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p8-docker-e2e.json"

LOW = ("技术服务合同\n第一条 甲方委托乙方提供运维服务，期限一年。\n"
       "第二条 甲方在验收合格后 30 日内支付服务费。\n"
       "第三条 双方按约定时间交付，验收标准以附件一为准。\n")
MID = ("技术服务合同\n第一条 甲方委托乙方提供运维服务，期限一年。\n"
       "第二条 乙方违反保密义务的，违约金为合同总额的 50%。\n"
       "第三条 甲方在验收合格后 30 日内支付服务费。\n")

ADMIN_U, ADMIN_P = "admin", "admin12345"          # 首次启动由网关启动钩子自动创建
SPEC_U, SPEC_P = "e2e-specialist", "E2eSpec@2026x"  # 本自检自建，用于验跨节点拦截


def make_caller(gateway: str):
    def call(method: str, path: str, payload=None, token: str = "", role: str = "",
             timeout: int = 90):
        headers = {}
        if role:
            headers["X-Role"] = role            # decide 等接口从 X-Role 头取角色
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(gateway + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:300]}
        except Exception as exc:
            return 0, {"error": f"{type(exc).__name__}: {exc}"}
    return call


def main() -> int:
    ap = argparse.ArgumentParser(description="容器交付形态端到端自检")
    ap.add_argument("--gateway", default="http://127.0.0.1:8790")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    call = make_caller(args.gateway.rstrip("/"))
    ev: dict = {"check": "P8-docker-e2e", "gateway": args.gateway,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    checks: list[dict] = []

    def check(name: str, ok: bool, detail=None) -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": detail})

    # ---------- 1) 容器健康与认证姿态 ----------
    code, health = call("GET", "/health")
    check("容器网关健康且能力服务在线",
          code == 200 and health.get("status") == "running"
          and health.get("capability_service_ready") is True,
          {"status": code, "capability_service_ready": health.get("capability_service_ready"),
           "auth_mode": health.get("auth_mode")})
    code, _ = call("GET", "/api/v1/auth/me", role="admin")
    check("jwt 模式拒绝演示令牌（容器按生产认证模式起）", code == 401, {"status": code})

    # ---------- 2) 首次部署自动建管理员并可登录 ----------
    code, login = call("POST", "/api/v1/auth/login",
                       {"username": ADMIN_U, "password": ADMIN_P})
    admin_token = login.get("access_token", "")
    check("首次部署自动创建管理员并可登录", code == 200 and bool(admin_token), {"status": code})
    code, me = call("GET", "/api/v1/auth/me", token=admin_token)
    check("管理员身份正确", code == 200 and me.get("role") == "admin", {"role": me.get("role")})

    # ---------- 3) 低风险：不生成待办，直接归档 ----------
    code, low = call("POST", "/api/v1/contracts",
                     {"text": LOW, "submitted_by": "docker-e2e"}, token=admin_token)
    check("低风险自动办结且无待办",
          code == 200 and low.get("status") == "completed"
          and low.get("risk_level") == "low" and not low.get("paused"),
          {"status": low.get("status"), "risk": low.get("risk_level")})

    # ---------- 4) 中风险：专员待办 ----------
    code, mid = call("POST", "/api/v1/contracts",
                     {"text": MID, "submitted_by": "docker-e2e"}, token=admin_token)
    rid = mid.get("run_id", "")
    check("中风险暂停在专员节点",
          code == 200 and mid.get("status") == "paused"
          and (mid.get("paused") or {}).get("role") == "specialist",
          {"status": mid.get("status"), "paused": mid.get("paused"), "run_id": rid})

    code, pend = call("GET", "/api/v1/pending?role=specialist", token=admin_token)
    mine = [t for t in pend.get("todos", []) if t["run_id"] == rid]
    check("待办可被按角色取出", code == 200 and len(mine) == 1,
          {"count": pend.get("count"), "matched": len(mine)})

    # ---------- 5) 账号与权限：建专员账号并用其令牌验跨节点拦截 ----------
    call("POST", "/api/v1/users",
         {"username": SPEC_U, "password": SPEC_P, "display_name": "容器自检专员",
          "role": "specialist"}, token=admin_token)
    code, slog = call("POST", "/api/v1/auth/login", {"username": SPEC_U, "password": SPEC_P})
    spec_token = slog.get("access_token", "")
    check("可创建专员账号并以专员身份登录", code == 200 and bool(spec_token), {"status": code})
    code, sme = call("GET", "/api/v1/auth/me", token=spec_token)
    check("专员令牌角色正确", code == 200 and sme.get("role") == "specialist",
          {"role": sme.get("role")})

    code, d1 = call("POST", f"/api/v1/runs/{rid}/decide",
                    {"decision": "confirm", "actor": SPEC_U, "note": "容器自检-专员确认"},
                    token=spec_token, role="specialist")
    check("专员确认后转主管复核（未直接办结）",
          code == 200 and d1.get("status") == "paused", {"status": code, "run": d1.get("status")})

    code, bad = call("POST", f"/api/v1/runs/{rid}/decide",
                     {"decision": "approve", "actor": SPEC_U}, token=spec_token, role="specialist")
    check("跨节点决策被拒（409，防伪造签核）", code == 409,
          {"status": code, "detail": str(bad)[:160]})

    code, d2 = call("POST", f"/api/v1/runs/{rid}/decide",
                    {"decision": "approve", "actor": "supervisor-zhang",
                     "note": "容器自检-主管签署"}, token=admin_token, role="supervisor")
    check("主管签署后归档", code == 200 and d2.get("status") == "completed",
          {"status": code, "run": d2.get("status")})

    code, run = call("GET", f"/api/v1/runs/{rid}", token=admin_token)
    events = [e.get("event") for e in (run.get("audit") or [])]
    check("证据链完整",
          code == 200 and all(x in events for x in
                              ("review_submitted", "human_decision", "review_archived")),
          {"events": events})

    # ---------- 6) 容器内后端确实是 PostgreSQL ----------
    code, diag = call("GET", "/api/v1/diagnostics", token=admin_token)
    db = (diag.get("database") or {}) if isinstance(diag, dict) else {}
    check("诊断显示 PostgreSQL 后端与驱动",
          code == 200 and db.get("db_mode") == "pg" and db.get("driver"),
          {"db_mode": db.get("db_mode"), "driver": db.get("driver"),
           "capability": (diag.get("capability_service") or {}).get("status")})

    code, rules = call("GET", "/api/v1/rules", token=admin_token)
    check("规则表可用", code == 200 and rules.get("active", 0) >= 1 and rules.get("digest"),
          {"active": rules.get("active"), "digest": rules.get("digest")})
    code, _ = call("GET", "/api/v1/metrics", token=admin_token)
    check("指标接口可用", code == 200, {"status": code})
    code, _ = call("GET", "/api/v1/runs?limit=5", token=admin_token)
    check("运行列表可读", code == 200, {"status": code})

    passed = sum(1 for c in checks if c["pass"])
    ev.update({"checks": checks, "passed": passed, "total": len(checks),
               "problems": [c["name"] for c in checks if not c["pass"]],
               "verdict": "PASS" if passed == len(checks) else "FAIL",
               "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")

    if not args.quiet:
        print(f"=== 容器交付形态端到端（网关 {args.gateway}）===")
        for c in checks:
            flag = "OK  " if c["pass"] else "FAIL"
            line = f"  [{flag}] {c['name']}"
            if not c["pass"]:
                line += f"   <- {c['detail']}"
            print(line)
    print(f"\n[p8-docker] 校验 {passed}/{len(checks)} 通过，判定 {ev['verdict']}")
    print(f"[p8-docker] 证据: {EVIDENCE}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
