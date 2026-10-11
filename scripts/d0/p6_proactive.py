#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P6 端到端：主动工作（例行巡检 + 自排跟进）

验证内容：
    A 例行巡检闭环：收件目录有新合同 → 主动发起审查 → 状态与风险等级 → 邮件通知
    B 幂等去重：再次扫描同一目录，不再重复审查（内容哈希去重）
    C 故障降级：mock OA 在 timeout/bad_data/empty 三种故障下都能被识别且不崩溃
    D 定时任务注册：把"例行巡检"注册为框架 Cron（到期由 Agent 执行提示词）
    E 自排跟进：审查中发现材料缺失 → 员工自己创建跟进 Cron 任务
    F 批次不阻塞：单条合同处理失败不影响其余合同

产出：docs/evidence/p6-proactive.json
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap, paths  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p6-proactive.json"

C1 = ("技术服务合同（巡检批次 A）\n第一条 甲方委托乙方提供运维服务，期限一年。\n"
      "第二条 甲方在验收合格后 30 日内支付服务费。\n")
C2 = ("劳动合同（巡检批次 B）\n第一条 乙方月工作时长不超过 280 小时。\n"
      "第二条 甲方有权无条件解除本合同，最终解释权归甲方所有。\n")


class _MailSink:
    """内存邮件网关：证明"邮件通道可替换"，同时让断言不依赖 Mock 邮箱。"""

    name = "memory"

    def __init__(self):
        self.sent = []

    def send_mail(self, to: str, subject: str, body: str, idempotency_key: str = ""):
        from service.connectors import MailResult
        self.sent.append({"to": to, "subject": subject, "body": body,
                          "idempotency_key": idempotency_key})
        return MailResult(True, message_id=f"mem-{len(self.sent)}", source=self.name)


def ensure_services() -> dict:
    """确保 Mock OA（8780）与岗位能力服务（8770）在线。"""
    import urllib.request
    started = {}
    for name, port, script, extra in (
            ("mock_oa", 8780, PROJECT_ROOT / "mock" / "mock_office.py", ["--timeout-sleep", "3"]),
            ("capability", 8770, PROJECT_ROOT / "mcp_server" / "compliance_mcp_server.py", [])):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                json.loads(r.read().decode("utf-8"))
                started[name] = "already-running"
                continue
        except Exception:
            pass
        log = PROJECT_ROOT / ".runtime" / "logs" / f"p6-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log, "a", encoding="utf-8")
        fh.write(f"\n===== P6 启动 {name} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        fh.flush()
        subprocess.Popen([sys.executable, str(script), *extra], stdout=fh,
                         stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
                         env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
                              "PYTHONPATH": str(PROJECT_ROOT)})
        t0 = time.time()
        ok = False
        while time.time() - t0 < 25:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                    ok = True
                    break
            except Exception:
                time.sleep(0.4)
        started[name] = "started" if ok else "failed"
    return started


