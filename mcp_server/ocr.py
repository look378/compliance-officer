#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OCR 档位（可插拔引擎）—— 让扫描件不再一律转人工

背景（`docs/12-代码审查报告.md` §6 的"OCR 档位"限制）：
    此前 `OCR_TIER="native"`：扫描件（无文本层）解析不出任何文字，只能整份转人工。
    这在企业里很常见（对方发的扫描版合同），因此必须有可用的 OCR 路径。

设计原则（与我方"零依赖离线可跑"的纪律一致）：
    1. **引擎可插拔**：`PaddleOCR → RapidOCR → Tesseract → HTTP OCR 服务 → none` 顺序自动探测。
       真实项目上哪种，取决于贵司环境，不由代码写死。
    2. **离线优先**：本机没装任何 OCR 依赖时，`tier=none` 并且**行为与从前一致**（转人工），
       不会因为"升级了 OCR"反而让原来的用例失败。
    3. **PDF 渲染是真实实现**：扫描件必须先把页面渲染成位图才能 OCR。
       用 `pypdfium2`（随 pdfplumber 一起装，无需额外获取模型）真实渲染，
       因此"渲染 → 送 OCR → 拼文本"这条链路本身是完整可验证的。
    4. **HTTP OCR 服务是一等公民**：企业内网通常已有 OCR 中台；
       只要给 URL 就能用，不需要在本机装模型（`OCR_ENGINE=http`）。

配置（环境变量）：
    OCR_ENGINE        auto | paddle | rapidocr | tesseract | http | none   （默认 auto）
    OCR_LANG          chi_sim+eng（tesseract）/ ch（rapidocr）           （默认按引擎给）
    OCR_DPI           渲染分辨率，默认 200（越大越准越慢）
    OCR_MAX_PAGES     单份文档最多 OCR 的页数，默认 30（防超大件拖垮）
    OCR_TIMEOUT_S     单页超时，默认 60
    OCR_HTTP_URL      OCR 服务地址（OCR_ENGINE=http 时必填）
    OCR_HTTP_FIELD    上传字段名，默认 file
    OCR_HTTP_TEXT_PATH  响应里正文的取值路径（支持点号），默认自动尝试 text/result/data.text
    OCR_TESSERACT_CMD  tesseract 可执行文件（不在 PATH 时指定）

输出契约：`ocr_image()` / `ocr_pdf()` 返回 `{ok, text, engine, tier, pages, pages_ocr, warnings}`；
    失败**不抛异常**，由调用方（contract_parser）决定如何降级（当前策略：仍有文本层就用文本层，
    完全没有文本就转人工）。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


DEFAULT_DPI = _env_int("OCR_DPI", 200)
MAX_PAGES = _env_int("OCR_MAX_PAGES", 30)
TIMEOUT_S = _env_int("OCR_TIMEOUT_S", 60)


@dataclass
class OcrResult:
    ok: bool
    text: str = ""
    engine: str = "none"
    tier: str = "none"
    pages: int = 0
    pages_ocr: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text, "engine": self.engine, "tier": self.tier,
                "pages": self.pages, "pages_ocr": self.pages_ocr,
                "text_chars": len(self.text or ""), "warnings": self.warnings,
                "error": self.error}


# ============================================================ 引擎契约

class OcrEngine(Protocol):
    name: str
    tier: str
    available: bool
    def recognize_png(self, png: bytes, page: int = 1) -> tuple[str, str]: ...


class NoOcrEngine:
    """显式关闭 OCR（默认降级档位：行为与升级前一致）。"""

    name = "none"
    tier = "none"
    available = False

    def recognize_png(self, png: bytes, page: int = 1) -> tuple[str, str]:
        return "", "OCR 未启用（OCR_ENGINE=none 或本机无可用引擎）"


