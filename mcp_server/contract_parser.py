#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
合同文档解析（本项目自有，MCP 服务使用）

支撑的输入与降级链（对应方案设计 §7 与 D0-G5 定档结论）：
    1) 原生文本 PDF  —— pdfplumber（文本 + 表格抽取）
    2) Word .docx    —— python-docx（段落 + 表格）
    3) 纯文本        —— 直接分段
    4) 扫描件（无文本层）—— **先尝试 OCR**（`mcp_server/ocr.py` 的可插拔引擎：
       PaddleOCR → RapidOCR → Tesseract → 企业 OCR 服务 → none）。
       本机无任何 OCR 依赖时档位为 none，行为与升级前完全一致（显式低置信度 + 转人工）。

关键纪律：
    - 解析结果不得由模型改写；抽取到什么就是什么
    - 表格必须转为 Markdown 保留结构；抽取失败要显式 `table_extract_failed=true`，不得静默丢弃
    - 低置信度是**信号**不是错误，必须原样传给工作流条件分支
    - OCR 文本同样只是"抽取结果"，必须带 `ocr_tier` 与告警，让下游知道它不是原生文本层
"""

import hashlib
import re
from pathlib import Path

from mcp_server import ocr as _ocr

# OCR 档位：native（原生文本）| ocr_lite | ocr_full | none（无可用引擎）
# 【审查修正后升级】原实现是**写死的常量** "native"，即"永不尝试 OCR"。
# 现在档位由 mcp_server/ocr.py 的 active_engine() 决定；真实档位在解析结果的
# `ocr_tier` 字段里返回（只反映"本次是否真的用了 OCR"，而不是"本机装没装"）。
OCR_TIER = "native"          # 兼容既有引用（仅用于展示/日志）

CLAUSE_START = re.compile(
    r"^\s*(第[一二三四五六七八九十百零〇\d]+条|[（(]?[一二三四五六七八九十]+[）)]|\d+[.、]|Article\s+\d+)")
MIN_TEXT_CHARS = 40          # 低于此判定"无文本层"
SUSPICIOUS_RATIO = 0.30      # CJK 占比过低视为可疑（乱码/字形缺失）


def _doc_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _table_to_markdown(rows: list[list]) -> str:
    """二维表 -> Markdown 表（首行作表头）。"""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [[("" if c is None else str(c).replace("\n", " ").strip()) for c in r] +
            [""] * (width - len(r)) for r in rows]
    head = norm[0]
    body = norm[1:] or [[""] * width]
    lines = ["| " + " | ".join(head) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _split_clauses(text: str) -> list[dict]:
    """按「第N条 / 一、 / 1.」切分条款。"""
    clauses, cur = [], None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = CLAUSE_START.match(line)
        if m:
            if cur:
                clauses.append(cur)
            cur = {"clause_id": f"C{len(clauses) + 1}", "heading": m.group(1), "text": line.strip()}
        else:
            if cur:
                cur["text"] += "\n" + line.strip()
            else:
                cur = {"clause_id": "C1", "heading": "", "text": line.strip()}
    if cur:
        clauses.append(cur)
    for c in clauses:
        c["char_count"] = len(c["text"])
    return clauses


def _confidence(text: str, clauses: list[dict], tables: list[dict], tier: str) -> tuple[float, list[str]]:
    """解析置信度与告警。

    评分口径（可解释、可调）：
        基线 0.5；识别到条款号 +0.25；正文有足够 CJK +0.15；抽出表格 +0.05；
        无文本层（扫描件）直接压到 0.1；出现可疑乱码 -0.3。
    """
    warns: list[str] = []
    score = 0.5
    recognized = sum(1 for c in clauses if c["heading"])
    if recognized:
        score += 0.25
    if len(text) >= MIN_TEXT_CHARS:
        score += 0.15
    if tables:
        score += 0.05
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    ratio = cjk / max(1, len(text or ""))
    if text and ratio < SUSPICIOUS_RATIO:
        score -= 0.3
        warns.append(f"中文字符占比偏低({ratio:.2f})，可能是乱码或字形缺失，建议人工确认")
    if tier != "native":
        score -= 0.2
    # 只有"没有任何可用文本"才压到最低档（扫描件 / 空文档）。
    # 注意不要用"文本较短"来判定低置信度：短条款（如"附件一：见下表。"）
    # 若已识别到条款编号，仍应视为可解析（P2 测试实测踩过这个误判）。
    if not text or len(text.strip()) < 10:
        score = 0.1
        warns.append("未检测到文本层（扫描件或空文档）；当前档位为无 OCR 模式，需转人工处理")
    if not recognized and clauses:
        warns.append("未识别到标准条款编号，建议人工确认分段")
    if not clauses:
        warns.append("未能切分出任何条款")
    return round(max(0.0, min(1.0, score)), 2), warns


# ---------------------------------------------------------------- PDF

def parse_pdf(path: Path, doc_id: str = "") -> dict:
    import pdfplumber

    data = path.read_bytes()
    pages_text: list[str] = []
    tables: list[dict] = []
    table_failed = False
    with pdfplumber.open(str(path)) as doc:
        page_count = len(doc.pages)
        for idx, page in enumerate(doc.pages, start=1):
            try:
                pages_text.append(page.extract_text() or "")
            except Exception:
                pages_text.append("")
            try:
                for t_idx, table in enumerate(page.extract_tables() or [], start=1):
                    if not table or not any(any(c for c in row) for row in table):
                        continue
                    md = _table_to_markdown(table)
                    tables.append({
                        "table_id": f"T{len(tables) + 1}",
                        "markdown": md,
                        "pages": str(idx),
                        "rows": len(table),
                        "cols": max(len(r) for r in table),
                        "source": f"page{idx}-table{t_idx}",
                    })
            except Exception:
                table_failed = True

    text = "\n".join(pages_text).strip()
    ocr_info: dict = {}
    # 【重要】tier 初值必须是 native：只有**真的调用了 OCR 并拿到文本**才提升档位。
    # 若写成 current_tier()，那么"本机装了 OCR 引擎但从没用上"的原生文本 PDF
    # 也会被标注成 ocr_lite，把解析来源说错（审计口径问题）。
    tier = "native"
    warns: list[str] = []
    # 【OCR 升级】无文本层（扫描件）时尝试 OCR；引擎与渲染都在 ocr.py 中实现。
    # 注意：这里**只补文本**，不改变"抽取到什么就是什么"的纪律；
    # OCR 结果会带上 ocr_tier / ocr_pages / 告警，供下游判断可信度。
    if len(text) < MIN_TEXT_CHARS:
        res = _ocr.ocr_pdf(path)
        ocr_info = res.as_dict()
        if res.ok:
            text = res.text.strip()
            tier = res.tier
            warns.append(f"文本层缺失，已用 OCR（{res.engine}）识别 {res.pages_ocr} 页")
            warns.extend(res.warnings)
        else:
            tier = "none"
            warns.append("文本层缺失且 OCR 不可用：" + (res.error or "未知原因"))
            warns.extend(res.warnings)

    clauses = _split_clauses(text)
    conf, conf_warns = _confidence(text, clauses, tables, tier)
    warns.extend(conf_warns)
    if table_failed:
        warns.append("部分表格抽取失败，需人工核对")
    return {
        "doc_id": doc_id or _doc_id(data),
        "doc_type": "未知",
        "source_format": "pdf",
        "page_count": page_count,
        "ocr_tier": tier,
        "ocr": ocr_info,
        "confidence": conf,
        "char_count": len(text),
        "clause_count": len(clauses),
        "clauses": clauses,
        "tables": tables,
        "table_extract_failed": bool(table_failed),
        "warnings": warns,
        "engine": f"pdfplumber+{tier}",
    }


# ---------------------------------------------------------------- DOCX

def parse_docx(path: Path, doc_id: str = "") -> dict:
    import docx

    data = path.read_bytes()
    d = docx.Document(str(path))
    paragraphs = [p.text for p in d.paragraphs if (p.text or "").strip()]
    text = "\n".join(paragraphs).strip()

    tables: list[dict] = []
    failed = False
    try:
        for t in d.tables:
            rows = [[cell.text for cell in row.cells] for row in t.rows]
            if not rows or not any(any(c for c in r) for r in rows):
                continue
            tables.append({
                "table_id": f"T{len(tables) + 1}",
                "markdown": _table_to_markdown(rows),
                "pages": "n/a",
                "rows": len(rows),
                "cols": max(len(r) for r in rows),
                "source": f"docx-table{len(tables) + 1}",
            })
    except Exception:
        failed = True

    clauses = _split_clauses(text)
    conf, warns = _confidence(text, clauses, tables, OCR_TIER)
    if failed:
        warns.append("Word 表格抽取失败，需人工核对")
    return {
        "doc_id": doc_id or _doc_id(data),
        "doc_type": "未知",
        "source_format": "docx",
        "page_count": None,
        "ocr_tier": OCR_TIER,
        "confidence": conf,
        "char_count": len(text),
        "clause_count": len(clauses),
        "clauses": clauses,
        "tables": tables,
        "table_extract_failed": bool(failed),
        "warnings": warns,
        "engine": f"python-docx/{OCR_TIER}",
    }


# ---------------------------------------------------------------- TEXT

def parse_text(text: str, doc_id: str = "") -> dict:
    text = (text or "").strip()
    clauses = _split_clauses(text)
    conf, warns = _confidence(text, clauses, [], OCR_TIER)
    return {
        "doc_id": doc_id or _doc_id(text.encode("utf-8")),
        "doc_type": "未知",
        "source_format": "text",
        "page_count": None,
        "ocr_tier": OCR_TIER,
        "confidence": conf,
        "char_count": len(text),
        "clause_count": len(clauses),
        "clauses": clauses,
        "tables": [],
        "table_extract_failed": False,
        "warnings": warns,
        "engine": f"text-split/{OCR_TIER}",
    }


# ---------------------------------------------------------------- 入口

def parse(path_or_text: str = "", as_text: bool = False, doc_id: str = "") -> dict:
    """统一入口：按扩展名分派；文件不存在且不像路径则按纯文本处理。"""
    if as_text:
        return parse_text(path_or_text, doc_id)
    p = Path(path_or_text)
    if p.exists() and p.is_file():
        suffix = p.suffix.lower()
        if suffix == ".pdf":
            return parse_pdf(p, doc_id)
        if suffix in (".docx", ".doc"):
            if suffix == ".doc":
                return {"doc_id": doc_id, "source_format": "doc", "confidence": 0.0,
                        "clauses": [], "tables": [], "table_extract_failed": False,
                        "ocr_tier": OCR_TIER,
                        "warnings": ["旧版 .doc 不被 python-docx 支持，请另存为 .docx 或 PDF"]}
            return parse_docx(p, doc_id)
        if suffix in (".txt", ".md"):
            return parse_text(p.read_text(encoding="utf-8", errors="replace"), doc_id)
        return {"doc_id": doc_id, "source_format": suffix or "unknown", "confidence": 0.0,
                "clauses": [], "tables": [], "table_extract_failed": False, "ocr_tier": OCR_TIER,
                "warnings": [f"不支持的文件类型：{suffix}"]}
    if path_or_text and not re.search(r"[\\/]|\.(pdf|docx|txt|md)$", path_or_text, re.I):
        return parse_text(path_or_text, doc_id)
    return {"doc_id": doc_id, "source_format": "missing", "confidence": 0.0,
            "clauses": [], "tables": [], "table_extract_failed": False, "ocr_tier": OCR_TIER,
            "warnings": [f"文件不存在：{path_or_text}"]}
