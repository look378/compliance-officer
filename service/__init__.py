#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
服务层包：合规审查编排与待办消费

模块：
    review_orchestrator.py   两段式编排器 + 待办 + 原子认领 + 启动补偿（P4 核心）

设计边界：
    - 只调用运行时框架的公开 API（WorkflowManager / WorkflowRunner），不改其源码
    - 不启动 HTTP 服务；网关层（gateway/）负责对外接口与 SSE
"""

__all__ = ["review_orchestrator"]
