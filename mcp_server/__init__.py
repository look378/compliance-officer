#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
合规官「小合」· 岗位能力服务包

模块职责：
    compliance_mcp_server.py  MCP Streamable HTTP 传输层（协议）
    tools_registry.py         工具契约与实现（岗位能力入口）
    contract_parser.py        合同解析（PDF/Word/文本 + 表格抽取 + OCR 档位）
    regulations_store.py      法规与内控条款库（CSV 主数据源 + 检索）
    rules_store.py            风险规则表读写与提案流程（唯一真相源）
    queue_store.py            统一待办队列与证据链（含原子领取）
    runtime_store.py          工作流 run 的原子抢占与待办扫描（与框架状态机解耦）

导入本包不会启动服务、也不会连接数据库；只有调用具体函数时才惰性访问存储。
"""

__all__ = [
    "compliance_mcp_server",
    "tools_registry",
    "contract_parser",
    "regulations_store",
    "rules_store",
    "queue_store",
    "runtime_store",
]
