#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
G5 门禁：文档解析与 OCR 档位验证（含降级链）

任务书要求的三档降级链：
    1) 完整 OCR（PaddleOCR + 结构还原）—— 体积大，Windows 需 VC++ 运行库
    2) 轻量 OCR（RapidOCR / onnxruntime）—— 体积小，Windows 友好
    3) 无 OCR 模式 —— 仅原生文本 PDF/Word，扫描件标记低置信度走人工

本脚本**探测真实环境**而不是假定：逐项检查依赖可用性、体积、是否需要管理员权限，
并用一份**自造的扫描件式 PDF（图片型，无文本层）**实测文本层可提取性，
从而给出「本项目在本机应当采用哪一档」的证据化结论。

产出：docs/evidence/d0-g5-ocr-tier.json
"""

import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "d0-g5-ocr-tier.json"
FIXTURES = PROJECT_ROOT / ".runtime" / "workspace" / "d0_fixtures"

CANDIDATES = [
    ("pdfplumber", "原生文本 PDF 解析（首选）"),
    ("fitz", "PyMuPDF：快速原生文本抽取"),
    ("pypdf", "pypdf：纯 Python PDF 解析"),
    ("docx", "python-docx：Word 解析"),
    ("paddleocr", "完整 OCR（重）"),
    ("rapidocr_onnxruntime", "轻量 OCR（onnxruntime）"),
    ("PIL", "图像处理（生成扫描件用）"),
]


def probe() -> dict:
    """逐项探测依赖可用性与自述版本。"""
    import importlib
    out = {}
    for mod, desc in CANDIDATES:
        item = {"desc": desc, "importable": False, "version": None, "error": None}
        try:
            m = importlib.import_module(mod)
            item["importable"] = True
            item["version"] = str(getattr(m, "__version__", "") or "")
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"[:160]
        out[mod] = item
    return out


def make_fixtures() -> dict:
    """生成两份最小测试件：原生文本 PDF（有文本层）与图片型 PDF（无文本层，模拟扫描件）。

    仅使用已可用的依赖；若生成失败则记录原因，不视为门禁失败。
    """
    FIXTURES.mkdir(parents=True, exist_ok=True)
    res = {"dir": str(FIXTURES)}
    text = ("劳动合同（示范）\n第一条 本合同为固定期限劳动合同，期限三年。\n"
            "第二条 乙方月工作时长不超过 260 小时，甲方可安排加班。\n"
            "第三条 甲方有权无条件解除本合同，最终解释权归甲方所有。\n"
            "第四条 乙方违反保密义务的，违约金为合同总额的 50%。\n")

    # 1) 原生文本 PDF
    #    用 reportlab + Windows 自带中文字体生成，确保文本层是**真中文**而不是缺字形的方块
    try:
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        font_name = "Helvetica"
        for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc",
                     r"C:\Windows\Fonts\simhei.ttf"):
            try:
                pdfmetrics.registerFont(TTFont("CJK", cand))
                font_name = "CJK"
                res["cjk_font"] = cand
                break
            except Exception:
                continue
        p = FIXTURES / "native_text.pdf"
        c = canvas.Canvas(str(p))
        c.setFont(font_name, 12)
        y = 800
        for line in text.splitlines():
            c.drawString(60, y, line)
            y -= 22
        c.save()
        res["native_pdf"] = {"ok": True, "path": str(p), "bytes": p.stat().st_size,
                             "font": font_name}
    except Exception as exc:
        res["native_pdf"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 2) 图片型 PDF（无文本层）
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (1200, 420), "white")
        d = ImageDraw.Draw(img)
        font = None
        for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"):
            try:
                font = ImageFont.truetype(cand, 20)
                break
            except Exception:
                continue
        y = 30
        for line in text.splitlines():
            d.text((20, y), line, fill="black", font=font)
            y += 45
        png = FIXTURES / "scanned_page.png"
        img.save(png)
        pdf = FIXTURES / "scanned_image_only.pdf"
        img.save(pdf, "PDF", resolution=150)
        res["image_pdf"] = {"ok": True, "path": str(pdf), "png": str(png),
                            "bytes": pdf.stat().st_size}
    except Exception as exc:
        res["image_pdf"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return res


def try_extract_text_layer(pdf: Path) -> dict:
    """用「有文本层就抽得到、图片型就抽不到」来判定 OCR 档位必要性。"""
    out = {}
    # pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf)) as doc:
            txt = "\n".join((pg.extract_text() or "") for pg in doc.pages)
        out["pdfplumber"] = {"chars": len(txt.strip()), "head": txt.strip()[:80]}
    except Exception as exc:
        out["pdfplumber"] = {"error": f"{type(exc).__name__}: {exc}"[:120]}
    # pypdf
    try:
        from pypdf import PdfReader
        r = PdfReader(str(pdf))
        txt = "\n".join((pg.extract_text() or "") for pg in r.pages)
        out["pypdf"] = {"chars": len(txt.strip()), "head": txt.strip()[:80]}
    except Exception as exc:
        out["pypdf"] = {"error": f"{type(exc).__name__}: {exc}"[:120]}
    return out


def main() -> int:
    ev = {"gate": "G5", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "env": {"platform": platform.platform(), "python": sys.version.split()[0],
                  "executable": sys.executable, "cwd": str(Path.cwd())}}
    print(f"[G5] 环境: {ev['env']['platform']} | Python {ev['env']['python']}")

    ev["deps"] = probe()
    for mod, item in ev["deps"].items():
        flag = "可用" if item["importable"] else "缺失"
        print(f"   {mod:24s} {flag:4s} {item['version'] or ''} {item['error'] or ''}")

    ev["fixtures"] = make_fixtures()
    print(f"[G5] 测试件: {json.dumps(ev['fixtures'], ensure_ascii=False)[:200]}")

    # 文本层可提取性实测
    ev["text_layer"] = {}
    for key in ("native_pdf", "image_pdf"):
        entry = ev["fixtures"].get(key) or {}
        if entry.get("ok"):
            ev["text_layer"][key] = try_extract_text_layer(Path(entry["path"]))
        else:
            ev["text_layer"][key] = {"skipped": entry.get("error")}
    print(f"[G5] 文本层提取: {json.dumps(ev['text_layer'], ensure_ascii=False)[:400]}")

    # ---- 档位判定 ----
    can_native = bool(ev["deps"]["pdfplumber"]["importable"] or ev["deps"]["pypdf"]["importable"])
    has_full_ocr = bool(ev["deps"]["paddleocr"]["importable"])
    has_lite_ocr = bool(ev["deps"]["rapidocr_onnxruntime"]["importable"])
    native_chars = (ev["text_layer"].get("native_pdf", {}).get("pdfplumber", {}) or {}).get("chars", 0)
    image_chars = (ev["text_layer"].get("image_pdf", {}).get("pdfplumber", {}) or {}).get("chars", 0)

    if has_full_ocr:
        tier = "ocr_full"
    elif has_lite_ocr:
        tier = "ocr_lite"
    elif can_native:
        tier = "none"
    else:
        tier = "blocked"

    # ---- OCR 渲染与引擎链路（升级后的实测）----
    # 之前 G5 只判定"有没有装 OCR 包"；现在还要证明：
    #   ① 扫描件 PDF 能被**真实渲染**成位图（不需要模型，可离线验证）
    #   ② 配了 OCR_ENGINE=http 时，整条链路（渲染 → 送 OCR 服务 → 拼文本 → 切条款）真的通
    #   ③ 没引擎时行为与升级前一致（转人工，不抛异常、不误判）
    from mcp_server import contract_parser, ocr as ocr_mod

    ev["ocr"] = {"describe": ocr_mod.describe()}
    render_probe = {}
    for key in ("native_pdf", "image_pdf"):
        entry = ev["fixtures"].get(key) or {}
        if not entry.get("ok"):
            render_probe[key] = {"skipped": entry.get("error")}
            continue
        pngs, total, err = ocr_mod.render_pdf_pages(Path(entry["path"]), dpi=80, max_pages=2)
        render_probe[key] = {"pages": total, "rendered": len(pngs),
                             "png_ok": bool(pngs and pngs[0][:8] == b"\x89PNG\r\n\x1a\n"),
                             "error": err}
    ev["ocr"]["render"] = render_probe
    print(f"[G5] PDF 渲染: {json.dumps(render_probe, ensure_ascii=False)[:300]}")

    # 用一个"假 OCR 服务"证明链路（真实环境把 OCR_HTTP_URL 换成贵司 OCR 中台即可）
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Stub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            body = json.dumps({"text": "技术服务合同\n第一条 甲方委托乙方提供运维服务。\n"
                                       "第二条 乙方违反保密义务的，违约金为合同总额的 50%。"},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import os as _os
    saved = {k: _os.environ.get(k) for k in ("OCR_HTTP_URL", "OCR_ENGINE")}
    _os.environ["OCR_HTTP_URL"] = f"http://127.0.0.1:{srv.server_address[1]}/ocr"
    _os.environ["OCR_ENGINE"] = "http"
    ocr_mod._CACHE.clear()
    pipe = {}
    image_entry = ev["fixtures"].get("image_pdf") or {}
    if image_entry.get("ok"):
        parsed = contract_parser.parse_pdf(Path(image_entry["path"]))
        pipe = {"ocr_tier": parsed["ocr_tier"], "engine": (parsed.get("ocr") or {}).get("engine"),
                "pages_ocr": (parsed.get("ocr") or {}).get("pages_ocr"),
                "clauses": parsed["clause_count"], "confidence": parsed["confidence"],
                "warnings": parsed["warnings"][:3]}
    # 原生文本 PDF 不得被送去 OCR（关键回归）
    native_entry = ev["fixtures"].get("native_pdf") or {}
    native_not_ocr = None
    if native_entry.get("ok"):
        p2 = contract_parser.parse_pdf(Path(native_entry["path"]))
        native_not_ocr = {"ocr_tier": p2["ocr_tier"], "ocr_called": bool(p2.get("ocr"))}
    srv.shutdown()
    srv.server_close()
    for k, v in saved.items():
        if v is None:
            _os.environ.pop(k, None)
        else:
            _os.environ[k] = v
    ocr_mod._CACHE.clear()
    ev["ocr"]["pipeline"] = pipe
    ev["ocr"]["native_not_ocr"] = native_not_ocr
    print(f"[G5] OCR 链路（假 OCR 服务）: {json.dumps(pipe, ensure_ascii=False)[:260]}")

    ev["assessment"] = {
        "native_text_ok": can_native,
        "native_pdf_extracted_chars": native_chars,
        "image_only_pdf_extracted_chars": image_chars,
        "native_layer_confirmed": native_chars > 0,
        "image_layer_absent_confirmed": image_chars == 0,
        "paddleocr_available": has_full_ocr,
        "rapidocr_available": has_lite_ocr,
        "chosen_tier": tier,
        "tier_meaning": {
            "ocr_full": "完整 OCR：扫描件可结构化还原（含表格）",
            "ocr_lite": "轻量 OCR：扫描件可识别文本，表格结构还原能力较弱",
            "none": "无 OCR 模式：仅支持原生文本 PDF/Word；扫描件标记低置信度转人工",
            "blocked": "连原生文本解析都不可用，必须安装依赖后才能开工",
        }[tier],
    }
    print(f"[G5] 判定档位: {tier} —— {ev['assessment']['tier_meaning']}")

    verdicts = {
        "native_parse_available": can_native,
        "native_text_layer_ok": native_chars > 0,
        "image_needs_ocr_confirmed": image_chars == 0,
        "tier_decided": tier in ("ocr_full", "ocr_lite", "none"),
        # 【OCR 升级】渲染必须真实可用（离线可验证，不依赖 OCR 模型）
        "pdf_render_works": bool((ev["ocr"]["render"].get("image_pdf") or {}).get("png_ok")),
        # 配了 OCR 服务时整条链路必须通
        "ocr_pipeline_works": (pipe.get("engine") == "http" and pipe.get("clauses", 0) >= 2
                               and pipe.get("ocr_tier") in ("ocr_lite", "ocr_full")),
        # 原生文本 PDF 绝不调用 OCR
        "native_pdf_skips_ocr": (native_not_ocr is not None
                                 and native_not_ocr["ocr_tier"] == "native"
                                 and native_not_ocr["ocr_called"] is False),
    }
    ev["verdicts"] = verdicts
    ev["verdict"] = "PASS" if all(verdicts.values()) else "FAIL"
    ev["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print("\n[G5] 子项判定:")
    for k, v in verdicts.items():
        print(f"   {k}: {'PASS' if v else 'FAIL'}")
    print(f"[G5] 总判定: {ev['verdict']}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[G5] 证据: {EVIDENCE}")
    return 0 if ev["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
