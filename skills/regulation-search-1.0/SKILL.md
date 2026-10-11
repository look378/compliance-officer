---
name: regulation-search
description: 法规与内控条款检索：向量 + 关键词混合召回、重排取 top-k，返回带条文编号与出处的适用法条，供风险标注引用。
tags: [regulation, rag, retrieval, 法条, 检索]
version: 1.0
metadata:
  openclaw:
    emoji: 📚
---

# 法规检索技能

## 用途

给定条款文本，返回**最相关的法规/内控条款**，每条必须带 `regulation_ref`（条文编号）与 `source`（法规名称），用于风险标注的引用依据。

## 调用方式

```
search_regulations(query="<条款文本>", top_k=5, filters={"domain": "劳动|金融销售|数据合规|..."})
```

## 检索链路（三段式，任一段失败必须记录）

1. **召回**：向量相似度 + 关键词（BM25）双路召回，各取 top-N 后合并去重。
2. **重排**：交叉编码器重排，取 top-k（默认 5）。
3. **阈值判定**：最高分低于阈值 → 视为**空检索**。

## 空检索与低分的强制处置

- 空检索或全部分数低于阈值时：**降级为纯规则表标注**。
- 报告中必须显式声明「**未检索到适用法条**」，并降低该条的结论强度。
- 严禁在无法条依据时编造条文编号或法规名称。

## 输出契约

```json
{
  "query": "...",
  "hits": [
    {"regulation_ref": "《XX法》第N条", "source": "...", "domain": "...",
     "text": "...", "vec_score": 0.0, "bm25_score": 0.0, "rerank_score": 0.0}
  ],
  "empty_result": false,
  "degraded_to_rules_only": false
}
```

## 纪律

- **无引用不判风险**：调用方拿不到 `regulation_ref` 时不得把该条标为风险。
- 检索结果**不得截断条文编号**；正文过长时可截断正文但必须保留编号与出处。
- 同一 query 的检索结果缓存到本次审查上下文，避免重复计费。
