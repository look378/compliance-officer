#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据库路径解析（本项目自有代码）

【审查修正 P1】统一"唯一 sqlite 文件"的落点。

背景（两条实测事实）：
    1. 框架 `dao/db.py` 用 `Path.cwd() / ".lemonclaw" / "lemonclaw.db"` 作为库路径，
       因此**谁决定 cwd，谁决定库文件**。
    2. 能力服务（`mcp_server/compliance_mcp_server.py`）原先只把项目根加入 sys.path、
       不改 cwd 也不注入框架目录，于是它在 `<项目根>/.lemonclaw/lemonclaw.db` 上建库，
       而网关与全部自检脚本用的是 `.runtime/workspace/.lemonclaw/lemonclaw.db`
       —— 同一套待办/审计数据被写进了两个文件，`from dao.db import get_db_path`
       在服务进程里还会直接 ModuleNotFoundError。

解析顺序（任一入口都收敛到同一个文件）：
    1) 环境变量 LEMONCLAW_DB_PATH（显式覆盖，交付/容器用）
    2) 框架 `dao.db.get_db_path()`（前提：框架目录在 sys.path 且 cwd 正确）
    3) 项目内固定路径 `.runtime/workspace/.lemonclaw/lemonclaw.db`（绝对路径，不依赖 cwd）
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 与 gateway 的 chdir 目标一致：框架工作区
WORKSPACE = PROJECT_ROOT / ".runtime" / "workspace"
# 框架里的软链接目录（Windows 上是 junction）：workspace/.lemonclaw -> lemonclaw-main/.lemonclaw
FRAMEWORK_DIR = Path(os.environ.get("LEMONCLAW_HOME",
                                    str(PROJECT_ROOT.parent / "lemonclaw-main")))


def resolve_db_path() -> Path:
    """返回唯一的 sqlite 库文件路径（保证目录存在）。

    【重要修正】框架的 `get_db_path()` 会把"**首次调用时的 cwd**"算出的路径
    缓存进模块级变量（lemonclaw-main/dao/db.py:34-55）。因此"跟随框架"是**不安全**的：
    同一个项目里，谁先调用、从哪个目录调用，就会决定库落在哪里 —— 实测出现过
    仓库根 `.lemonclaw/lemonclaw.db` 与工作区 `.runtime/workspace/.lemonclaw/lemonclaw.db`
    两份库并存，这直接违反框架"全局只能有一个 sqlite 文件"的约束。

    裁决顺序：
      1) `LEMONCLAW_DB_PATH` 显式覆盖（**必须最先判断**：测试与自检用它把库隔离到临时目录，
         若先走框架分支就会把测试数据写进生产库 —— 实测踩过）
      2) 框架已解析出的路径，但仅当它等于或位于工作区内（说明 cwd 正确）
      3) 工作区绝对路径 `.runtime/workspace/.lemonclaw/lemonclaw.db`
    """
    override = (os.environ.get("LEMONCLAW_DB_PATH") or "").strip()
    if override:
        p = Path(override)
    else:
        canonical = (WORKSPACE / ".lemonclaw" / "lemonclaw.db").resolve()
        p = None
        try:                                   # 框架已算好且落在工作区内 → 采用
            from dao.db import get_db_path
            candidate = Path(get_db_path()).resolve()
            ws = WORKSPACE.resolve()
            if candidate == canonical or ws in candidate.parents:
                p = candidate
        except Exception:
            p = None
        if p is None:
            p = canonical
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
