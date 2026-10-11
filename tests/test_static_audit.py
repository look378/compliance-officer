#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
静态审查工具自身的回归测试

为什么要有这个文件：
    `scripts/tools/static_audit.py` 是"机械审查"的执行者 —— 它如果失效，
    审查报告会**全绿但毫无意义**。第二次审查就真实发生过这种事：
    `tests/test_sso.py` 漏了 `import os`，整个 SSO 单测模块在 pytest 收集阶段
    NameError，而当时的审查工具既查不出"未定义名称"、也不报"未使用导入"。

    因此这里对**检查器本身**做回归：既验证它能抓到真问题，也验证它不误报
    （漏报会让假绿通过，误报会让人不再看报告 —— 两个方向都要守）。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "tools"))

from static_audit import DEF_EXEMPT_PATTERNS, undefined_names  # noqa: E402


def test_detects_name_used_at_module_level_without_import():
    """本文件为修复的真实缺陷：模块级用了 os，却没 import os。"""
    issues = undefined_names('import json\nX = os.environ.get("A")\n', "t.py")
    assert len(issues) == 1
    assert issues[0]["kind"] == "undefined_name"
    assert issues[0]["line"] == 2
    assert "os" in issues[0]["detail"]


def test_detects_name_used_inside_function_without_import():
    issues = undefined_names("def f():\n    return os.sep\n", "t.py")
    assert len(issues) == 1 and "os" in issues[0]["detail"]


@pytest.mark.parametrize("src", [
    'import os\nX = os.environ.get("A")\n',                  # 正常导入
    "X = len([1, 2])\n",                                     # 内置名
    "def outer():\n    v = 1\n    def inner():\n        return v\n    return inner\n",  # 闭包
    "def a():\n    return b()\ndef b():\n    return 1\n",    # 先用后定义
    "X = __file__\n",                                        # 解释器注入名
    "try:\n    import x\nexcept Exception:\n    x = None\nY = x\n",   # 兜底赋值
])
def test_no_false_positives(src):
    assert undefined_names(src, "t.py") == []


def test_wildcard_import_skips_file():
    """`from x import *` 无法静态判定，必须整文件跳过（宁可漏报也不刷屏）。"""
    assert undefined_names("from foo import *\nX = bar\n", "t.py") == []


def test_brace_syntax_error_does_not_raise():
    """语法错误由语法检查负责，这里不能抛异常把整轮审查带崩。"""
    assert undefined_names("def f(:\n", "t.py") == []


def test_pytest_test_classes_are_exempt_from_dead_code():
    """pytest 按 `Test*` 命名约定收集，代码里没有调用点 —— 不能报成死代码。"""
    import re
    assert any(re.match(pat, "TestSomething") for pat in DEF_EXEMPT_PATTERNS)