def upload(text: str) -> dict:
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:8780/oa/contracts",
                                 data=text.encode("utf-8"), method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ev = {"flow": "P6-proactive", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": {}}
    checks: dict[str, dict] = {}
    problems: list[str] = []

    def check(name, ok, detail=None):
        checks[name] = {"pass": bool(ok), "detail": detail}
        if not ok:
            problems.append(name)

    try:
        ev["services"] = ensure_services()
        print(f"[P6] 服务: {ev['services']}")

        # 隔离巡检状态：不改动真实 .runtime/scan-state.json
        # 【审查修正 P1】原实现直接 unlink() 生产状态文件且不备份、不还原：
        # processed_hashes 是"同一合同只主动审一次"的唯一依据，删掉后
        # **历史合同会在下一轮真实巡检里全变成新合同**（重复审查 + 重复发邮件）。
        # 现在把状态路径切到自检专用文件，并在结束时清理。
        import os as _os
        selftest_state = PROJECT_ROOT / ".runtime" / "logs" / "p6-scan-state.json"
        _os.environ["SCAN_STATE_PATH"] = str(selftest_state)
        if selftest_state.exists():
            selftest_state.unlink()
        ev["scan_state_path"] = str(selftest_state)

        from service.proactive_worker import ProactiveWorker
        from service.review_orchestrator import ReviewOrchestrator
        orch = ReviewOrchestrator()
        orch.ensure_compiled()
        worker = ProactiveWorker(orchestrator=orch)

        u1 = upload(C1 + f"\n（批次标记 {time.time()}）")
        u2 = upload(C2 + f"\n（批次标记 {time.time()}）")
        ev["uploaded"] = [u1, u2]
        print(f"[P6] 已投递合同 2 份: {[u.get('contract_id') for u in (u1, u2)]}")

        # ---------------- A 例行巡检闭环 ----------------
        scan1 = worker.scan_once(notify_to="compliance-lead@example.com")
        submitted = scan1["submitted"]
        risks = {s["contract_id"]: s["risk_level"] for s in submitted}
        check("A_scan_submits_new_contracts",
              scan1["inbox_ok"] and scan1["new"] >= 2 and len(submitted) >= 2
              and all(s["run_id"] for s in submitted),
              {"new": scan1["new"], "submitted": submitted})
        check("A_risk_levels_assigned",
              "low" in risks.values() and "high" in risks.values(),
              {"risks": risks})
        check("A_notifications_sent", len(scan1["notifications"]) >= 2,
              {"notified": scan1["notifications"]})
        print(f"[P6][A] 巡检: 扫描 {scan1['scanned']} 份，新发起 {scan1['new']} 份，"
              f"风险 {risks}，通知 {len(scan1['notifications'])} 封")
        ev["cases"]["A_scan"] = scan1

        # ---------------- B 幂等去重 ----------------
        scan2 = worker.scan_once(notify_to="")
        check("B_idempotent_rescan",
              scan2["new"] == 0 and scan2["skipped_duplicate"] >= 2,
              {"new": scan2["new"], "skipped": scan2["skipped_duplicate"], "scanned": scan2["scanned"]})
        print(f"[P6][B] 重复巡检: 新发起 {scan2['new']} 份，按内容哈希跳过 {scan2['skipped_duplicate']} 份")
        ev["cases"]["B_idempotent"] = scan2

        # ---------------- C 故障降级 ----------------
        faults = {}
        for mode in ("timeout", "bad_data", "empty"):
            r = worker.scan_once(fault=mode, notify_to="")
            faults[mode] = {"ok": r["inbox_ok"], "degraded": r["degraded"],
                            "new": r["new"], "error": r.get("error")}
        check("C_faults_handled",
              faults["timeout"]["degraded"] is True
              and faults["bad_data"]["degraded"] is True
              and faults["empty"]["ok"] is True and faults["empty"]["new"] == 0,
              faults)
        print(f"[P6][C] 故障降级: timeout={faults['timeout']['degraded']} "
              f"bad_data={faults['bad_data']['degraded']} empty_new={faults['empty']['new']}")
        ev["cases"]["C_faults"] = faults

        # ---------------- D 定时任务注册 ----------------
        sched = worker.schedule_scan(cron="0 9 * * *")
        listed = worker.list_scheduled()
        check("D_cron_registered",
              sched["ok"] and any(t["cron"] == "0 9 * * *" for t in listed),
              {"schedule": sched, "tasks": listed})
        print(f"[P6][D] 例行巡检定时任务: {sched.get('raw')}")
        ev["cases"]["D_schedule"] = {"schedule": sched, "tasks": listed}

        # ---------------- E 自排跟进任务 ----------------
        fu = worker.schedule_followup(case_ref=f"CT-followup-{int(time.time()) % 10000}",
                                      missing="乙方资质证明与近三年审计报告", due_days=3)
        listed2 = worker.list_scheduled()
        followups = [t for t in listed2 if "跟进提醒" in t["prompt"]]
        check("E_followup_scheduled",
              fu["ok"] and bool(followups) and fu["due"],
              {"followup": fu, "matched": followups})
        print(f"[P6][E] 自排跟进: 到期 {fu['due']}，提示词已注册={bool(followups)}")
        ev["cases"]["E_followup"] = {"followup": fu, "matched": followups}

        # ---------------- F 批次不阻塞 ----------------
        # 【审查修正 P2】原实现依赖"Mock 的 -DUP 记录无对应文件"来制造单条失败，
        # 但 Mock 的 duplicate 模式写入的是**同内容不同 ID**的记录（有文件、内容哈希相同），
        # 实际效果是"被幂等跳过"而不是"下载失败"—— 该用例从未真正制造过失败。
        # 现在确定性地注入一个会抛错的条目：monkeypatch download 让指定 contract_id 失败，
        # 再验证批次其余条目仍被正常处理（failed 非空 + scanned 覆盖全部条目）。
        import urllib.request
        dup_req = urllib.request.Request(
            "http://127.0.0.1:8780/oa/contracts",
            data=(C1 + "\n（脏数据条目）").encode("utf-8"), method="POST",
            headers={"X-Mock-Mode": "duplicate"})
        with urllib.request.urlopen(dup_req, timeout=10) as r:
            json.loads(r.read().decode("utf-8"))

        # 再投递两份**内容唯一**的新合同，作为确定性的"受害者"与"后续条目"：
        # 内容哈希全新（标记放在最前面，保证与任何历史内容都不同）→ 必然未被处理过
        # → 一定会走到 download 分支（不会被幂等跳过）。
        # 两份是必要的：只有确认"受害者之后仍有条目被正常处理"，才能证明批次没有中断。
        mark = time.time()
        victim = upload(f"（故障用例 A {mark}）\n" + C1).get("contract_id", "")
        tail_id = upload(f"（故障用例 B {mark}）\n" + C2).get("contract_id", "")
        items_now = worker.list_inbox().get("items") or []
        ev["F_injected"] = {"items": len(items_now), "victim": victim, "tail": tail_id}
        if not (victim and tail_id):
            check("F_batch_continues_on_partial_failure", False,
                  {"reason": "投递故障用例失败，无法验证批次健壮性"})
        real_download = worker.download

        def _flaky_download(contract_id, _victim=victim, _real=real_download):
            if contract_id == _victim:
                raise OSError(f"注入的下载故障（{contract_id}）")
            return _real(contract_id)

        worker.download = _flaky_download
        try:
            # max_items 必须覆盖整个收件目录：Mock 的收件目录是**共享且跨轮累积**的，
            # 一旦历史条目超过 20 条，默认上限会把本轮刚投递的"受害者/后续条目"截断在批次之外，
            # 用例就会因为"没轮到它"而假失败（实测踩过）。
            scan3 = worker.scan_once(notify_to="", max_items=max(20, len(items_now) + 2))
        finally:
            worker.download = real_download
        # 要求：①坏条目确实进了 failed；②注入故障之后**仍有提交成功**（批次未被中断）。
        tail_ok = any(s.get("contract_id") == tail_id for s in (scan3.get("submitted") or []))
        check("F_batch_continues_on_partial_failure",
              scan3["inbox_ok"] and bool(scan3["failed"])
              and any(f.get("contract_id") == victim for f in scan3["failed"])
              and scan3["new"] >= 1 and tail_ok
              and scan3["scanned"] >= 2,
              {"injected_failure": victim, "tail_contract": tail_id,
               "new": scan3["new"], "skipped": scan3["skipped_duplicate"],
               "scanned": scan3["scanned"], "tail_submitted": tail_ok,
               "failed": scan3["failed"][:3]})
        print(f"[P6][F] 批次健壮性: 注入故障 {victim}；新发起 {scan3['new']}，"
              f"跳过 {scan3['skipped_duplicate']}，失败明细 {len(scan3['failed'])} 条")
        ev["cases"]["F_batch"] = scan3

        # ---------------- G 企业连接器：共享盘目录作为合同来源 ----------------
        # 这是"真实 OA/合同库对接"的可运行证明：把连接器从 Mock 换成**共享盘目录**，
        # 巡检逻辑一行不改，仍然完成"发现新合同 → 发起审查 → 幂等只审一次"。
        from service.connectors import DirectoryContractSource, describe as describe_conn
        from service.proactive_worker import ProactiveWorker as _PW
        share = PROJECT_ROOT / ".runtime" / "g-share-inbox"
        if share.exists():
            import shutil as _sh
            _sh.rmtree(share, ignore_errors=True)
        share.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        (share / f"共享盘合同A-{stamp}.txt").write_text(
            f"（共享盘 A {stamp}）\n" + C1, encoding="utf-8")
        (share / f"共享盘合同B-{stamp}.txt").write_text(
            f"（共享盘 B {stamp}）\n" + C2, encoding="utf-8")
        # 带元数据的一份：验证"OA 提供编号/时间"的形态
        (share / f"共享盘合同C-{stamp}.txt").write_text(
            f"（共享盘 C {stamp}）\n" + C1, encoding="utf-8")
        (share / f"共享盘合同C-{stamp}.txt.meta.json").write_text(
            json.dumps({"contract_id": f"OA-SHARE-{stamp}", "uploaded_at": "2026-10-10 09:00:00",
                        "source": "oa-share"}, ensure_ascii=False), encoding="utf-8")
        # 脏数据：只有元数据没有原文（企业共享盘常见）→ 必须单条失败、不阻塞
        (share / f"共享盘合同D-{stamp}.txt.meta.json").write_text(
            json.dumps({"contract_id": f"OA-BROKEN-{stamp}"}, ensure_ascii=False), encoding="utf-8")

        dir_source = DirectoryContractSource(root=str(share))
        dir_mail = _MailSink()          # 邮件改成"记录到内存"，证明网关可替换
        dir_worker = _PW(orchestrator=orch, contract_source=dir_source, mail_gateway=dir_mail)
        scan_dir = dir_worker.scan_once(notify_to="compliance-lead@example.com", max_items=50)
        scan_dir2 = dir_worker.scan_once(notify_to="", max_items=50)
        listing = dir_source.list_contracts()
        ev["cases"]["G_connectors"] = {
            "contract_dir": str(share), "connector_config": describe_conn(),
            "listing_count": len(listing.items), "listing_ok": listing.ok,
            "scan": scan_dir, "rescan_new": scan_dir2["new"],
            "rescan_skipped": scan_dir2["skipped_duplicate"],
            "mails_sent": len(dir_mail.sent),
        }
        check("G_connector_dir_source_works",
              listing.ok and len(listing.items) == 4
              and scan_dir["inbox_ok"] and scan_dir["new"] == 3
              and len(dir_mail.sent) == 3,
              {"listed": len(listing.items), "new": scan_dir["new"],
               "mails": len(dir_mail.sent), "failed": scan_dir["failed"][:2]})
        # 幂等：同一份内容换个文件名不得被重复审查（幂等键来自内容哈希）
        (share / f"共享盘合同A-副本-{stamp}.txt").write_text(
            f"（共享盘 A {stamp}）\n" + C1, encoding="utf-8")
        scan_dir3 = dir_worker.scan_once(notify_to="", max_items=50)
        check("G_connector_content_hash_idempotent",
              scan_dir3["new"] == 0 and scan_dir3["skipped_duplicate"] >= 3,
              {"new": scan_dir3["new"], "skipped": scan_dir3["skipped_duplicate"]})
        # 脏数据（有元数据无原文）必须计入 failed 而不是静默跳过
        check("G_connector_dirty_entry_reported",
              any("BROKEN" in str(f.get("contract_id")) for f in scan_dir["failed"]),
              {"failed": scan_dir["failed"][:3]})
        print(f"[P6][G] 连接器（共享盘目录）: 清单 {len(listing.items)} 条，"
              f"新发起 {scan_dir['new']} 份，通知 {len(dir_mail.sent)} 封，"
              f"重扫跳过 {scan_dir2['skipped_duplicate']} 份，脏数据 {len(scan_dir['failed'])} 条")

        # 连接器不可达时必须显式降级（不能当成"没有新合同"）
        missing = _PW(orchestrator=orch,
                      contract_source=DirectoryContractSource(
                          root=str(PROJECT_ROOT / ".runtime" / "no-such-inbox")),
                      mail_gateway=dir_mail)
        scan_missing = missing.scan_once(notify_to="")
        check("G_connector_unavailable_degrades",
              scan_missing["inbox_ok"] is False and scan_missing["degraded"] is True
              and bool(scan_missing.get("error")),
              {"inbox_ok": scan_missing["inbox_ok"], "error": scan_missing.get("error", "")[:120]})
        ev["cases"]["G_connector_degrade"] = {
            "inbox_ok": scan_missing["inbox_ok"], "degraded": scan_missing["degraded"],
            "error": scan_missing.get("error")}
        print(f"[P6][G] 连接器降级: 目录不存在 -> degraded={scan_missing['degraded']}，"
              f"错误可读={bool(scan_missing.get('error'))}")

        ev["checks"] = checks
        ev["problems"] = problems
        ev["passed"] = sum(1 for c in checks.values() if c["pass"])
        ev["total"] = len(checks)
        ev["verdict"] = "PASS" if not problems else "FAIL"
        ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        ev["paths"] = paths()
    finally:
        # 自检扫描状态清理（真实 .runtime/scan-state.json 全程未被触碰）
        try:
            if selftest_state.exists():
                selftest_state.unlink()
            ev["scan_state_cleaned"] = True
        except OSError as exc:
            ev["scan_state_cleaned"] = f"{type(exc).__name__}: {exc}"

    print(f"\n[P6] 校验 {ev['passed']}/{ev['total']} 通过，判定 {ev['verdict']}")
    for p in problems:
        print(f"   - 失败: {p}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[P6] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