class HttpOcrEngine:
    """企业 OCR 服务（内网中台）：把页面 PNG POST 过去，取回文本。

    请求：`multipart/form-data`，字段名默认 `file`（可用 OCR_HTTP_FIELD 改）。
    响应：JSON，正文取值按 `OCR_HTTP_TEXT_PATH`（点号路径）或自动尝试 text/result/data.text；
          也兼容直接返回纯文本（text/plain）。
    """

    tier = "ocr_lite"

    def __init__(self, url: str = "", field_name: str = "", text_path: str = "",
                 timeout: int = 0):
        self.url = url or _env("OCR_HTTP_URL", "")
        self.field_name = field_name or _env("OCR_HTTP_FIELD", "file")
        self.text_path = text_path or _env("OCR_HTTP_TEXT_PATH", "")
        self.timeout = timeout or TIMEOUT_S
        self.name = "http"

    @property
    def available(self) -> bool:
        return bool(self.url)

    def recognize_png(self, png: bytes, page: int = 1) -> tuple[str, str]:
        if not self.available:
            return "", "未配置 OCR_HTTP_URL"
        boundary = "----complianceocr"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{self.field_name}"; '
            f'filename="page{page}.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8") + png + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read().decode("utf-8", errors="replace")
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return "", f"OCR 服务 HTTP {exc.code}"
        except Exception as exc:
            return "", f"OCR 服务不可达：{type(exc).__name__}: {exc}"
        if "json" not in ctype.lower():
            return payload.strip(), ""
        try:
            data = json.loads(payload)
        except Exception:
            return "", "OCR 服务返回的不是合法 JSON"
        text = self._pick_text(data)
        if text:
            return text, ""
        searched = f"配置的取值路径 {self.text_path!r}" if self.text_path else "自动字段探测"
        return "", f"OCR 响应里找不到正文（{searched}；响应字段：{self._keys(data)}）"

    def _pick_text(self, data: Any) -> str:
        if isinstance(data, str):
            return data
        if not isinstance(data, dict):
            return ""
        if self.text_path:
            # 显式配置了取值路径就**严格按它取**：取不到即失败，
            # 不再回退到"猜字段"——否则字段写错时会悄悄取到别的字段（实测踩过）。
            node: Any = data
            for part in [p for p in self.text_path.split(".") if p]:
                if not isinstance(node, dict) or part not in node:
                    return ""
                node = node[part]
            return str(node)
        for key in ("text", "result", "content", "ocr_text"):
            if isinstance(data.get(key), str):
                return data[key]
        # 兼容 {"data": {"text": ...}} 这类包装
        for value in data.values():
            if isinstance(value, dict):
                nested = self._pick_text(value)
                if nested:
                    return nested
        return ""

    @staticmethod
    def _keys(data: Any) -> list[str]:
        return sorted(data.keys()) if isinstance(data, dict) else [type(data).__name__]


class RapidOcrEngine:
    """RapidOCR（onnxruntime，模型随 Python 包分发，无需单独下载）。"""

    tier = "ocr_lite"
    name = "rapidocr"

    def __init__(self, lang: str = ""):
        self.lang = lang or _env("OCR_LANG", "ch")
        self._engine = None
        self._error = ""

    @property
    def available(self) -> bool:
        # 用 find_spec 表达"探测依赖是否存在"的意图（`import X` 只探测存在性时，
        # X 从未被使用，会被静态审查正确地报为未使用导入）。
        import importlib.util
        try:
            return importlib.util.find_spec("rapidocr_onnxruntime") is not None
        except Exception:
            return False

    def _ensure(self):
        if self._engine is not None:
            return self._engine
        import numpy as np
        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        self._engine = (RapidOCR(), np, Image)
        return self._engine

    def recognize_png(self, png: bytes, page: int = 1) -> tuple[str, str]:
        try:
            engine, np, Image = self._ensure()
            img = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
            result, _ = engine(img)
            lines = [seg[1] for seg in (result or []) if len(seg) > 1 and seg[1]]
            return "\n".join(lines), ""
        except Exception as exc:
            return "", f"RapidOCR 失败：{type(exc).__name__}: {exc}"


