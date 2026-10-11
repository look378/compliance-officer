#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
法规与内控条款库（本项目自有，MCP 服务使用）

数据来源：`data/regulations/regulations.csv`（主数据源，逐条可审阅）
          `data/regulations/seed_regulations.json`（早期种子，作为兜底）

为什么要独立于运行时框架的记忆系统（方案设计 §4）：
    框架的长期记忆注入预算默认 1500 token、单块正文硬截 200 字符、检索为纯词面 TF-IDF
    且启动只装载 100 条；法规条文会被截断且同义改写即漏检。因此法规必须走本项目自建检索。

D0 检索实现：关键词 + 中文字符 2-gram 重叠打分（确定性、零依赖、可单测）。
P3 起可挂向量检索与重排，但**必须与本通道分开报告指标**，不得合并成一个"准确率"。
"""

import csv
import hashlib
import re
from pathlib import Path

# 本文件位于 <project>/mcp_server/，因此项目根是上一级
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = PROJECT_ROOT / "data" / "regulations" / "regulations.csv"
JSON_PATH = PROJECT_ROOT / "data" / "regulations" / "seed_regulations.json"

_cache: dict | None = None


def _read_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            ref = (row.get("regulation_ref") or "").strip()
            if not ref:
                continue
            rows.append({
                "regulation_ref": ref,
                "source": (row.get("source") or "").strip(),
                "domain": (row.get("domain") or "").strip(),
                "keywords": (row.get("keywords") or "").strip(),
                "text": (row.get("text") or "").strip(),
            })
    return rows


def _read_json(path: Path) -> list[dict]:
    import json
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("regulation_ref")]


def _digest(items: list[dict]) -> str:
    payload = "|".join(f"{i['regulation_ref']}::{i['text'][:80]}" for i in sorted(
        items, key=lambda x: x["regulation_ref"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def load(force: bool = False) -> dict:
    """加载法规库（带缓存）。返回 {items, source, digest, by_ref}。"""
    global _cache
    if _cache is not None and not force:
        return _cache

    items: list[dict] = []
    source = "none"
    if CSV_PATH.exists():
        items = _read_csv(CSV_PATH)
        source = CSV_PATH.name
    if not items and JSON_PATH.exists():
        items = _read_json(JSON_PATH)
        source = JSON_PATH.name

    _cache = {
        "items": items,
        "source": source,
        "digest": _digest(items) if items else "empty",
        "by_ref": {i["regulation_ref"]: i for i in items},
    }
    return _cache


def count() -> int:
    return len(load()["items"])


def domains() -> list[str]:
    return sorted({i["domain"] for i in load()["items"] if i.get("domain")})


def tokens(text: str) -> list[str]:
    """中文 2-gram + 英文/数字词，粗切分（P3 起可替换为分词器）。"""
    cn = re.findall(r"[\u4e00-\u9fff]", text or "")
    bigrams = ["".join(cn[i:i + 2]) for i in range(max(0, len(cn) - 1))]
    words = re.findall(r"[A-Za-z0-9]+", text or "")
    return bigrams + [w.lower() for w in words]


def search(query: str, top_k: int = 5, threshold: float = 0.05,
           domain: str | None = None) -> dict:
    """关键词/字符 2-gram 重叠检索。

    返回结构固定（供标注引用）：
        {query, hits[{regulation_ref, source, domain, text, score, vec_score, bm25_score,
                      rerank_score}], empty_result, degraded_to_rules_only, engine, library}
    空检索必须显式声明——调用方不得据此编造法条编号。
    """
    lib = load()
    q_tokens = set(tokens(query or ""))
    items = [i for i in lib["items"] if (not domain or i.get("domain") == domain)]

    # 统计每个 token 覆盖多少条法规：覆盖过广的 token 不具区分度（如"应当""合同"），
    # 予以剔除。否则 2-gram 词面匹配会把"毫不相关的查询"也算成命中（P2 自检实测发现）。
    df: dict[str, int] = {}
    item_tokens: list[set] = []
    for it in items:
        hay = f"{it.get('keywords','')}{it.get('text','')}{it.get('source','')}"
        toks = set(tokens(hay))
        item_tokens.append(toks)
        for t in toks:
            df[t] = df.get(t, 0) + 1
    common_cut = max(3, int(0.6 * max(1, len(items))))
    discriminative = lambda ts: {t for t in ts if df.get(t, 0) <= common_cut}  # noqa: E731
    q_eff = discriminative(q_tokens)
    if not q_eff:
        # 查询里全是通用词：不做强行命中，显式声明空结果
        q_eff = set()

    scored = []
    for it, d_tokens in zip(items, item_tokens):
        d_eff = discriminative(d_tokens)
        inter = q_eff & d_eff
        # 至少 2 个有区分度的 token 重合才算命中，抑制 2-gram 噪声
        if len(inter) < 2:
            continue
        kw_eff = discriminative(set(tokens(it.get("keywords", ""))))
        kw_hit = len(q_eff & kw_eff)
        score = round((len(inter) + 0.5 * kw_hit) / max(1, len(q_eff)), 4)
        if score >= threshold:
            scored.append((score, it))

    scored.sort(key=lambda x: (-x[0], x[1]["regulation_ref"]))
    hits = [{
        "regulation_ref": it["regulation_ref"],
        "source": it["source"],
        "domain": it["domain"],
        "text": it["text"],
        "score": s,
        "vec_score": None,       # 向量通道未启用（P3）
        "bm25_score": s,         # 当前为词面重叠分，作为 BM25 通道的占位
        "rerank_score": None,    # 重排未启用（P3）
    } for s, it in scored[:max(1, top_k)]]

    return {
        "query": query,
        "hits": hits,
        "empty_result": len(hits) == 0,
        "degraded_to_rules_only": len(hits) == 0,
        "engine": "keyword-ngram/v2(min-overlap=2,df-filter)",
        "debug": {"query_tokens": len(q_tokens), "effective_tokens": len(q_eff),
                  "common_token_cut": common_cut},
        "library": {"source": lib["source"], "count": len(lib["items"]), "digest": lib["digest"]},
    }
