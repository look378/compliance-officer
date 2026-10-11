#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
连接器层单元测试（真实 OA/合同库/邮件对接）

运行：
    .runtime\\venv\\Scripts\\python.exe -m pytest tests -q

覆盖重点（都是"企业对接最容易出事"的地方）：
    - 共享盘目录：元数据可选、脏数据（缺文件）、不存在目录、递归、幂等键来自内容
    - OA HTTP 接口：字段映射、鉴权头、结构不符必须**显式失败**而不是当成"没有新合同"
    - 邮件网关：HTTP / SMTP / none，失败必须返回 ok=False 而不是静默丢弃
    - 工厂：未知类型必须报错（不能静默回退到 mock 导致"以为接了真系统其实还在用 mock"）
    - 降级语义：外部系统故障一律 degraded 且带 error，绝不静默返回空清单
"""

import json
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from service import connectors as C  # noqa: E402


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def workdir():
    """项目内临时目录（本机受限环境下 pytest 内置 tmp_path 会因 ACL 被拒）。"""
    base = PROJECT_ROOT / ".runtime" / "testtmp"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"c{int(time.time() * 1000)}-{time.perf_counter_ns() % 100000}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _StubHandler(BaseHTTPRequestHandler):
    """可编程的假 OA / 邮件网关。"""

    routes: dict = {}          # (method, path) -> (status, body_bytes, headers)
    seen: list = []            # 收到的请求（method, path, headers, body）

    def log_message(self, *a):  # 静音
        pass

    def _handle(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).seen.append({"method": method, "path": self.path,
                                "headers": dict(self.headers), "body": body})
        status, payload, extra = type(self).routes.get(
            (method, self.path), (404, b'{"error":"not found"}', {}))
        # 【第二次审查修正】先复制再改：原来 extra.pop 会就地修改夹具注册的字典，
        # 同一路由第二次请求就丢掉了 Content-Type。
        extra = dict(extra)
        self.send_response(status)
        self.send_header("Content-Type", extra.pop("Content-Type", "application/json"))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


@pytest.fixture()
def stub_server():
    """起一个假 OA 服务，返回 (base_url, routes, seen)；测试结束自动关闭。"""
    routes: dict = {}
    seen: list = []
    _StubHandler.routes = routes
    _StubHandler.seen = seen
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", routes, seen
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def smtp_stub():
    """极简 SMTP 服务器（自己实现协议，不依赖 aiosmtpd 等外部包）。

    支持 EHLO/HELO、AUTH LOGIN、MAIL FROM、RCPT TO、DATA、QUIT，
    收集原始邮件文本供断言；`reject_auth=True` 时 AUTH 返回 535（验证失败路径）。
    """
    box = {"messages": [], "reject_auth": False}
    ready = threading.Event()
    state = {}

    def _serve(sock: socket.socket):
        sock.listen(5)
        state["port"] = sock.getsockname()[1]
        ready.set()
        while state.get("running", True):
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=_session, args=(conn, box), daemon=True).start()

    def _session(conn: socket.socket, box: dict):
        f = conn.makefile("rwb", buffering=0)

        def send(line: str):
            f.write((line + "\r\n").encode())

        send("220 stub ESMTP ready")
        data_mode = False
        buf: list[bytes] = []
        try:
            for raw in f:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if data_mode:
                    if line == ".":
                        # 直接拼**原始字节**：先 decode 再 encode 会把非 ASCII 正文毁掉
                        box["messages"].append(b"\r\n".join(buf).decode("utf-8", errors="replace"))
                        buf, data_mode = [], False
                        send("250 OK queued")
                    else:
                        buf.append(raw.rstrip(b"\r\n"))
                    continue
                up = line.upper()
                if up.startswith(("EHLO", "HELO")):
                    send("250-stub")
                    send("250 AUTH LOGIN PLAIN")
                elif up.startswith("AUTH"):
                    send("535 5.7.8 authentication failed" if box["reject_auth"] else "235 ok")
                elif up.startswith("MAIL FROM"):
                    send("250 OK")
                elif up.startswith("RCPT TO"):
                    send("250 OK")
                elif up == "DATA":
                    data_mode = True
                    send("354 End data with <CR><LF>.<CR><LF>")
                elif up == "QUIT":
                    send("221 Bye")
                    break
                else:
                    send("250 OK")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    state["running"] = True
    threading.Thread(target=_serve, args=(sock,), daemon=True).start()
    ready.wait(5)
    try:
        yield "127.0.0.1", state["port"], box
    finally:
        state["running"] = False
        try:
            sock.close()
        except OSError:
            pass


# ============================================================ 共享盘目录

class TestDirectorySource:
    def test_lists_contract_files_with_optional_metadata(self, workdir):
        (workdir / "CT-001.txt").write_text("技术服务合同\n第一条 双方按约定交付。", encoding="utf-8")
        (workdir / "CT-002.pdf").write_bytes(b"%PDF-1.4 fake")
        (workdir / "CT-002.pdf.meta.json").write_text(json.dumps(
            {"contract_id": "OA-2026-002", "uploaded_at": "2026-10-01 09:00:00",
             "source": "oa-share"}), encoding="utf-8")
        (workdir / "readme.md.txt.bak").write_text("x", encoding="utf-8")   # 非法后缀，忽略

        src = C.DirectoryContractSource(root=str(workdir))
        res = src.list_contracts()
        assert res.ok and not res.degraded
        ids = sorted(i["contract_id"] for i in res.items)
        assert ids == ["CT-001", "OA-2026-002"], ids
        # 元数据可覆盖编号/时间；未提供时回落到文件名与 mtime
        got = {i["contract_id"]: i for i in res.items}
        assert got["OA-2026-002"]["uploaded_at"] == "2026-10-01 09:00:00"
        assert got["CT-001"]["size"] > 0 and got["CT-001"]["uploaded_at"]

    def test_fetch_by_filename_and_by_meta_id(self, workdir):
        p = workdir / "CT-100.txt"
        p.write_text("劳动合同\n第一条 乙方月工作时长不超过 180 小时。", encoding="utf-8")
        src = C.DirectoryContractSource(root=str(workdir))
        assert src.fetch_contract("CT-100").ok
        # 元数据改编号后，用新编号也能取到
        (workdir / "CT-100.txt.meta.json").write_text(
            json.dumps({"contract_id": "OA-XYZ"}), encoding="utf-8")
        r = src.fetch_contract("OA-XYZ")
        assert r.ok and b"180" in r.content

    def test_missing_dir_is_degraded_not_empty(self, workdir):
        """目录不存在必须判 degraded（否则会被当成"没有新合同"，静默漏审）。"""
        src = C.DirectoryContractSource(root=str(workdir / "not-mounted"))
        res = src.list_contracts()
        assert res.ok is False and res.degraded is True and "不存在" in res.error

    def test_recursive_scan(self, workdir):
        sub = workdir / "2026-10"
        sub.mkdir()
        (sub / "CT-DEEP.md").write_text("条款", encoding="utf-8")
        flat = C.DirectoryContractSource(root=str(workdir), recursive=False)
        deep = C.DirectoryContractSource(root=str(workdir), recursive=True)
        assert flat.list_contracts().items == []
        assert [i["contract_id"] for i in deep.list_contracts().items] == ["CT-DEEP"]

    def test_fetch_missing_contract_returns_error(self, workdir):
        (workdir / "a.txt").write_text("x", encoding="utf-8")
        r = C.DirectoryContractSource(root=str(workdir)).fetch_contract("nope")
        assert r.ok is False and "不存在" in r.error

    def test_idempotency_key_is_content_hash_not_filename(self, workdir):
        """同一内容换了文件名，幂等键必须相同（防止改个名就被重复审查）。"""
        (workdir / "v1.txt").write_text("同一份合同内容", encoding="utf-8")
        (workdir / "v2.txt").write_text("同一份合同内容", encoding="utf-8")
        src = C.DirectoryContractSource(root=str(workdir))
        h1 = C.content_hash(src.fetch_contract("v1").content)
        h2 = C.content_hash(src.fetch_contract("v2").content)
        assert h1 == h2


# ============================================================ OA HTTP 接口

class TestHttpSource:
    def test_default_contract_shape(self, stub_server):
        base, routes, _ = stub_server
        routes[("GET", "/oa/contracts")] = (200, json.dumps({
            "count": 1, "items": [{"contract_id": "CT-9", "content_hash": "abc123",
                                   "uploaded_at": "2026-10-02 10:00:00", "size": 42}],
        }).encode(), {})
        routes[("GET", "/oa/contracts/CT-9")] = (200, b"contract body", {})

        src = C.HttpContractSource(base=base)
        res = src.list_contracts()
        assert res.ok and res.items[0]["contract_id"] == "CT-9"
        assert res.items[0]["content_hash"] == "abc123"
        assert src.fetch_contract("CT-9").content == b"contract body"

    def test_field_mapping_for_vendor_shape(self, stub_server, monkeypatch):
        """企业接口字段名/嵌套结构都不同：必须靠配置适配，而不是改代码。"""
        base, routes, _ = stub_server
        routes[("GET", "/api/v2/docs")] = (200, json.dumps({
            "data": {"records": [{"docNo": "OA-77", "sha": "zz"}]},
        }).encode(), {})
        routes[("GET", "/api/v2/docs/OA-77/raw")] = (200, b"raw-bytes", {})
        monkeypatch.setenv("OA_LIST_ITEMS_KEY", "data.records")   # 支持点号路径
        monkeypatch.setenv("OA_ID_FIELD", "docNo")
        monkeypatch.setenv("OA_HASH_FIELD", "sha")

        src = C.HttpContractSource(base=base, list_path="/api/v2/docs",
                                   fetch_path="/api/v2/docs/{contract_id}/raw")
        res = src.list_contracts()
        assert res.ok, res.error
        assert res.items[0]["contract_id"] == "OA-77"
        assert res.items[0]["content_hash"] == "zz"
        assert src.fetch_contract("OA-77").content == b"raw-bytes"

        # 路径写错时必须显式失败并提示顶层字段，方便对接排查
        monkeypatch.setenv("OA_LIST_ITEMS_KEY", "payload.records")
        bad = C.HttpContractSource(base=base, list_path="/api/v2/docs").list_contracts()
        assert bad.ok is False and "payload.records" in bad.error

    def test_wrong_structure_is_explicit_failure(self, stub_server):
        """字段改名/返回 HTML（企业网关常见）必须显式失败，不能当"没有新合同"。"""
        base, routes, _ = stub_server
        routes[("GET", "/oa/contracts")] = (200, b"<html>login required</html>", {})
        res = C.HttpContractSource(base=base).list_contracts()
        assert res.ok is False and res.degraded and "不是 JSON" in res.error

        routes[("GET", "/oa/contracts")] = (200, b'{"unexpected": []}', {})
        res2 = C.HttpContractSource(base=base).list_contracts()
        # 报错必须点出"缺哪条路径 + 顶层有哪些字段"，方便对接时一眼定位厂商字段名
        assert res2.ok is False and "缺少路径" in res2.error and "unexpected" in res2.error

        routes[("GET", "/oa/contracts")] = (200, b'{"items": {"a": 1}}', {})
        res3 = C.HttpContractSource(base=base).list_contracts()
        assert res3.ok is False and "不是数组" in res3.error

    def test_item_without_id_is_reported_per_item(self, stub_server):
        base, routes, _ = stub_server
        routes[("GET", "/oa/contracts")] = (200, json.dumps(
            {"items": [{"size": 1}, {"contract_id": "CT-OK"}]}).encode(), {})
        res = C.HttpContractSource(base=base).list_contracts()
        assert res.ok is True
        bad = [i for i in res.items if not i["contract_id"]]
        assert bad and "error" in bad[0]
        assert any(i["contract_id"] == "CT-OK" for i in res.items)

    def test_5xx_and_unreachable_are_degraded(self, stub_server):
        base, routes, _ = stub_server
        routes[("GET", "/oa/contracts")] = (500, b'{"error":"boom"}', {})
        res = C.HttpContractSource(base=base).list_contracts()
        assert res.ok is False and "HTTP 500" in res.error

        res2 = C.HttpContractSource(base="http://127.0.0.1:1").list_contracts()
        assert res2.ok is False and res2.degraded

    def test_missing_base_url_config(self):
        res = C.HttpContractSource(base="").list_contracts()
        assert res.ok is False and "OA_BASE_URL" in res.error

    def test_auth_header_is_sent(self, stub_server, monkeypatch):
        base, routes, seen = stub_server
        routes[("GET", "/oa/contracts")] = (200, b'{"items": []}', {})
        monkeypatch.setenv("OA_AUTH_HEADER", "Authorization")
        monkeypatch.setenv("OA_AUTH_TOKEN", "Bearer secret-token")
        C.HttpContractSource(base=base).list_contracts()
        assert seen and seen[-1]["headers"].get("Authorization") == "Bearer secret-token"


# ============================================================ 邮件网关

class TestMailGateways:
    def test_http_gateway_posts_payload(self, stub_server):
        base, routes, seen = stub_server
        routes[("POST", "/mail/send")] = (200, json.dumps(
            {"ok": True, "message_id": "mid-1"}).encode(), {})
        res = C.HttpMailGateway(url=f"{base}/mail/send").send_mail(
            "a@example.com", "主题", "正文", idempotency_key="k1")
        assert res.ok and res.message_id == "mid-1"
        sent = json.loads(seen[-1]["body"].decode("utf-8"))
        assert sent["to"] == "a@example.com" and sent["idempotency_key"] == "k1"

    def test_http_gateway_failure_is_reported(self, stub_server):
        base, routes, _ = stub_server
        routes[("POST", "/mail/send")] = (500, b'{"error":"smtp down"}', {})
        res = C.HttpMailGateway(url=f"{base}/mail/send").send_mail("a@x.com", "s", "b")
        assert res.ok is False and "HTTP 500" in res.error

    def test_http_gateway_requires_url(self):
        res = C.HttpMailGateway(url="").send_mail("a@x.com", "s", "b")
        assert res.ok is False and "MAIL_HTTP_URL" in res.error

    def test_smtp_gateway_sends_via_real_protocol(self, smtp_stub):
        """用本地假 SMTP 服务器验证**真实发信协议**（不依赖外网/真实邮箱）。"""
        from email import message_from_bytes
        from email.header import decode_header, make_header
        host, port, box = smtp_stub
        gw = C.SmtpMailGateway(host=host, port=port, sender="compliance@corp.example",
                               starttls=False)
        res = gw.send_mail("lead@corp.example", "[主动审查] 合同 CT-1",
                           "风险等级=high", idempotency_key="k-9")
        assert res.ok, res.error
        assert box["messages"], "假 SMTP 服务器应收到一封信"
        # 用字节解析（message_from_string 在 3.13 会把非 ASCII 正文转成 \uXXXX 转义）
        msg = message_from_bytes(box["messages"][0].encode("utf-8"))
        # 非 ASCII 主题会按 RFC 2047 编码传输，断言时解码（而不是直接比对原文）
        subject = str(make_header(decode_header(str(msg["Subject"]))))
        assert subject == "[主动审查] 合同 CT-1"
        assert str(msg["To"]) == "lead@corp.example"
        assert str(msg["X-Idempotency-Key"]) == "k-9"
        assert "风险等级=high" in msg.get_payload(decode=True).decode("utf-8")

    def test_smtp_login_failure_is_reported(self, smtp_stub):
        host, port, box = smtp_stub
        box["reject_auth"] = True
        gw = C.SmtpMailGateway(host=host, port=port, user="u", password="bad",
                               starttls=False)
        res = gw.send_mail("a@x.com", "s", "b")
        # 【第二次审查修正】原写法是 `A and B or C`，`or "530" in ...` 可独立满足断言，
        # 失败路径可能被绕过。加括号明确优先级。
        assert res.ok is False and ("SMTP" in res.error.upper() or "530" in res.error), res.error

    def test_smtp_requires_host(self):
        res = C.SmtpMailGateway(host="").send_mail("a@x.com", "s", "b")
        assert res.ok is False and "SMTP_HOST" in res.error

    def test_null_gateway_is_explicitly_off(self):
        res = C.NullMailGateway().send_mail("a@x.com", "s", "b")
        assert res.ok and res.source == "none"


# ============================================================ 工厂

class TestFactory:
    def test_unknown_source_raises(self):
        """未知类型必须报错：静默回退到 mock 会让人以为接了真系统。"""
        with pytest.raises(ValueError):
            C.build_contract_source("sharepoint")

    def test_unknown_gateway_raises(self):
        with pytest.raises(ValueError):
            C.build_mail_gateway("exchange")

    def test_env_driven_selection(self, monkeypatch):
        monkeypatch.setenv("CONTRACT_SOURCE", "dir")
        monkeypatch.setenv("CONTRACT_DIR", str(PROJECT_ROOT))
        assert isinstance(C.build_contract_source(), C.DirectoryContractSource)
        monkeypatch.setenv("MAIL_GATEWAY", "none")
        assert isinstance(C.build_mail_gateway(), C.NullMailGateway)

    def test_describe_does_not_leak_secrets(self, monkeypatch):
        monkeypatch.setenv("SMTP_PASSWORD", "super-secret")
        monkeypatch.setenv("OA_AUTH_TOKEN", "another-secret")
        d = C.describe()
        blob = json.dumps(d, ensure_ascii=False)
        assert "super-secret" not in blob and "another-secret" not in blob
        assert d["smtp_auth_configured"] is True and d["oa_auth_configured"] is True