class TesseractEngine:
    """Tesseract CLI（本地已安装 tesseract 时可用）。"""

    tier = "ocr_lite"

    def __init__(self, cmd: str = "", lang: str = ""):
        self.cmd = cmd or _env("OCR_TESSERACT_CMD", "") or (shutil.which("tesseract") or "")
        self.lang = lang or _env("OCR_LANG", "chi_sim+eng")
        self.name = "tesseract"

    @property
    def available(self) -> bool:
        return bool(self.cmd and Path(self.cmd).exists())

    def recognize_png(self, png: bytes, page: int = 1) -> tuple[str, str]:
        if not self.available:
            return "", "未找到 tesseract 可执行文件"
        tmpdir = tempfile.mkdtemp(prefix="ocr-")
        try:
            img_path = Path(tmpdir) / f"page{page}.png"
            img_path.write_bytes(png)
            out_base = Path(tmpdir) / "out"
            proc = subprocess.run(
                [self.cmd, str(img_path), str(out_base), "-l", self.lang],
                capture_output=True, timeout=self.timeout if hasattr(self, "timeout") else TIMEOUT_S)
            out_txt = Path(str(out_base) + ".txt")
            if proc.returncode != 0 and not out_txt.exists():
                return "", f"tesseract 退出码 {proc.returncode}：{proc.stderr[:120]!r}"
            return (out_txt.read_text(encoding="utf-8", errors="replace") if out_txt.exists() else ""), ""
        except subprocess.TimeoutExpired:
            return "", "tesseract 超时"
        except Exception as exc:
            return "", f"tesseract 失败：{type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class PaddleOcrEngine:
    """PaddleOCR（精度最高，但需较大的模型与依赖，企业内网常不可得）。"""

    tier = "ocr_full"
    name = "paddle"

    def __init__(self, lang: str = ""):
        self.lang = lang or _env("OCR_LANG", "ch")
        self._engine = None

    @property
    def available(self) -> bool:
        import importlib.util
        try:
            return importlib.util.find_spec("paddleocr") is not None
        except Exception:
            return False

    def recognize_png(self, png: bytes, page: int = 1) -> tuple[str, str]:
        try:
            import numpy as np
            from PIL import Image
            from paddleocr import PaddleOCR  # type: ignore
            if self._engine is None:
                self._engine = PaddleOCR(use_angle_cls=True, lang=self.lang, show_log=False)
            img = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
            result = self._engine.ocr(img, cls=True)
            lines = []
            for page_res in (result or []):
                for item in (page_res or []):
                    if len(item) > 1 and item[1]:
                        lines.append(str(item[1][0]))
            return "\n".join(lines), ""
        except Exception as exc:
            return "", f"PaddleOCR 失败：{type(exc).__name__}: {exc}"


# 顺序即优先级：精度高者优先，但都要"装得上"才会被选中
_ENGINE_ORDER = ("paddle", "rapidocr", "tesseract", "http")


def _build(name: str) -> OcrEngine:
    if name == "paddle":
        return PaddleOcrEngine()
    if name == "rapidocr":
        return RapidOcrEngine()
    if name == "tesseract":
        return TesseractEngine()
    if name == "http":
        return HttpOcrEngine()
    return NoOcrEngine()


_CACHE: dict[str, Any] = {}


def active_engine(force: str = "") -> OcrEngine:
    """按配置/优先级选出可用引擎（结果缓存，避免每次调用都探测）。"""
    want = (force or _env("OCR_ENGINE", "auto")).lower()
    key = f"engine:{want}"
    if key in _CACHE:
        return _CACHE[key]
    if want in ("none", "off", "disable"):
        eng: OcrEngine = NoOcrEngine()
    elif want == "auto":
        eng = NoOcrEngine()
        for cand in _ENGINE_ORDER:
            e = _build(cand)
            if e.available:
                eng = e
                break
    else:
        e = _build(want)
        eng = e if e.available else NoOcrEngine()
    _CACHE[key] = eng
    return eng


def describe() -> dict:
    """当前 OCR 档位（供诊断与证据留痕）。"""
    eng = active_engine()
    probes = {}
    for cand in _ENGINE_ORDER:
        try:
            probes[cand] = bool(_build(cand).available)
        except Exception:
            probes[cand] = False
    return {"configured": _env("OCR_ENGINE", "auto").lower(), "engine": eng.name,
            "tier": eng.tier, "available_engines": probes,
            "dpi": DEFAULT_DPI, "max_pages": MAX_PAGES,
            "http_url_configured": bool(_env("OCR_HTTP_URL"))}


# ============================================================ PDF 渲染（真实实现）

