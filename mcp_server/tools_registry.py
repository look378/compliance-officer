#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
岗位能力工具集（MCP 工具契约层）

设计原则（方案设计 §3.1）：
    - 每个工具返回**结构化 JSON**，不返回自然语言；报告可读性由渲染层负责
    - 每个工具输出携带可追溯字段（rule_table_digest / regulation 编号 / trace_id）
    - 工具是"岗位能力的唯一入口"：工作流的 tool 节点与主 Agent 走的都是这里

工具清单（P2 完整版）：
    文档类   parse_contract  extract_tables
    检索类   search_regulations
    判定类   annotate_clause
    报告类   generate_report
    规则类   propose_rule  list_rule_proposals  approve_rule_proposal  reject_rule_proposal
    流程类   record_decision  list_pending  list_audit  metrics
    自描述   service_info
"""

import uuid
from datetime import datetime
from pathlib import Path

from mcp_server import contract_parser, queue_store, regulations_store, rules_store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "reports"

SEVERITY_LABEL = {"high": "高风险", "mid": "中风险", "low": "低风险"}


def _schema(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _s(desc: str, **kw) -> dict:
    d = {"type": "string", "description": desc}
    d.update(kw)
    return d


def _i(desc: str, **kw) -> dict:
    d = {"type": "integer", "description": desc}
    d.update(kw)
    return d


# ============================================================ 文档

def _parse_contract(path: str = "", text: str = "", doc_id: str = "") -> dict:
    if path:
        out = contract_parser.parse(path, doc_id=doc_id)
    elif text:
        out = contract_parser.parse(text, as_text=True, doc_id=doc_id)
    else:
        return {"ok": False, "errors": ["必须提供 path 或 text 之一"]}
    out["ok"] = True
    # 表单一等公民：没有表格时显式说明，而不是让调用方以为"漏了"
    out["tables_found"] = len(out.get("tables", []))
    out["low_confidence"] = out.get("confidence", 0) < 0.6
    return out


def _extract_tables(path: str = "", text: str = "", doc_id: str = "") -> dict:
    parsed = _parse_contract(path=path, text=text, doc_id=doc_id)
    if not parsed.get("ok"):
        return parsed
    tables = parsed.get("tables", [])
    return {
        "ok": True,
        "doc_id": parsed.get("doc_id"),
        "source_format": parsed.get("source_format"),
        "ocr_tier": parsed.get("ocr_tier"),
        "table_count": len(tables),
        "tables": tables,
        "table_extract_failed": parsed.get("table_extract_failed", False),
        "note": ("未抽取到表格。可能是文档本身无表格，或为扫描件（当前档位 "
                 f"{parsed.get('ocr_tier')}，不做 OCR）。") if not tables else "",
        "warnings": parsed.get("warnings", []),
    }


# ============================================================ 检索

def _search_regulations(query: str, top_k: int = 5, domain: str = "") -> dict:
    res = regulations_store.search(query, top_k=top_k, domain=domain or None)
    res["ok"] = True
    return res


# ============================================================ 判定

def _annotate_clause(clause_text: str, clause_id: str = "") -> dict:
    out = rules_store.annotate(clause_text, clause_id)
    out["ok"] = True
    out["engine"] = "rules/v1"
    return out


def _annotate_document(path: str = "", text: str = "", doc_id: str = "",
                       max_clauses: int = 200) -> dict:
    """整篇标注：解析 + 逐条规则判定 + 汇总分级。

    返回同时包含**决策依据字段**（overall_risk_level / decision_summary / hit_rule_ids），
    便于调用方（工作流服务端）把它们写入工作流状态、驱动条件边与人工节点提问。

    【审查修正 P1】原实现有两处会让"什么都没读到"变成"低风险、直接办结"：
      1) `clauses = parsed["clauses"][:max_clauses]` 静默截断：长合同只审前 200 条，
         后面的条款从不判定，调用方也看不出被截断；
      2) 解析置信度低（扫描件/无文本层/low_confidence）时仍然照常给等级，
         若恰好没有命中规则就得到 overall_risk_level="low"，工作流会把 run 直接
         归档为"低风险办结"，人工复核被整段绕过。
    现在：
      - 0 条款一律 `ok=False`（无法判定就不是低风险，必须转人工/报错）；
      - 截断/解析失败/低置信度都会把等级**提升到至少 mid**，并带上 forced_human_review 标记，
        使工作流按 `overall_risk_level != low` 路由到人工节点（fail-safe，不 fail-open）。
    """
    parsed = _parse_contract(path=path, text=text, doc_id=doc_id)
    if not parsed.get("ok"):
        return parsed

    all_clauses = parsed.get("clauses", []) or []
    truncated = len(all_clauses) > max_clauses
    clauses = all_clauses[:max_clauses]
    if not clauses:
        # 无条款 = 无判定依据。绝不能返回 low（那等于"未审即通过"）。
        return {
            "ok": False,
            "doc_id": parsed.get("doc_id"),
            "source_format": parsed.get("source_format"),
            "ocr_tier": parsed.get("ocr_tier"),
            "confidence": parsed.get("confidence"),
            "clause_count": 0,
            "tables_found": parsed.get("tables_found", 0),
            "warnings": parsed.get("warnings", []),
            "errors": ["未从文档中提取到任何条款，无法判定风险等级（扫描件需 OCR 或转人工处理）"],
            "advisory_only": True,
        }

    results = []
    for c in clauses:
        ann = rules_store.annotate(c.get("text", ""), c.get("clause_id", ""))
        item = {k: ann[k] for k in ("clause_id", "clause_text", "risk_level", "risk_hits",
                                    "has_citation")}
        item["heading"] = c.get("heading", "")
        # 命中规则后附上可引用的法条（法规库检索）
        if ann["risk_hits"]:
            refs = []
            for h in ann["risk_hits"]:
                cited = (h.get("regulation_ref") or "").strip()
                if cited:
                    refs.append(cited)
            item["regulation_refs"] = sorted(set(refs))
        results.append(item)

    levels = [r["risk_level"] for r in results]
    overall = "high" if "high" in levels else ("mid" if "mid" in levels else "low")

    # 降级保护：解析质量不足或存在未判定条款时，不允许给出"低风险"结论。
    quality_risks = []
    if parsed.get("table_extract_failed"):
        quality_risks.append("表格抽取失败")
    if parsed.get("low_confidence"):
        quality_risks.append(f"解析置信度偏低（{parsed.get('confidence')}）")
    if truncated:
        quality_risks.append(f"条款超过 {max_clauses} 条上限，未判定 {len(all_clauses) - max_clauses} 条")
    forced_human_review = bool(quality_risks)
    if forced_human_review and overall == "low":
        overall = "mid"

    risky = [r for r in results if r["risk_level"] in ("mid", "high")]
    rule_hits = sorted({h["rule_id"] for r in results for h in r.get("risk_hits", [])})
    # 供工作流使用的紧凑摘要（可作为工具节点的输出字段，进而驱动条件边）：
    # 工作流的模板插值只能拿"字符串型"状态字段，因此这里同时给出可读文本与离散等级。
    decision_summary = (
        f"合同 {parsed.get('doc_id')}｜解析档位 {parsed.get('ocr_tier')}"
        f"｜置信度 {parsed.get('confidence')}"
        f"｜条款 {len(clauses)} 条，风险条款 {len(risky)} 条，整体等级 {overall}"
        f"｜命中规则 {','.join(rule_hits) if rule_hits else '无'}"
    )
    if forced_human_review:
        decision_summary += f"｜已强制转人工（{'；'.join(quality_risks)}）"
    warnings = list(parsed.get("warnings", []) or [])
    if truncated:
        warnings.append(f"条款被截断：仅判定前 {max_clauses} 条（共 {len(all_clauses)} 条）")
    return {
        "ok": True,
        "doc_id": parsed.get("doc_id"),
        "source_format": parsed.get("source_format"),
        "ocr_tier": parsed.get("ocr_tier"),
        "confidence": parsed.get("confidence"),
        "low_confidence": parsed.get("low_confidence"),
        "clause_count": len(clauses),
        "clause_count_total": len(all_clauses),
        "truncated": truncated,
        "forced_human_review": forced_human_review,
        "quality_risks": quality_risks,
        "risky_clause_count": len(risky),
        "overall_risk_level": overall,
        "hit_rule_ids": rule_hits,
        "decision_summary": decision_summary,
        "rule_table_digest": rules_store.digest(),
        "annotations": results,
        "tables_found": parsed.get("tables_found", 0),
        "warnings": warnings,
        "advisory_only": True,
    }


# ============================================================ 报告

def _render_markdown(ctx: dict) -> str:
    doc = ctx.get("doc_id", "unknown")
    ann = ctx.get("annotation") or {}
    lines = [
        f"# 合同合规审查报告（{doc}）",
        "",
        f"- 报告编号：{ctx.get('report_id')}",
        f"- 生成时间：{ctx.get('generated_at')}",
        f"- 合同来源：{ann.get('source_format', 'n/a')}｜解析档位：{ann.get('ocr_tier', 'n/a')}"
        f"｜解析置信度：{ann.get('confidence', 'n/a')}",
        f"- 规则表指纹：{ann.get('rule_table_digest', 'n/a')}",
        f"- 审查结论：**{SEVERITY_LABEL.get(ann.get('overall_risk_level'), '未判定')}**"
        f"（条款 {ann.get('clause_count', 0)} 条，其中风险条款 {ann.get('risky_clause_count', 0)} 条）",
        "",
        "## 一、执行摘要",
        "",
        ctx.get("summary") or "（无）",
        "",
        "## 二、风险条款与修改建议",
        "",
    ]
    risky = [a for a in ann.get("annotations", []) if a.get("risk_hits")]
    if not risky:
        lines.append("未发现达到风险阈值的条款。")
    for a in risky:
        lines.append(f"### {a['clause_id']} {a.get('heading', '')} — "
                     f"{SEVERITY_LABEL.get(a['risk_level'], a['risk_level'])}")
        lines.append("")
        lines.append(f"- 原文：{a.get('clause_text', '')[:300]}")
        for h in a.get("risk_hits", []):
            lines.append(f"- 命中规则：`{h['rule_id']}`（v{h.get('rule_version')}，{h.get('severity')}）"
                         f"，匹配片段「{h.get('match', '')}」")
            if h.get("regulation_ref"):
                lines.append(f"- 依据法条：{h['regulation_ref']}")
            if h.get("suggestion"):
                lines.append(f"- 修改建议：{h['suggestion']}")
        lines.append("")
    lines += ["## 三、审核链", ""]
    for item in ctx.get("audit_trail", []) or []:
        lines.append(f"- {item}")
    lines += [
        "",
        "## 四、声明",
        "",
        "1. 本报告的自动判定**仅供参考**，高风险与中风险结论必须经人工确认后方可作为处理依据。",
        "2. 所有风险结论均附规则编号与法条编号；无法条依据的条目不判为风险。",
        "3. 报告内容与规则表版本绑定，规则变更后需重新审查。",
        "",
        f"> trace_id: {ctx.get('trace_id', 'n/a')}",
    ]
    return "\n".join(lines)


def _render_pdf(markdown: str, out_path: Path) -> tuple[bool, str]:
    """Markdown -> PDF（reportlab）。中文字体不可用时返回失败原因，由调用方降级为 Markdown。"""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
    except Exception as exc:
        return False, f"reportlab 不可用：{exc}"

    font = "Helvetica"
    for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"):
        try:
            pdfmetrics.registerFont(TTFont("CJK", cand))
            font = "CJK"
            break
        except Exception:
            continue

    try:
        c = canvas.Canvas(str(out_path), pagesize=A4)
        width, height = A4
        c.setFont(font, 10)
        y = height - 50
        for raw in markdown.splitlines():
            line = raw
            while len(line) > 0:
                chunk, line = line[:46], line[46:]
                if y < 50:
                    c.showPage()
                    c.setFont(font, 10)
                    y = height - 50
                c.drawString(40, y, chunk)
                y -= 15
            y -= 3
            if y < 50:
                c.showPage()
                c.setFont(font, 10)
                y = height - 50
        c.save()
        return True, ""
    except Exception as exc:
        return False, f"PDF 渲染失败：{exc}"


def _generate_report(doc_id: str = "", annotation: dict | None = None,
                     summary: str = "", trace_id: str = "",
                     audit_trail: list | None = None, formats: str = "md,pdf") -> dict:
    REPORT_ID = f"RPT-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"
    ann = annotation or {}
    if not ann and doc_id:
        return {"ok": False, "errors": ["必须提供 annotation（来自 annotate_document）"]}

    ctx = {
        "report_id": REPORT_ID,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "doc_id": doc_id or ann.get("doc_id", "unknown"),
        "annotation": ann,
        "summary": summary or (
            f"共审查条款 {ann.get('clause_count', 0)} 条，"
            f"其中 {ann.get('risky_clause_count', 0)} 条触发风险规则；"
            f"整体风险等级 {SEVERITY_LABEL.get(ann.get('overall_risk_level'), '未判定')}。"
            + ("（解析置信度偏低，建议人工复核解析结果）" if ann.get("low_confidence") else "")),
        "trace_id": trace_id,
        "audit_trail": audit_trail or [],
    }
    md = _render_markdown(ctx)

    want = {f.strip().lower() for f in (formats or "md").split(",") if f.strip()}
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    written, warnings = [], []
    if "md" in want:
        p = REPORTS_DIR / f"{REPORT_ID}.md"
        p.write_text(md, encoding="utf-8")
        written.append(str(p))
    if "pdf" in want:
        pdf_path = REPORTS_DIR / f"{REPORT_ID}.pdf"
        ok, err = _render_pdf(md, pdf_path)
        if ok:
            written.append(str(pdf_path))
        else:
            warnings.append(f"PDF 生成失败，已保留 Markdown：{err}")

    return {
        "ok": True,
        "report_id": REPORT_ID,
        "doc_id": ctx["doc_id"],
        "paths": written,
        "formats": [Path(p).suffix.lstrip(".") for p in written],
        "markdown": md,
        "overall_risk_level": ann.get("overall_risk_level"),
        "rule_table_digest": ann.get("rule_table_digest"),
        "warnings": warnings,
    }


# ============================================================ 规则提案

def _propose_rule(reject_reason: str, pattern: str, severity: str, regulation_ref: str = "",
                  suggestion: str = "", contract_id: str = "", proposed_by: str = "",
                  evidence: str = "", run_id: str = "", trace_id: str = "") -> dict:
    res = rules_store.propose(reject_reason=reject_reason, pattern=pattern, severity=severity,
                              regulation_ref=regulation_ref, suggestion=suggestion,
                              contract_id=contract_id, proposed_by=proposed_by, evidence=evidence)
    if res.get("ok") and run_id:
        queue_store.record_audit(run_id, "rule_proposal_created", actor=proposed_by or "unknown",
                                 detail={"proposal_id": res["proposal_id"], "kind": res["kind"],
                                         "reason": reject_reason[:200]}, trace_id=trace_id)
        # 提案审批也进统一待办队列（与合同审查共用一套机制）
        queue_store.create_todo(
            run_id=run_id, node_name="rule_proposal_review", role_required="supervisor",
            title=f"规则提案待审：{res['proposal_id']}（{res['kind']}）",
            summary=f"驳回原因：{reject_reason[:120]}；建议模式：{pattern}",
            payload={"proposal_id": res["proposal_id"], "conflict_with": res.get("conflict_with", [])},
            trace_id=trace_id)
    return res


def _list_rule_proposals(status: str = "") -> dict:
    items = rules_store.list_proposals()
    if status:
        items = [p for p in items if p.get("status") == status]
    return {"ok": True, "count": len(items), "proposals": items}


def _approve_rule_proposal(proposal_id: str, approved_by: str, run_id: str = "",
                           trace_id: str = "") -> dict:
    res = rules_store.approve(proposal_id, approved_by)
    if res.get("ok") and run_id:
        queue_store.record_audit(run_id, "rule_proposal_approved", actor=approved_by,
                                 role="supervisor",
                                 detail={"proposal_id": proposal_id, "rule_id": res.get("rule_id"),
                                         "version": res.get("version")}, trace_id=trace_id)
    return res


def _reject_rule_proposal(proposal_id: str, rejected_by: str, reason: str = "",
                          run_id: str = "", trace_id: str = "") -> dict:
    res = rules_store.reject(proposal_id, rejected_by, reason)
    if res.get("ok") and run_id:
        queue_store.record_audit(run_id, "rule_proposal_rejected", actor=rejected_by,
                                 role="supervisor",
                                 detail={"proposal_id": proposal_id, "reason": reason[:200]},
                                 trace_id=trace_id)
    return res


# ============================================================ 流程

def _record_decision(run_id: str, node_name: str, decision: str, actor: str,
                     role: str = "", note: str = "", trace_id: str = "",
                     todo_id: str = "") -> dict:
    detail = {"node": node_name, "decision": decision, "note": note[:300]}
    audit = queue_store.record_audit(run_id, "human_decision", actor=actor, role=role,
                                     detail=detail, trace_id=trace_id, todo_id=todo_id)
    completed = None
    if todo_id:
        completed = queue_store.complete_todo(todo_id, actor, decision, note)
    return {"ok": True, "audit": audit, "todo_result": completed,
            "run_id": run_id, "node": node_name, "decision": decision, "actor": actor}


def _list_pending(role: str = "", status: str = "open", run_id: str = "", limit: int = 50) -> dict:
    todos = queue_store.list_todos(role=role, status=status, run_id=run_id, limit=limit)
    return {"ok": True, "count": len(todos), "role": role or "all", "status": status, "todos": todos}


def _list_audit(run_id: str = "", limit: int = 100) -> dict:
    return {"ok": True, "events": queue_store.list_audit(run_id=run_id, limit=limit)}


def _metrics() -> dict:
    m = queue_store.metrics()
    m["ok"] = True
    m["rule_table_digest"] = rules_store.digest()
    m["regulation_count"] = regulations_store.count()
    return m


def _service_info() -> dict:
    return {
        "ok": True,
        "server": "compliance-officer",
        "stage": "P2",
        "tools": sorted(TOOLS.keys()),
        "rule_table": {"count": len(rules_store.load_all()),
                       "active": len(rules_store.active()),
                       "digest": rules_store.digest()},
        "regulations": {"count": regulations_store.count(),
                        "source": regulations_store.load()["source"],
                        "domains": regulations_store.domains()},
        "ocr_tier": contract_parser.OCR_TIER,
        "capability_channels": {
            "mcp_tools": "确定性执行通道（D0-G4 已验证）",
            "skills": "岗位 SOP 说明书通道（框架技能脚本通道在本机不可用，见 D0 报告 §1.6）",
        },
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================ 注册

TOOLS: dict[str, dict] = {
    "parse_contract": {
        "fn": _parse_contract,
        "description": "解析合同文档（PDF/Word/纯文本），返回结构化条款列表、Markdown 表格、"
                       "解析置信度与告警。扫描件在无 OCR 档位下返回低置信度并提示转人工。",
        "inputSchema": _schema({"path": _s("文件路径（与 text 二选一）"),
                                "text": _s("合同纯文本（与 path 二选一）"),
                                "doc_id": _s("文档标识（可选）")}, []),
    },
    "extract_tables": {
        "fn": _extract_tables,
        "description": "抽取合同中的表格并转为 Markdown（保留列头与行列结构）。未抽到表格时显式说明原因，"
                       "不会静默返回空。",
        "inputSchema": _schema({"path": _s("文件路径"), "text": _s("纯文本"),
                                "doc_id": _s("文档标识（可选）")}, []),
    },
    "search_regulations": {
        "fn": _search_regulations,
        "description": "检索法规与内控条款，返回带条文编号与出处的命中列表；空检索时 empty_result=true，"
                       "调用方不得据此编造法条。",
        "inputSchema": _schema({"query": _s("待检索的条款或关键词"),
                                "top_k": _i("返回条数，默认 5"),
                                "domain": _s("领域过滤（可选）")}, ["query"]),
    },
    "annotate_clause": {
        "fn": _annotate_clause,
        "description": "对单条条款做规则风险标注，返回 risk_level、命中规则（含版本）、法条引用与建议。"
                       "无引用不判风险；high/mid 必须人工确认。",
        "inputSchema": _schema({"clause_text": _s("条款原文"),
                                "clause_id": _s("条款编号（可选）")}, ["clause_text"]),
    },
    "annotate_document": {
        "fn": _annotate_document,
        "description": "整篇合同标注：解析 + 逐条判定 + 汇总分级（overall_risk_level）。"
                       "是进入人工审批前的标准入口。",
        "inputSchema": _schema({"path": _s("文件路径"), "text": _s("纯文本"),
                                "doc_id": _s("文档标识（可选）"),
                                "max_clauses": _i("最多标注条款数，默认 200")}, []),
    },
    "generate_report": {
        "fn": _generate_report,
        "description": "生成合规审查报告（Markdown + PDF），含执行摘要、风险条款定位、修改建议、"
                       "法条依据与审核链。",
        "inputSchema": _schema({"doc_id": _s("文档标识"),
                                "annotation": {"type": "object", "description": "annotate_document 的输出"},
                                "summary": _s("自定义执行摘要（可选）"),
                                "trace_id": _s("全链路追踪 ID（可选）"),
                                "audit_trail": {"type": "array", "items": {"type": "string"},
                                                "description": "审核链文本（可选）"},
                                "formats": _s("输出格式，默认 md,pdf")}, ["annotation"]),
    },
    "propose_rule": {
        "fn": _propose_rule,
        "description": "由人工驳回原因生成规则提案（不直接改规则库）。内置模式精确匹配 + difflib 相似度去重"
                       "（>0.85 转修订提案）与同模式异等级的冲突提示（交主管裁决）。",
        "inputSchema": _schema({"reject_reason": _s("人工驳回原因"),
                                "pattern": _s("拟新增/修订的正则模式"),
                                "severity": _s("low|mid|high"),
                                "regulation_ref": _s("关联法条编号"),
                                "suggestion": _s("建议话术"),
                                "contract_id": _s("来源合同编号"),
                                "proposed_by": _s("提案人"),
                                "evidence": _s("证据片段"),
                                "run_id": _s("关联工作流 run（可选）"),
                                "trace_id": _s("追踪 ID（可选）")},
                               ["reject_reason", "pattern", "severity"]),
    },
    "list_rule_proposals": {
        "fn": _list_rule_proposals,
        "description": "列出规则提案（可按 status 过滤：pending_supervisor/approved/rejected）。",
        "inputSchema": _schema({"status": _s("状态过滤（可选）")}, []),
    },
    "approve_rule_proposal": {
        "fn": _approve_rule_proposal,
        "description": "主管批准提案 -> 版本化写入规则表（version+1、写入审计字段）。重复批准会被拒绝。",
        "inputSchema": _schema({"proposal_id": _s("提案编号"), "approved_by": _s("批准人"),
                                "run_id": _s("关联 run（可选）"), "trace_id": _s("追踪 ID（可选）")},
                               ["proposal_id", "approved_by"]),
    },
    "reject_rule_proposal": {
        "fn": _reject_rule_proposal,
        "description": "主管驳回规则提案（不改动规则表）。",
        "inputSchema": _schema({"proposal_id": _s("提案编号"), "rejected_by": _s("驳回人"),
                                "reason": _s("驳回原因"), "run_id": _s("关联 run（可选）"),
                                "trace_id": _s("追踪 ID（可选）")}, ["proposal_id", "rejected_by"]),
    },
    "record_decision": {
        "fn": _record_decision,
        "description": "记录人工决策（append-only 审计），并可选地把对应待办标记为完成。"
                       "人工节点恢复时应调用本工具留下操作者证据。",
        "inputSchema": _schema({"run_id": _s("工作流 run ID"), "node_name": _s("人工节点名"),
                                "decision": _s("confirm|modify|reject|approve"),
                                "actor": _s("操作者"), "role": _s("角色（可选）"),
                                "note": _s("处理意见（可选）"), "trace_id": _s("追踪 ID（可选）"),
                                "todo_id": _s("待办编号（可选）")},
                               ["run_id", "node_name", "decision", "actor"]),
    },
    "list_pending": {
        "fn": _list_pending,
        "description": "列出待办事项（按角色/状态过滤）。替代框架『靠用户在聊天里带 run_id』的做法。",
        "inputSchema": _schema({"role": _s("specialist|supervisor|admin（可选）"),
                                "status": _s("open|claimed|done|expired|all，默认 open"),
                                "run_id": _s("按 run 过滤（可选）"),
                                "limit": _i("条数上限，默认 50")}, []),
    },
    "list_audit": {
        "fn": _list_audit,
        "description": "查询审计事件（证据链）。可按 run_id 过滤。",
        "inputSchema": _schema({"run_id": _s("run ID（可选）"), "limit": _i("条数上限，默认 100")}, []),
    },
    "metrics": {
        "fn": _metrics,
        "description": "指标快照：待办总量/办结/在办、人审率、决策分布、驳回原因样本、规则与法规版本指纹。",
        "inputSchema": _schema({}, []),
    },
    "service_info": {
        "fn": _service_info,
        "description": "岗位能力服务自描述：工具清单、规则表指纹、法规库规模与领域、OCR 档位。",
        "inputSchema": _schema({}, []),
    },
}


def list_tools_payload() -> list:
    return [{"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
            for name, spec in TOOLS.items()]
