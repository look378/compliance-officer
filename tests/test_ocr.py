#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OCR 档位单元测试（扫描件不再一律转人工）

运行：
    .runtime\\venv\\Scripts\\python.exe -m pytest tests -q

覆盖重点（离线环境也能全绿）：
    - PDF **渲染**是真实实现：把"扫描版 PDF"（整页位图、无文本层）渲染成 PNG，
      这一环不需要 OCR 模型，因此可以真测
    - OCR 引擎可插拔：用**假 OCR 服务**（本地 HTTP）验证 multipart 请求格式与响应解析
    - 档位与降级：无引擎时 tier=none 且行为与升级前一致（不抛异常、不误判为可解析）
    - 解析链路：扫描件 → OCR → 条款切分 → contract_parser 输出带 ocr_tier 与告警
"""

import io
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, os.environ.get("LEMONCLAW_HOME", str(PROJECT_ROOT.parent / "lemonclaw-main")))

from mcp_server import contract_parser, ocr  # noqa: E402


@pytest.fixture()
def workdir():
    base = PROJECT_ROOT / ".runtime" / "testtmp"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"o{int(time.time() * 1000)}-{time.perf_counter_ns() % 100000}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _cjk_font(size: int = 34):
    """取一个能渲染中文的 TrueType 字体。

    【重要】PIL 的默认位图字体**渲染不出中文**（会画成空白/豆腐块），
    用它生成的"扫描件"根本不含中文字形，OCR 自然识别不出 —— 实测踩过这个坑：
    造出来的测试件让 OCR 结果看起来"引擎不行"，其实是测试件本身没字。
    """
    from PIL import ImageFont
    win = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("simhei.ttf", "msyh.ttc", "simsun.ttc", "STSONG.TTF"):
        p = Path(win) / "Fonts" / name
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_scanned_pdf(path: Path, lines: list[str], pages: int = 1) -> Path:
    """生成"扫描版"PDF：每页是一张整页位图，**没有任何文本层**。

    这正是企业里最常见的扫描件形态；用它才能验证"渲染 + OCR"这条链路。
    文字用真实中文字体绘制（见 _cjk_font 的说明）。
    """
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas

    font = _cjk_font()
    c = rl_canvas.Canvas(str(path), pagesize=(595, 842))     # A4
    for page_no in range(pages):
        img = Image.new("RGB", (1240, 1754), "white")        # 150dpi 左右
        draw = ImageDraw.Draw(img)
        y = 120
        for line in lines:
            draw.text((80, y), line, fill="black", font=font)
            y += 70
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        c.drawImage(ImageReader(buf), 0, 0, width=595, height=842)
        c.showPage()
    c.save()
    return path


class _OcrStubHandler(BaseHTTPRequestHandler):
    """假 OCR 服务：按 `mode` 返回不同形态的响应，便于断言链路真的通了。"""

    mode = "text"
    contract_text = ""       # mode="contract" 时返回这份"识别结果"
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).seen.append({"ctype": self.headers.get("Content-Type", ""),
                                "bytes": len(raw), "body": raw})
        mode = type(self).mode
        if mode == "text":
            payload, ctype = "识别出的文本\n第一条 本产品保本。", "text/plain"
        elif mode == "json":
            payload, ctype = json.dumps({"result": "JSON 里的文本\n第二条 最终解释权归甲方。"},
                                        ensure_ascii=False), "application/json"
        elif mode == "nested":
            payload, ctype = json.dumps({"data": {"text": "嵌套结构的文本"},
                                         "code": 0}, ensure_ascii=False), "application/json"
        elif mode == "contract":
            payload, ctype = json.dumps({"text": type(self).contract_text},
                                        ensure_ascii=False), "application/json"
        elif mode == "broken":
            payload, ctype = json.dumps({"unexpected": [1, 2]}, ensure_ascii=False), "application/json"
        else:
            payload, ctype = "nope", "text/plain"
        body = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def ocr_service():
    _OcrStubHandler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OcrStubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/ocr", _OcrStubHandler
    finally:
        srv.shutdown()
        srv.server_close()


# ============================================================ 档位与引擎

class TestTier:
    def test_engine_detection_reports_all_probes(self):
        ocr._CACHE.clear()          # 【第二次审查修正】先清缓存，避免读到上个用例的引擎
        d = ocr.describe()
        assert set(d["available_engines"]) == {"paddle", "rapidocr", "tesseract", "http"}
        assert d["configured"] in ("auto", "paddle", "rapidocr", "tesseract", "http", "none")
        assert d["dpi"] > 0 and d["max_pages"] > 0

    def test_none_engine_when_nothing_available(self, monkeypatch):
        monkeypatch.setenv("OCR_ENGINE", "auto")
        monkeypatch.delenv("OCR_HTTP_URL", raising=False)
        ocr._CACHE.clear()
        eng = ocr.active_engine()
        # 【第二次审查修正】原断言 `tier in ("none","ocr_lite","ocr_full")` 近乎恒真
        # （只排除了 blocked），无法反映"无引擎时必须降级"这一要求。
        # 现在按本机是否装了引擎分别断言：
        has_local = any(ocr.describe()["available_engines"][n]
                        for n in ("paddle", "rapidocr", "tesseract"))
        if has_local:
            assert eng.name in ("paddle", "rapidocr", "tesseract")
            assert eng.tier in ("ocr_lite", "ocr_full")
        else:
            assert eng.name == "none" and eng.tier == "none"
            assert eng.available is False

    def test_explicit_none_disables_ocr(self, monkeypatch):
        monkeypatch.setenv("OCR_ENGINE", "none")
        ocr._CACHE.clear()
        res = ocr.ocr_image_bytes(b"\x89PNG\r\n\x1a\n fake")
        assert res.ok is False and res.tier == "none" and "OCR 不可用" in res.error

    def test_http_engine_selected_when_configured(self, monkeypatch, ocr_service):
        url, _ = ocr_service
        monkeypatch.setenv("OCR_HTTP_URL", url)
        monkeypatch.setenv("OCR_ENGINE", "http")
        ocr._CACHE.clear()
        eng = ocr.active_engine()
        assert eng.name == "http" and eng.available is True

    def test_unknown_engine_falls_back_to_none(self, monkeypatch):
        monkeypatch.setenv("OCR_ENGINE", "magic-ocr")
        ocr._CACHE.clear()
        eng = ocr.active_engine()
        assert eng.name == "none" and eng.tier == "none"


# ============================================================ PDF 渲染（真实实现）

class TestPdfRendering:
    def test_renders_scanned_pdf_to_png(self, workdir):
        pdf = make_scanned_pdf(workdir / "scan.pdf", ["扫描件第一行", "扫描件第二行"])
        pngs, total, err = ocr.render_pdf_pages(pdf, dpi=100)
        assert err == "" and total == 1 and len(pngs) == 1
        assert pngs[0][:8] == b"\x89PNG\r\n\x1a\n", "应当是合法 PNG"

    def test_scanned_pdf_has_no_text_layer(self, workdir):
        """前提校验：我们造的"扫描件"确实**没有文本层**（只有位图）。

        【重要】这里必须直接查 PDF 文本层（pdfplumber），而不是看
        `contract_parser.parse_pdf` 的结果 —— 后者已经接上了 OCR：
        本机装了真实引擎时它会把位图里的字识别出来（那是**功能正常**的表现；
        实测就是这一点让这条断言一度失败，见下方 test_real_engine_* 用例）。
        """
        import pdfplumber
        pdf = make_scanned_pdf(workdir / "scan.pdf", ["这条文字只存在于位图里"])
        with pdfplumber.open(str(pdf)) as doc:
            text = "\n".join((p.extract_text() or "") for p in doc.pages)
            images = sum(len(p.images) for p in doc.pages)
        assert text.strip() == "", "扫描版 PDF 不应有文本层（否则测试前提不成立）"
        assert images >= 1, "扫描版 PDF 必须至少内嵌一张整页位图"

    def test_max_pages_limits_rendering(self, workdir):
        pdf = make_scanned_pdf(workdir / "multi.pdf", ["页"], pages=3)
        pngs, total, err = ocr.render_pdf_pages(pdf, dpi=80, max_pages=2)
        assert err == "" and total == 3 and len(pngs) == 2

    def test_rendering_missing_file_is_reported(self, workdir):
        pngs, total, err = ocr.render_pdf_pages(workdir / "nope.pdf", dpi=80)
        assert pngs == [] and err


# ============================================================ HTTP OCR 引擎

class TestHttpEngine:
    def test_multipart_request_and_plain_text_response(self, ocr_service):
        url, handler = ocr_service
        handler.mode = "text"
        eng = ocr.HttpOcrEngine(url=url)
        text, err = eng.recognize_png(b"\x89PNG\r\n\x1a\nDATA", page=2)
        assert err == "" and "本产品保本" in text
        sent = handler.seen[-1]
        assert sent["ctype"].startswith("multipart/form-data; boundary=")
        assert b'name="file"' in sent["body"] and b"page2.png" in sent["body"]

    def test_json_result_field(self, ocr_service):
        url, handler = ocr_service
        handler.mode = "json"
        text, err = ocr.HttpOcrEngine(url=url).recognize_png(b"x")
        assert err == "" and "最终解释权" in text

    def test_nested_json_auto_discovery(self, ocr_service):
        url, handler = ocr_service
        handler.mode = "nested"
        text, err = ocr.HttpOcrEngine(url=url).recognize_png(b"x")
        assert err == "" and "嵌套结构" in text

    def test_explicit_text_path(self, ocr_service, monkeypatch):
        url, handler = ocr_service
        handler.mode = "nested"
        monkeypatch.setenv("OCR_HTTP_TEXT_PATH", "data.text")
        text, err = ocr.HttpOcrEngine(url=url).recognize_png(b"x")
        assert err == "" and "嵌套结构" in text
        monkeypatch.setenv("OCR_HTTP_TEXT_PATH", "data.missing")
        text2, err2 = ocr.HttpOcrEngine(url=url).recognize_png(b"x")
        assert text2 == "" and "data.missing" in err2

    def test_unexpected_json_is_reported_with_keys(self, ocr_service):
        url, handler = ocr_service
        handler.mode = "broken"
        text, err = ocr.HttpOcrEngine(url=url).recognize_png(b"x")
        assert text == "" and "unexpected" in err

    def test_unreachable_service_is_reported(self):
        text, err = ocr.HttpOcrEngine(url="http://127.0.0.1:1/ocr").recognize_png(b"x")
        assert text == "" and "不可达" in err

    def test_missing_url_is_reported(self):
        text, err = ocr.HttpOcrEngine(url="").recognize_png(b"x")
        assert text == "" and "OCR_HTTP_URL" in err


# ============================================================ 端到端：扫描件 → OCR → 解析

class TestScannedContractPipeline:
    def test_scanned_pdf_becomes_parsable_via_ocr(self, workdir, ocr_service, monkeypatch):
        """核心用例：**扫描件不再一律转人工**。

        假 OCR 服务返回的正是这份合同应有的条款文本（真实环境下由 OCR 引擎给出），
        验证的是"渲染 → 送 OCR → 拼文本 → 切条款 → 置信度"这条链路完整可用。
        """
        url, handler = ocr_service
        handler.mode = "contract"
        handler.contract_text = ("技术服务合同\n"
                                 "第一条 甲方委托乙方提供运维服务，期限一年。\n"
                                 "第二条 乙方违反保密义务的，违约金为合同总额的 50%。\n"
                                 "第三条 甲方应在验收合格后 30 日内支付服务费。\n")
        monkeypatch.setenv("OCR_HTTP_URL", url)
        monkeypatch.setenv("OCR_ENGINE", "http")
        ocr._CACHE.clear()

        pdf = make_scanned_pdf(workdir / "scanned-contract.pdf",
                               ["这条文字只在位图里，必须靠 OCR 才能拿到"])
        parsed = contract_parser.parse_pdf(pdf)
        assert parsed["ocr_tier"] in ("ocr_lite", "ocr_full")
        assert parsed["ocr"]["engine"] == "http"
        assert parsed["ocr"]["pages_ocr"] == 1
        assert parsed["clause_count"] >= 3
        assert any("违约金" in c["text"] for c in parsed["clauses"])
        assert parsed["confidence"] >= 0.6
        assert any("已用 OCR" in w for w in parsed["warnings"])
        ev = parsed["ocr"]
        assert ev["ok"] is True and ev["text_chars"] > 0

    def test_scanned_pdf_without_engine_still_goes_to_human(self, workdir, monkeypatch):
        """没有 OCR 引擎时，行为与升级前一致：低置信度 + 明确告警（转人工）。"""
        monkeypatch.setenv("OCR_ENGINE", "none")
        monkeypatch.delenv("OCR_HTTP_URL", raising=False)
        ocr._CACHE.clear()
        pdf = make_scanned_pdf(workdir / "scan.pdf", ["只有位图"])
        parsed = contract_parser.parse_pdf(pdf)
        assert parsed["clause_count"] == 0
        assert parsed["confidence"] <= 0.2
        assert any("OCR 不可用" in w or "文本层缺失" in w for w in parsed["warnings"])

    def test_native_pdf_does_not_invoke_ocr(self, workdir, ocr_service, monkeypatch):
        """关键回归：原生文本 PDF **不得**被送去 OCR（那会白烧算力并引入误差）。"""
        url, handler = ocr_service
        monkeypatch.setenv("OCR_HTTP_URL", url)
        monkeypatch.setenv("OCR_ENGINE", "http")
        ocr._CACHE.clear()
        handler.seen = []

        from reportlab.pdfgen import canvas as rl_canvas
        native = workdir / "native.pdf"
        c = rl_canvas.Canvas(str(native), pagesize=(595, 842))
        c.drawString(80, 780, "Native text contract clause one with enough characters.")
        c.drawString(80, 750, "第二条 甲方应在验收合格后 30 日内支付服务费，逾期按日万分之五计息。")
        c.showPage()
        c.save()

        parsed = contract_parser.parse_pdf(native)
        assert parsed["char_count"] > 0
        assert parsed["ocr_tier"] == "native"
        assert parsed["ocr"] == {}
        assert handler.seen == [], "原生文本 PDF 不应调用 OCR 服务"

    def test_ocr_image_bytes_api(self, ocr_service, monkeypatch):
        url, handler = ocr_service
        handler.mode = "text"
        monkeypatch.setenv("OCR_HTTP_URL", url)
        monkeypatch.setenv("OCR_ENGINE", "http")
        ocr._CACHE.clear()
        res = ocr.ocr_image_bytes(b"\x89PNG\r\n\x1a\n x")
        assert res.ok and res.pages_ocr == 1 and "本产品保本" in res.text


# ============================================================ 真实引擎（装了就必须真识别）

def _engine_available(name: str) -> bool:
    try:
        return bool(getattr(ocr._build(name), "available", False))
    except Exception:
        return False


REAL_ENGINE = next((n for n in ("rapidocr", "paddle", "tesseract") if _engine_available(n)), "")


@pytest.mark.skipif(not REAL_ENGINE, reason="本机未安装任何本地 OCR 引擎（用假 OCR 服务覆盖链路）")
class TestRealEngine:
    """本机装了真实 OCR 引擎时，必须**真的把扫描件里的中文字识别出来**。

    这组用例是"OCR 升级"的最终判据：不是"链路通了"，而是"真的读出了字"。
    """

    CONTRACT_LINES = [
        "技术服务合同",
        "第一条 甲方委托乙方提供运维服务，期限一年。",
        "第二条 乙方违反保密义务的，违约金为合同总额的 50%。",
        "第三条 甲方应在验收合格后 30 日内支付服务费。",
    ]

    def test_real_engine_recognizes_chinese_scan(self, workdir, monkeypatch):
        monkeypatch.setenv("OCR_ENGINE", REAL_ENGINE)
        monkeypatch.delenv("OCR_HTTP_URL", raising=False)
        monkeypatch.setenv("OCR_DPI", "200")
        ocr._CACHE.clear()

        pdf = make_scanned_pdf(workdir / "real-scan.pdf", self.CONTRACT_LINES)
        res = ocr.ocr_pdf(pdf)
        assert res.ok, f"{REAL_ENGINE} 未识别出任何文字：{res.error} {res.warnings}"
        assert res.engine == REAL_ENGINE and res.pages_ocr >= 1
        assert len(res.text) >= 10, f"识别文本过短：{res.text!r}"
        # 中文契约关键词必须出现（允许 OCR 有少量误差，因此做"包含任一"判定）
        assert any(k in res.text for k in ("违约金", "运维服务", "验收", "委托")), \
            f"{REAL_ENGINE} 识别结果不含关键条款：{res.text!r}"

    def test_real_engine_pipeline_gives_parsable_contract(self, workdir, monkeypatch):
        """扫描件 → 真实 OCR → 条款切分 → 可判定风险（整条业务链路可用）。"""
        monkeypatch.setenv("OCR_ENGINE", REAL_ENGINE)
        monkeypatch.delenv("OCR_HTTP_URL", raising=False)
        ocr._CACHE.clear()

        pdf = make_scanned_pdf(workdir / "real-contract.pdf", self.CONTRACT_LINES)
        parsed = contract_parser.parse_pdf(pdf)
        assert parsed["ocr_tier"] in ("ocr_lite", "ocr_full"), parsed.get("ocr")
        assert (parsed.get("ocr") or {}).get("engine") == REAL_ENGINE
        assert parsed["clause_count"] >= 2, parsed["clauses"]
        assert parsed["confidence"] >= 0.6, parsed["confidence"]
        assert any("已用 OCR" in w for w in parsed["warnings"])

        # 再往前一步：把 OCR 出来的文本送进规则判定，确认能真的判出风险
        from mcp_server import rules_store
        joined = "\n".join(c["text"] for c in parsed["clauses"])
        ann = rules_store.annotate(joined)
        assert ann["risk_level"] in ("low", "mid", "high")
        # 扫描件里含"违约金 50%"，真实规则表应当命中 RR-006
        assert "RR-006" in [h["rule_id"] for h in ann["risk_hits"]], ann["risk_hits"]

    def test_real_engine_available_is_reported_in_describe(self, monkeypatch):
        monkeypatch.setenv("OCR_ENGINE", "auto")
        ocr._CACHE.clear()
        d = ocr.describe()
        assert d["available_engines"][REAL_ENGINE] is True
        assert d["engine"] == REAL_ENGINE, f"auto 应挑中已安装的引擎，实际 {d['engine']}"