def iter_pdf_pages(path: Path, dpi: int = 0, max_pages: int = 0, first: int = 0):
    """**逐页**渲染 PDF（生成器）：返回 (总页数, 页码起始的生成器)。

    【第二次审查修正】原实现一次性把 max_pages 页全部渲染成 PNG 再返回列表：
    200 DPI 的 A4 页约 8MB PNG，30 页就是 240MB 常驻内存（企业扫描件很常见），
    属于"扫描件一多就把服务打爆"的隐患。改为**用一页渲染一页、用完即释放**。
    """
    dpi = dpi or DEFAULT_DPI
    max_pages = max_pages or MAX_PAGES
    try:
        import pypdfium2 as pdfium
    except Exception as exc:
        raise RuntimeError(f"缺少 PDF 渲染依赖 pypdfium2：{exc}") from exc
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise RuntimeError(f"PDF 打开失败：{type(exc).__name__}: {exc}") from exc

    total = len(pdf)
    last = min(total, first + max_pages)

    def _gen():
        try:
            for idx in range(first, last):
                page = pdf[idx]
                bitmap = None
                try:
                    bitmap = page.render(scale=dpi / 72.0)
                    pil = bitmap.to_pil()
                    buf = io.BytesIO()
                    pil.save(buf, format="PNG")
                    yield idx + 1, buf.getvalue()
                finally:
                    # 及时释放位图（pypdfium2 的位图持有非托管内存）
                    try:
                        if bitmap is not None:
                            bitmap.close()
                    except Exception:
                        pass
                    try:
                        page.close()
                    except Exception:
                        pass
        finally:
            try:
                pdf.close()
            except Exception:
                pass

    return total, _gen()


def render_pdf_pages(path: Path, dpi: int = 0, max_pages: int = 0,
                     first: int = 0) -> tuple[list[bytes], int, str]:
    """把 PDF 页面渲染为 PNG（**兼容保留**：自检与测试用的小批量场景）。

    大文档请用 `iter_pdf_pages`（流式，不驻留内存）。
    """
    try:
        total, gen = iter_pdf_pages(path, dpi=dpi, max_pages=max_pages, first=first)
    except RuntimeError as exc:
        return [], 0, str(exc)
    pngs = [png for _, png in gen]
    return pngs, total, ""


def ocr_pdf(path: Path, force_engine: str = "", dpi: int = 0,
            max_pages: int = 0) -> OcrResult:
    """扫描件 PDF → 文本（**流式**：渲染一页、识别一页、立即释放）。"""
    eng = active_engine(force_engine)
    if not eng.available:
        return OcrResult(False, engine=eng.name, tier="none",
                         warnings=["本机无可用 OCR 引擎；扫描件仍需人工处理"],
                         error="OCR 不可用")
    try:
        total, gen = iter_pdf_pages(path, dpi=dpi, max_pages=max_pages)
    except RuntimeError as exc:
        return OcrResult(False, engine=eng.name, tier=eng.tier, warnings=[str(exc)],
                         error=str(exc))
    texts, warns, ocr_pages, rendered = [], [], 0, 0
    for page_no, png in gen:
        rendered += 1
        text, perr = eng.recognize_png(png, page=page_no)
        del png                     # 立刻放手，别把整本合同的位图都留在内存里
        if perr:
            warns.append(f"第 {page_no} 页 OCR 失败：{perr}")
            continue
        if text.strip():
            texts.append(text.strip())
            ocr_pages += 1
    if total > rendered:
        warns.append(f"仅 OCR 前 {rendered} 页（共 {total} 页，受 OCR_MAX_PAGES 限制）")
    joined = "\n".join(texts)
    return OcrResult(bool(joined.strip()), text=joined, engine=eng.name, tier=eng.tier,
                     pages=total, pages_ocr=ocr_pages, warnings=warns,
                     error="" if joined.strip() else "OCR 未识别出任何文字")


def ocr_image_bytes(png: bytes, force_engine: str = "") -> OcrResult:
    """单张位图 → 文本（用于图片型合同/截图）。"""
    eng = active_engine(force_engine)
    if not eng.available:
        return OcrResult(False, engine=eng.name, tier="none",
                         warnings=["本机无可用 OCR 引擎"], error="OCR 不可用")
    text, err = eng.recognize_png(png, page=1)
    return OcrResult(bool(text.strip()), text=text.strip(), engine=eng.name, tier=eng.tier,
                     pages=1, pages_ocr=1 if text.strip() else 0,
                     warnings=[err] if err else [], error=err)
