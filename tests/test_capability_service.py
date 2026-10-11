#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P2 单元测试：岗位能力服务核心模块

运行：
    .runtime\\venv\\Scripts\\python.exe -m pytest tests -q

测试纪律：
    - 全部使用临时目录 + monkeypatch 隔离，不写真实 rules/ 与数据库
    - 不只测"happy path"：坏正则、缺引用、重复批准、并发领取、空检索等失败路径必须覆盖
    - 与自检脚本（scripts/d0/p2_tools_e2e.py）互补：自检走端到端，本文件走细粒度分支
"""

import json
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server import regulations_store, rules_store  # noqa: E402


# ---------------------------------------------------------------- 夹具
# 不使用 pytest 内置 tmp_path：本机受限环境下 pytest 对临时目录做 ACL 操作会被拒绝
# （WinError 5）。这里在项目内自建目录，语义等价且在任何环境下都能跑。
# 同理，pytest.ini 关闭了 cacheprovider 插件（关闭后 cache_dir 配置项不再生效）。

@pytest.fixture()
def workdir():
    base = PROJECT_ROOT / ".runtime" / "testtmp"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"t{int(time.time() * 1000)}-{int.from_bytes(__import__('os').urandom(3), 'big')}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================ 法规库与检索

class TestRegulationsStore:
    def test_library_loaded_from_csv(self):
        lib = regulations_store.load(force=True)
        assert lib["source"] == "regulations.csv"
        assert len(lib["items"]) >= 40, "法规库应不少于 40 条"
        assert lib["digest"] and lib["digest"] != "empty"

    def test_no_duplicate_refs(self):
        refs = [i["regulation_ref"] for i in regulations_store.load()["items"]]
        assert len(refs) == len(set(refs)), "法规编号不得重复"

    def test_domains_cover_required_areas(self):
        ds = regulations_store.domains()
        for need in ("合同通则", "劳动用工", "数据合规", "金融销售", "政府采购", "租赁"):
            assert need in ds, f"缺少领域：{need}"

    @pytest.mark.parametrize("query,expect_ref", [
        ("月工作时长不超过 280 小时", "《劳动法》第 41 条"),
        ("保本收益 保证收益", "《人身保险销售行为管理办法》第 22 条"),
        ("最终解释权归甲方", "《民法典》第 498 条"),
        ("收集个人信息无需同意", "《个人信息保护法》第 13 条"),
    ])
    def test_search_hits_expected_regulation(self, query, expect_ref):
        res = regulations_store.search(query, top_k=5)
        assert not res["empty_result"], f"应命中：{query}"
        assert expect_ref in [h["regulation_ref"] for h in res["hits"]]

    def test_unrelated_query_returns_empty_and_declares_it(self):
        res = regulations_store.search("火星移民审批流程 zzqqxx", top_k=3)
        assert res["empty_result"] is True
        assert res["degraded_to_rules_only"] is True
        assert res["hits"] == []

    def test_domain_filter(self):
        res = regulations_store.search("个人信息 单独同意 告知", top_k=5, domain="数据合规")
        assert res["hits"], "数据合规领域应能命中"
        assert all(h["domain"] == "数据合规" for h in res["hits"])

    def test_hits_carry_citation_fields(self):
        res = regulations_store.search("违约金 过高", top_k=3)
        # 【审查修正 P2】原断言没有先确认 hits 非空：检索退化为空结果时
        # `for h in []` 恒真、测试依然通过（空循环假绿）。
        assert res["hits"], "『违约金 过高』应至少命中一条法规"
        for h in res["hits"]:
            assert h["regulation_ref"] and h["source"] and h["text"]
            assert h["score"] > 0


# ============================================================ 规则表

@pytest.fixture()
def tmp_rules(workdir, monkeypatch):
    """把规则表与提案目录切到临时目录，避免污染唯一真相源。"""
    rules_file = workdir / "risk_rules.yaml"
    rules_file.write_text(
        "- id: RR-001\n"
        "  version: 1\n"
        "  status: active\n"
        "  pattern: \"保本.{0,6}收益\"\n"
        "  severity: high\n"
        "  priority: 10\n"
        "  regulation_ref: \"《示例法》第 1 条\"\n"
        "  suggestion: 删除保本表述\n"
        "  source: seed\n"
        "  approved_by: seed\n"
        "  approved_at: 2026-10-09\n"
        "  changelog: 测试用\n"
        "- id: RR-002\n"
        "  version: 1\n"
        "  status: draft\n"
        "  pattern: \"最终解释权\"\n"
        "  severity: mid\n"
        "  priority: 5\n"
        "  regulation_ref: \"《示例法》第 2 条\"\n"
        "  suggestion: 删除最终解释权\n"
        "  source: seed\n"
        "  approved_by: seed\n"
        "  approved_at: 2026-10-09\n"
        "  changelog: 测试用\n",
        encoding="utf-8")
    monkeypatch.setattr(rules_store, "RULES_PATH", rules_file)
    monkeypatch.setattr(rules_store, "PROPOSALS_DIR", workdir / "proposals")
    return rules_file


class TestRulesStore:
    def test_active_excludes_draft(self, tmp_rules):
        assert [r["id"] for r in rules_store.active()] == ["RR-001"]

    def test_annotate_high(self, tmp_rules):
        out = rules_store.annotate("本产品保本，年化收益 5%。")
        assert out["risk_level"] == "high"
        assert out["risk_hits"][0]["rule_id"] == "RR-001"
        assert out["has_citation"] is True

    def test_annotate_low_when_no_hit(self, tmp_rules):
        out = rules_store.annotate("双方按约定交付。")
        assert out["risk_level"] == "low" and out["risk_hits"] == []

    def test_bad_regex_does_not_break_annotation(self, tmp_rules, monkeypatch):
        rules_file = tmp_rules
        rules_file.write_text(
            rules_file.read_text(encoding="utf-8") +
            "- id: RR-003\n  version: 1\n  status: active\n  pattern: \"[unclosed\"\n"
            "  severity: high\n  regulation_ref: \"《示例法》第 3 条\"\n", encoding="utf-8")
        out = rules_store.annotate("任意文本")
        assert out["risk_level"] == "low", "坏正则必须被跳过而不是让标注失败"

    def test_digest_changes_with_rules(self, tmp_rules):
        d1 = rules_store.digest()
        rules = rules_store.load_all()
        rules[0]["version"] = 2
        rules_store.restore(rules)
        assert rules_store.digest() != d1

    def test_norm_pattern_ignores_regex_escaping(self):
        assert rules_store._norm_pattern(r"月.{0,4}\d+") == rules_store._norm_pattern(r"月.{0,4}\d+")
        assert rules_store._norm_pattern(r"a\\.b") == rules_store._norm_pattern("a.b")

    def test_literal_core_strips_metachars(self):
        assert rules_store._literal_core("最终解释权归.{0,4}方所有") == "最终解释权归方所有"


class TestRuleProposal:
    def test_propose_new(self, tmp_rules):
        p = rules_store.propose("原因", "无.{0,4}通知义务", "mid",
                                regulation_ref="《示例法》第 9 条", suggestion="建议话术", proposed_by="tester")
        assert p["ok"] and p["kind"] == "new" and p["proposed_rule_id"] == "RR-003"

    def test_propose_exact_dedup(self, tmp_rules):
        p = rules_store.propose("原因", "保本.{0,6}收益", "high",
                                regulation_ref="《示例法》第 1 条", suggestion="建议话术")
        assert p["kind"] == "revise" and p["target_rule_id"] == "RR-001"
        assert p["dedup"]["layer"] == "exact"
        assert p["version"] == 2

    def test_propose_containment_dedup(self, tmp_rules):
        """核心串被完整包含 -> 判为同一规则，转修订（避免同义规则重复入库）。"""
        p = rules_store.propose("原因", "保本收益承诺", "high",
                                regulation_ref="《示例法》第 1 条", suggestion="建议话术")
        assert p["kind"] == "revise" and p["dedup"]["layer"] == "containment"
        assert p["target_rule_id"] == "RR-001"

    def test_shared_prefix_is_not_containment(self, tmp_rules):
        """仅共享前缀（覆盖率不足）不得判为同一规则——实测校准过的重要边界。"""
        p = rules_store.propose("原因", "保本承诺或者保本说明", "high",
                                regulation_ref="《示例法》第 1 条", suggestion="建议话术")
        assert p["dedup"]["layer"] != "containment", "共享前缀被误判为同一规则会造成漏报"

    def test_propose_conflict_requires_manual(self, tmp_rules):
        p = rules_store.propose("等级分歧", "保本.{0,6}收益", "mid",
                                regulation_ref="《示例法》第 1 条", suggestion="建议话术")
        assert p["kind"] == "conflict"
        assert p["conflict_requires_manual"] is True
        assert p["conflict_with"][0]["rule_id"] == "RR-001"

    def test_propose_rejects_bad_input(self, tmp_rules):
        assert rules_store.propose("", "x", "high", regulation_ref="r", suggestion="s")["ok"] is False
        assert rules_store.propose("r", "[bad", "high", regulation_ref="r", suggestion="s")["ok"] is False
        assert rules_store.propose("r", "ok", "super", regulation_ref="r", suggestion="s")["ok"] is False

    def test_proposal_persisted_as_file(self, tmp_rules):
        p = rules_store.propose("原因", "新模式", "low", regulation_ref="《示例法》第 9 条", suggestion="建议话术")
        f = rules_store.PROPOSALS_DIR / f"{p['proposal_id']}.json"
        assert f.exists()
        assert json.loads(f.read_text(encoding="utf-8"))["kind"] == p["kind"]

    def test_approve_writes_versioned_rule(self, tmp_rules):
        p = rules_store.propose("原因", "新模式", "mid",
                                regulation_ref="《示例法》第 9 条", suggestion="建议话术", proposed_by="tester")
        res = rules_store.approve(p["proposal_id"], "supervisor")
        assert res["ok"] and res["version"] == 1
        rule = rules_store.get(res["rule_id"])
        assert rule["status"] == "active" and rule["source"] == "proposal"
        assert rule["approved_by"] == "supervisor" and rule["regulation_ref"] == "《示例法》第 9 条"

    def test_approve_requires_citation(self, tmp_rules):
        p = rules_store.propose("原因", "新模式", "mid", regulation_ref="", suggestion="建议话术")
        res = rules_store.approve(p["proposal_id"], "supervisor")
        assert res["ok"] is False
        assert any("regulation_ref" in e for e in res["errors"])

    def test_approve_rejects_conflict_proposal(self, tmp_rules):
        p = rules_store.propose("等级分歧", "保本.{0,6}收益", "mid",
                                regulation_ref="《示例法》第 1 条", suggestion="建议话术")
        res = rules_store.approve(p["proposal_id"], "supervisor")
        assert res["ok"] is False
        assert any("冲突" in e for e in res["errors"])

    def test_approve_is_idempotent_guarded(self, tmp_rules):
        p = rules_store.propose("原因", "新模式", "mid", regulation_ref="《示例法》第 9 条", suggestion="建议话术")
        assert rules_store.approve(p["proposal_id"], "sup")["ok"] is True
        again = rules_store.approve(p["proposal_id"], "sup")
        assert again["ok"] is False and "已批准" in again["errors"][0]

    def test_approve_requires_actor(self, tmp_rules):
        p = rules_store.propose("原因", "新模式", "mid", regulation_ref="《示例法》第 9 条", suggestion="建议话术")
        assert rules_store.approve(p["proposal_id"], "")["ok"] is False

    def test_approve_missing_proposal(self, tmp_rules):
        assert rules_store.approve("RP-notexist", "sup")["ok"] is False

    def test_revise_bumps_version(self, tmp_rules):
        p1 = rules_store.propose("原因", "保本.{0,6}收益", "high",
                                 regulation_ref="《示例法》第 1 条", suggestion="建议话术")
        # 精确去重 -> 修订
        assert p1["kind"] == "revise" and p1["version"] == 2
        # 先解决冲突：把现有规则改成同等级，再批准
        rules = rules_store.load_all()
        for r in rules:
            if r["id"] == "RR-001":
                r["severity"] = "high"
        rules_store.restore(rules)
        res = rules_store.approve(p1["proposal_id"], "sup")
        assert res["ok"] and res["version"] == 2
        assert rules_store.get("RR-001")["version"] == 2


# ============================================================ 合同解析

class TestContractParser:
    def test_text_parsing_splits_clauses(self):
        from mcp_server import contract_parser
        text = "第一条 甲乙双方约定如下。\n第二条 乙方月工作时长不超过 280 小时。"
        out = contract_parser.parse(text, as_text=True)
        assert out["source_format"] == "text"
        assert out["clause_count"] == 2
        assert out["clauses"][1]["heading"] == "第二条"
        assert out["confidence"] >= 0.6

    def test_missing_file_reported(self):
        from mcp_server import contract_parser
        out = contract_parser.parse("/no/such/file.pdf")
        assert out["confidence"] == 0.0
        assert any("不存在" in w for w in out["warnings"])

    def test_unsupported_suffix_reported(self, workdir):
        from mcp_server import contract_parser
        p = workdir / "a.xlsx"
        p.write_bytes(b"x")
        out = contract_parser.parse(str(p))
        assert any("不支持" in w for w in out["warnings"])

    def test_docx_with_table(self, workdir):
        import docx
        from mcp_server import contract_parser
        d = docx.Document()
        d.add_paragraph("第一条 双方约定付款计划见下表。")
        d.add_paragraph("第二条 付款方式为银行转账。")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text = "期次"
        t.cell(0, 1).text = "比例"
        t.cell(1, 0).text = "首期"
        t.cell(1, 1).text = "30%"
        p = workdir / "c.docx"
        d.save(str(p))
        out = contract_parser.parse(str(p))
        assert out["source_format"] == "docx"
        assert len(out["tables"]) == 1
        assert "| 期次 | 比例 |" in out["tables"][0]["markdown"]
        # 仅 1 个条款 + 有表格：置信度应达到"可用"区间且高于空文档基线
        assert out["confidence"] >= 0.6
        assert out["clauses"] and out["clauses"][0]["heading"] == "第一条"

    def test_short_doc_flagged_low_confidence(self):
        """无条款编号的短文档必须被判为低置信度（而非静默通过）。"""
        from mcp_server import contract_parser
        out = contract_parser.parse("附件一：付款计划见下表。", as_text=True)
        assert out["confidence"] <= 0.5
        assert any("未识别到标准条款编号" in w for w in out["warnings"])

    def test_table_to_markdown_pads_ragged_rows(self):
        from mcp_server import contract_parser
        md = contract_parser._table_to_markdown([["a", "b"], ["c"]])
        assert md.count("|") == 9  # 表头+分隔+数据 三行，每行 3 个竖线
        assert "| c |  |" in md


# ============================================================ 工具契约

class TestToolsRegistry:
    def test_tool_count_and_schema_shape(self):
        from mcp_server import tools_registry as T
        assert len(T.TOOLS) >= 15
        for name, spec in T.TOOLS.items():
            assert spec["description"], f"{name} 缺描述"
            sch = spec["inputSchema"]
            assert sch["type"] == "object" and "properties" in sch and "required" in sch
            for req in sch["required"]:
                assert req in sch["properties"], f"{name} 的必填字段 {req} 未在 properties 中声明"

    def test_tool_names_are_snake_case(self):
        from mcp_server import tools_registry as T
        for name in T.TOOLS:
            assert re.match(r"^[a-z][a-z0-9_]*$", name), f"工具名不规范：{name}"

    def test_generate_report_requires_annotation(self):
        from mcp_server import tools_registry as T
        assert T._generate_report(doc_id="d1", annotation=None)["ok"] is False

    def test_generate_report_markdown_contains_citation(self, workdir, monkeypatch):
        """报告渲染必须给出规则编号依据与免责声明。

        【审查修正 P2】原来直接调 _generate_report，会真的往 <项目>/reports/ 落
        .md/.pdf（已累积 60+ 个文件）；文件头却声称"不写真实目录"。
        现在把 REPORTS_DIR 指到临时目录，测试真正隔离。
        """
        from mcp_server import tools_registry as T
        monkeypatch.setattr(T, "REPORTS_DIR", workdir / "reports", raising=True)
        doc = T._annotate_document(text="第一条 本产品保本，年化收益 5%。")
        rep = T._generate_report(doc_id=doc["doc_id"], annotation=doc)
        assert rep["ok"] and "合同合规审查报告" in rep["markdown"]
        assert "RR-001" in rep["markdown"], "报告必须给出规则编号依据"
        assert "仅供参考" in rep["markdown"], "报告必须含免责声明"
        for p in rep["paths"]:
            assert str(workdir) in str(p), f"报告必须落在隔离目录内：{p}"

    def test_annotate_document_overall_level(self):
        from mcp_server import tools_registry as T
        doc = T._annotate_document(text="第一条 双方按约定交付。\n第二条 最终解释权归甲方。")
        assert doc["overall_risk_level"] == "mid"
        assert doc["risky_clause_count"] == 1

    def test_service_info_lists_capabilities(self):
        from mcp_server import tools_registry as T
        info = T._service_info()
        assert info["ok"] and len(info["tools"]) >= 15
        assert info["regulations"]["count"] >= 40
        assert "mcp_tools" in info["capability_channels"]


# ============================================================ MCP 协议层

class TestMcpProtocol:
    def test_initialize_returns_session_and_protocol(self):
        from mcp_server import compliance_mcp_server as S
        resp, sid = S.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {}}, None)
        assert sid and resp["result"]["protocolVersion"] == S.PROTOCOL_VERSION
        assert resp["result"]["serverInfo"]["name"] == S.SERVER_NAME

    def test_tools_list_payload(self):
        from mcp_server import compliance_mcp_server as S
        resp, sid = S.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)
        assert sid is None
        names = [t["name"] for t in resp["result"]["tools"]]
        assert "parse_contract" in names and "search_regulations" in names
        for t in resp["result"]["tools"]:
            assert t["description"] and t["inputSchema"]["type"] == "object"

    def test_tools_call_returns_json_text(self):
        from mcp_server import compliance_mcp_server as S
        resp, _ = S.handle_rpc({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_regulations", "arguments": {"query": "保本收益"}}}, None)
        assert resp["result"]["isError"] is False
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["ok"] and payload["hits"]

    def test_unknown_tool_is_error_not_crash(self):
        from mcp_server import compliance_mcp_server as S
        resp, _ = S.handle_rpc({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}}}, None)
        assert resp["result"]["isError"] is True

    def test_bad_arguments_reported_as_error(self):
        from mcp_server import compliance_mcp_server as S
        resp, _ = S.handle_rpc({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "annotate_clause", "arguments": {"wrong": 1}}}, None)
        assert resp["result"]["isError"] is True
        assert "参数错误" in resp["result"]["content"][0]["text"]

    def test_unknown_method_returns_jsonrpc_error(self):
        from mcp_server import compliance_mcp_server as S
        resp, _ = S.handle_rpc({"jsonrpc": "2.0", "id": 6, "method": "nope/method"}, None)
        assert resp["error"]["code"] == -32601

    def test_notification_returns_no_response(self):
        from mcp_server import compliance_mcp_server as S
        resp, sid = S.handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
        assert resp is None and sid is None
