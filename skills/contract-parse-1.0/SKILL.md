---
name: contract-parse
description: 合同文档解析：原生文本 PDF/Word 提取、扫描件 OCR 降级链、表格抽取为 Markdown、输出结构化条款列表与解析置信度。
tags: [contract, parse, ocr, table, 解析]
version: 1.0
metadata:
  openclaw:
    emoji: 📄
---

# 合同解析技能

## 用途

把一份合同文件（PDF / Word / 扫描件）转换为**结构化条款列表 + 表格 + 置信度**，供 `annotate_risk` 使用。

## 调用方式

解析脚本位于本技能目录，通过 `run_skill_script` 执行：

```
run_skill_script(skill_name="contract-parse", script="scripts/classify_risk.py", args="--text \"<待判定文本>\"")
```

> 脚本在技能自己的隔离 venv 中运行；首次使用前需 `/skills setup contract-parse` 安装依赖。

## 解析通道与降级链

| 优先级 | 通道 | 适用 | 失败处置 |
|---|---|---|---|
| 1 | 原生文本提取 | 文本型 PDF / Word | 提取为空 → 降级 2 |
| 2 | 高精度 OCR | 扫描件 | 安装失败或过重 → 降级 3 |
| 3 | 轻量 OCR | 扫描件（体积小、Windows 友好） | 仍失败 → 降级 4 |
| 4 | 无 OCR 模式 | 仅原生文本 | 扫描件标记「低置信度」转人工 |

**任何降级都必须记录在输出的 `ocr_tier` 字段中**，报告里要写清本次用了哪一档，不允许静默降级。

## 表格抽取要求

- 表格必须转为 Markdown，保留列头与合并信息。
- 价款、比例、期限、数量等数值不得舍入、不得省略单位。
- 跨页表格须拼接为一张，并标注来源页码区间。
- 抽取失败时**明确输出 `table_extract_failed: true`**，不得静默丢弃表格。

## 输出契约

```json
{
  "doc_id": "...",
  "doc_type": "劳动|采购|租赁|金融销售|技术服务|未知",
  "ocr_tier": "native|ocr_full|ocr_lite|none",
  "confidence": 0.0,
  "clauses": [{"clause_id": "1.1", "title": "...", "text": "...", "page": 1}],
  "tables": [{"table_id": "T1", "markdown": "...", "pages": "2-3"}],
  "warnings": ["..."]
}
```

## 纪律

- 解析结果**不得由模型改写**：抽取到什么就是什么，模型只做归类与标注。
- 低置信度不是错误，是**信号**：必须原样传递给工作流的条件分支，由人工决定。
