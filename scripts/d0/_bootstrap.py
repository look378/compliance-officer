#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D0 公共引导：把本机运行时平台（.runtime/workspace + .runtime/.lemonclaw）准备好，
并让外部运行时框架以「只读源码 + PYTHONPATH」方式被引用。

关键点（对应方案设计说明 §2 零改动边界）：
    - 只向 .lemonclaw/ 写配置与技能包，绝不写入框架源码目录
    - 进程工作目录切到 .runtime/workspace，使框架的相对路径数据库与工作区一致
    - 框架源码目录只加入 sys.path
"""

import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME = PROJECT_ROOT / ".runtime"
WORKSPACE = RUNTIME / "workspace"
# 运行时框架（LemonClaw）的位置：优先环境变量 LEMONCLAW_HOME，
# 默认取**本项目目录的同级兄弟目录** lemonclaw-main —— 这样换台机器/换路径都不用改代码。
# 约定：<workspace>/lemonclaw-main 与 <workspace>/compliance_officer 并排（见 README「从零开始」）。
FRAMEWORK = Path(os.environ.get("LEMONCLAW_HOME")
                 or (PROJECT_ROOT.parent / "lemonclaw-main"))


def port_pid(port: int) -> int | None:
    """返回监听该端口的进程 PID（用 netstat 解析，不依赖 NetTCPIP 模块/WMI）。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=20, errors="replace").stdout
    except Exception:
        return None
    # 【审查修正 P2】原实现用 Get-NetTCPConnection（PowerShell 模块），
    # 模块缺失时命令发现阶段就报错；这里改成解析 netstat 输出，任何 Windows 都能用。
    pat = re.compile(rf":{port}\s+\S+\s+LISTENING\s+(\d+)")
    for line in out.splitlines():
        m = pat.search(line)
        if m:
            return int(m.group(1))
    return None


def stop_port(port: int, why: str = "") -> dict:
    """停掉监听该端口的进程（自检脚本用）。

    目的：避免自检复用**上一次运行留下的旧进程**（旧代码/旧 AUTH_MODE），
    那种"静默复用"会让门禁结果失真（实测踩过：jwt 模式下的脚本复用了 demo 模式网关）。
    """
    pid = port_pid(port)
    if not pid:
        return {"port": port, "stopped": False, "reason": "端口空闲"}
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       text=True, timeout=20, errors="replace")
        return {"port": port, "stopped": True, "pid": pid, "why": why}
    except Exception as exc:
        return {"port": port, "stopped": False, "pid": pid,
                "reason": f"{type(exc).__name__}: {exc}"}


def bootstrap() -> None:
    """准备环境并切换工作目录（幂等）。

    【必须在使用任何框架模块之前调用】
    框架的配置类在「模块导入时」就把 .env 的值固化成了类属性默认值
    （lemonclaw-main/config/config.py:53-118 使用 os.environ.get 作为类属性默认值），
    因此必须先 load_dotenv + chdir，再导入框架，否则配置一律为空。
    """
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (RUNTIME / ".lemonclaw").mkdir(parents=True, exist_ok=True)
    (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.chdir(WORKSPACE)

    fw = str(FRAMEWORK)
    if fw not in sys.path:
        sys.path.insert(0, fw)
    # 项目根也入 path，便于脚本引用本项目自有模块（如 mcp_server.runtime_store）
    pr = str(PROJECT_ROOT)
    if pr not in sys.path:
        sys.path.insert(0, pr)

    # 先加载 .env 到进程环境，再让框架模块在导入时读到（顺序不可颠倒）
    from dotenv import load_dotenv
    env_path = FRAMEWORK / ".lemonclaw" / ".env"
    if not env_path.exists():
        raise FileNotFoundError(
            f"未找到运行时配置 {env_path}；请先执行 scripts/deploy-runtime.ps1"
        )
    load_dotenv(env_path, override=True)


def paths() -> dict:
    return {
        "project_root": str(PROJECT_ROOT),
        "runtime": str(RUNTIME),
        "workspace": str(WORKSPACE),
        "lemonclaw_dir": str(RUNTIME / ".lemonclaw"),
        "framework": str(FRAMEWORK),
        "db": str(WORKSPACE / ".lemonclaw" / "lemonclaw.db"),
    }
