#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P2 自检 C：Mock 外部系统的故障注入（容错矩阵的可执行化）

覆盖容错矩阵中与外部系统相关的全部故障类型，并验证"调用方必须能区分"：
    happy      正常上传/列表/下载/发信
    empty      结构合法但内容为空（不得当成"有数据"）
    bad_data   结构错误但 HTTP 200（最阴险：调用方以为成功）
    error      5xx
    timeout    服务端不响应 -> 客户端超时（必须能被超时机制捕获）
    duplicate  重复投递 → 调用方需靠内容哈希/幂等键去重

产出：docs/evidence/p2-mock-faults.json
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MOCK = PROJECT_ROOT / "mock" / "mock_office.py"
LOG = PROJECT_ROOT / ".runtime" / "logs" / "p2-mock.log"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p2-mock-faults.json"
BASE = "http://127.0.0.1:8780"
TIMEOUT_SLEEP = 3.0          # 自检用短休眠（默认 35s 太长）


def start() -> subprocess.Popen:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOG, "a", encoding="utf-8")
    fh.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    fh.flush()
    return subprocess.Popen(
        [sys.executable, str(MOCK), "--port", "8780", "--timeout-sleep", str(TIMEOUT_SLEEP)],
        stdout=fh, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})


def wait_health(timeout=25.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(f"{BASE}/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    ev = {"check": "P2-mock-faults", "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    checks: dict[str, dict] = {}
    problems: list[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    proc = None
    if not wait_health(timeout=2):
        proc = start()
    if not wait_health():
        print("[mock] 服务启动失败，见日志")
        ev["verdict"] = "FAIL"
        EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    ev["health"] = httpx.get(f"{BASE}/health", timeout=5).json()
    print(f"[mock] 健康检查: {ev['health']['status']} 默认故障={ev['health']['fault_default']}")

    content = ("劳动合同（mock 用例）\n第一条 乙方月工作时长不超过 280 小时。\n"
               "第二条 本产品保本，年化收益不低于 5%。\n").encode("utf-8")

    # ---------------- happy path ----------------
    r = httpx.post(f"{BASE}/oa/contracts", content=content, timeout=10)
    up = r.json()
    check("upload_ok", r.status_code == 200 and up.get("content_hash"),
          {"status": r.status_code, "contract_id": up.get("contract_id")})
    cid = up.get("contract_id")

    r2 = httpx.post(f"{BASE}/oa/contracts", content=content, timeout=10)
    check("upload_dedup_by_hash", r2.json().get("deduplicated") is True
          and r2.json().get("contract_id") == cid, {"resp": r2.json()})

    lst = httpx.get(f"{BASE}/oa/contracts", timeout=10).json()
    check("list_ok", lst["count"] >= 1 and any(i["contract_id"] == cid for i in lst["items"]),
          {"count": lst["count"]})

    dl = httpx.get(f"{BASE}/oa/contracts/{cid}", timeout=10)
    check("download_ok", dl.status_code == 200 and dl.content == content,
          {"status": dl.status_code, "bytes": len(dl.content)})

    miss = httpx.get(f"{BASE}/oa/contracts/CT-NOTEXIST", timeout=10)
    check("missing_returns_404", miss.status_code == 404, {"status": miss.status_code})

    mail = httpx.post(f"{BASE}/mail/send",
                      json={"to": "supervisor@example.com", "subject": "合同待复核",
                            "body": "请复核高风险条款", "idempotency_key": "run-001"},
                      timeout=10).json()
    check("mail_send_ok", mail.get("ok") and mail.get("message_id"), mail)

    # ---------------- 故障：empty ----------------
    e1 = httpx.get(f"{BASE}/oa/contracts", headers={"X-Mock-Mode": "empty"}, timeout=10).json()
    check("fault_empty_is_structured", e1.get("count") == 0 and e1.get("items") == [],
          {"count": e1.get("count")})

    # ---------------- 故障：bad_data ----------------
    r = httpx.get(f"{BASE}/oa/contracts", headers={"X-Mock-Mode": "bad_data"}, timeout=10)
    body = r.json()
    check("fault_bad_data_detectable",
          r.status_code == 200 and "items" not in body and "unexpected" in body,
          {"status": r.status_code, "keys": sorted(body.keys())})

    # ---------------- 故障：error ----------------
    r = httpx.get(f"{BASE}/oa/contracts", headers={"X-Mock-Mode": "error"}, timeout=10)
    check("fault_error_is_5xx", r.status_code == 500, {"status": r.status_code})

    # ---------------- 故障：timeout ----------------
    t0 = time.time()
    timed_out = False
    try:
        httpx.get(f"{BASE}/oa/contracts", headers={"X-Mock-Mode": "timeout"}, timeout=1.0)
    except httpx.TimeoutException:
        timed_out = True
    except Exception as exc:
        ev.setdefault("timeout_exc", f"{type(exc).__name__}: {exc}")
    elapsed = round(time.time() - t0, 2)
    check("fault_timeout_detectable", timed_out, {"client_timeout_s": 1.0, "elapsed": elapsed})

    # ---------------- 故障：duplicate ----------------
    d = httpx.post(f"{BASE}/oa/contracts", content=content + "重复投递特征".encode("utf-8"),
                   headers={"X-Mock-Mode": "duplicate"}, timeout=10).json()
    d2 = httpx.post(f"{BASE}/mail/send", json={"to": "a@b.c", "subject": "dup"},
                    headers={"X-Mock-Mode": "duplicate"}, timeout=10).json()
    listing = httpx.get(f"{BASE}/oa/contracts", timeout=10).json()
    dups = [i for i in listing["items"] if i["contract_id"].endswith("-DUP")]
    check("fault_duplicate_visible", len(dups) >= 1 and d.get("contract_id"),
          {"dup_ids": [i["contract_id"] for i in dups], "mail_id": d2.get("message_id")})

    # ---------------- 回信注入 ----------------
    inj = httpx.post(f"{BASE}/mail/inject",
                     json={"from": "specialist@example.com", "subject": "Re: 合同待复核",
                           "body": "确认高风险，要求修改后重新提交。"}, timeout=10).json()
    inbox = httpx.get(f"{BASE}/mail/inbox", timeout=10).json()
    check("inbound_injection", inj.get("ok") and any(
        i.get("direction") == "inbound" for i in inbox["items"]),
        {"inbox_count": inbox["count"]})

    ev["checks"] = checks
    ev["problems"] = problems
    ev["passed"] = sum(1 for c in checks.values() if c["pass"])
    ev["total"] = len(checks)
    ev["verdict"] = "PASS" if not problems else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n[mock] 自检 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[mock] 证据: {EVIDENCE}")

    if proc is not None:
        proc.terminate()
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
