#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
代码审查：全量静态扫描（本项目自有，P8 审查工具）

用途：在人工审查之外，用脚本把"机械可判定"的问题一次性扫出来，避免漏检。
检查项：
    1) 语法是否可编译（每文件 py_compile）
    2) 未使用的 import（模块级）
    3) 定义了但全仓未被引用的函数/类（疑似死代码，排除 main/魔法方法/测试用例/入口）
    4) 函数内"赋值后从未读取"的局部变量
    5) 恒真/恒假判断、裸 except、可变默认参数
    6) return/raise 之后的不可达语句
    7) 各存储模块的 DB 路径解析是否一致（避免连到不同的库）
    8) registry 工具名 vs workflows spec 引用的工具名一致性
    9) TODO/占位/调试残留
   10) 每文件行数与函数清单（作为审查覆盖面证据）
   11) **未定义名称**（被读取但本模块既没定义也没导入、又不是内置名）

产出：docs/evidence/p8-static-audit.json
"""

import ast
import builtins
import json
import re
import symtable
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p8-static-audit.json"
SKIP_DIRS = (".runtime", "__pycache__", ".git", "reports", "node_modules", ".pytest_cache")
BUILTINS = set(dir(builtins))
# 由 import 机制/解释器注入、但不是 builtins 的名字
SPECIAL_GLOBALS = {"__file__", "__name__", "__doc__", "__package__", "__builtins__",
                   "__spec__", "__loader__", "__annotations__", "__debug__", "__path__"}

# 允许"定义了但未被本仓引用"的例外（框架约定/测试/入口/路由/回调）
DEF_EXEMPT_PATTERNS = (
    r"^main$", r"^__", r"^test_", r"^_", r"^create_", r"^get_", r"^ensure_",
    r"^list_", r"^record_", r"^verify_", r"^login$", r"^logout$", r"^index$",
    # FastAPI 路由处理函数：由装饰器注册，名称不会出现在调用点
    r"^(health|whoami|samples|rules|metrics|audit_console|auth_events|logs|logs_stream|"
    r"config_view|agent_info|agent_task|agent_history|agent_reset|submit_contract|"
    r"upload_contract|list_runs|pending|run_detail|decide|recover|proposals|"
    r"review_proposal|list_users|create_user|set_user_status|change_password|run_events)$",
    # 框架回调 / 依赖
    r"^(auth|identity|require|require_perm|orch|orch_health|agent_runtime|trace_middleware|"
    r"diagnostics)$",
    # HTTP handler / 中间件约定名
    r"^(do_GET|do_POST|do_DELETE|log_message|gen)$",
    # AST NodeVisitor 回调：由 ast.walk/visit 按 visit_<NodeType> 约定调用
    r"^visit_[A-Z]",
    # pytest 测试类：由收集器按 Test* 命名约定实例化，代码里不会出现调用点
    # 【第二次审查修正】原先把 21 个测试类当"死代码"报出来，噪声淹没了真问题
    r"^Test",
)


def py_files() -> list[Path]:
    out = []
    for p in PROJECT_ROOT.rglob("*.py"):
        if any(s in p.parts for s in SKIP_DIRS):
            continue
        out.append(p)
    return sorted(out)


def undefined_names(source: str, filename: str) -> list[dict]:
    """扫"被读取、但本模块既没定义也没导入、又不是内置"的名字。

    【第二次审查新增】为什么必须单独扫这一项：
        `tests/test_sso.py` 在模块级用了 `os.environ` 却漏了 `import os`。
        ① 语法检查（compile）发现不了 —— 编译期 `os` 只是合法标识符；
        ② "未使用 import"检查也发现不了 —— 它只查"导入了没用"，不查"用了没导入"；
        ③ 后果是整个 SSO 单测模块在 pytest **收集阶段**就 NameError，
          而这套审查当时是全绿的 —— 这正是"机械检查有盲区"的典型。
    判据（保守，宁可漏报）：
        · 顶层作用域里被引用、且不是赋值/导入/def/class 的名字；
        · 子作用域里被判定为**全局解析**（is_global）的名字；
        · 排除 builtins、解释器注入名；
        · 文件里存在 `import *` 时整文件跳过（无法静态判定）。
    """
    issues: list[dict] = []
    if re.search(r"^\s*from\s+[\w.]+\s+import\s+\*", source, re.M):
        return issues                       # 通配导入：无法静态判定，整文件跳过
    try:
        top = symtable.symtable(source, filename, "exec")
    except Exception:
        return issues
    defined: set[str] = set()
    for sym in top.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace() or sym.is_parameter():
            defined.add(sym.get_name())

    bad: set[str] = set()

    def walk(table, is_module: bool):
        for sym in table.get_symbols():
            nm = sym.get_name()
            if not sym.is_referenced() or nm in defined or nm in BUILTINS \
                    or nm in SPECIAL_GLOBALS:
                continue
            if sym.is_assigned() or sym.is_imported() or sym.is_namespace() \
                    or sym.is_parameter() or sym.is_free():
                continue
            # 只报"模块级"或"明确按全局名解析"的引用；闭包变量/局部变量不报
            if is_module or sym.is_global():
                bad.add(nm)
        for child in table.get_children():
            walk(child, False)

    walk(top, True)
    if not bad:
        return issues
    # symtable 的 Symbol 不提供行号，回到 AST 上定位第一次读取位置
    try:
        tree = ast.parse(source)
    except Exception:
        return [{"kind": "undefined_name", "line": 0,
                 "detail": f"名字 {nm} 被读取但未定义/未导入（会 NameError）"}
                for nm in sorted(bad)]
    seen: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in bad:
            key = (node.id, node.lineno)
            if key in seen:
                continue
            seen.add(key)
            issues.append({
                "kind": "undefined_name", "line": node.lineno,
                "detail": f"名字 {node.id} 被读取，但本模块既未定义也未导入（会 NameError）"})
    for nm in sorted(bad):                  # AST 里找不到（极少见）也要报
        if not any(n == nm for n, _ in seen):
            issues.append({"kind": "undefined_name", "line": 0,
                           "detail": f"名字 {nm} 被读取但未定义/未导入（会 NameError）"})
    return issues


class FileFacts(ast.NodeVisitor):
    """收集单个文件的定义与引用。"""

    def __init__(self, path: Path):
        self.path = path
        self.imports: list[tuple[str, str, int]] = []      # (module, name, lineno)
        self.defs: list[tuple[str, int]] = []              # 模块级 def/class
        self.local_defs: list[tuple[str, int]] = []        # 函数内 def
        self.names_used: set[str] = set()
        self._load_counts: Counter = Counter()     # 每个名字被"读取"的次数
        self.assigned: list[tuple[str, int]] = []          # 局部赋值
        self.loaded: set[str] = set()
        self.issues: list[dict] = []
        self.tool_names: list[tuple[str, int]] = []        # TOOLS 字典的键
        self.funcs: list[str] = []
        self.import_failed = False
        # 能力探测导入的行号（try/except 内，见 visit_Try）
        self.optional_import_lines: set[int] = set()

    # ---------- 收集 ----------
    def visit_Import(self, node: ast.Import):
        for a in node.names:
            self.imports.append((a.name, a.asname or a.name.split(".")[0], node.lineno))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for a in node.names:
            self.imports.append((node.module or "", a.asname or a.name, node.lineno))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.defs.append((node.name, node.lineno))
        self.funcs.append(node.name)
        self._check_func(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef):
        # 只把类本身记为模块级定义；类方法由框架/调用方按属性访问，容易误报为死代码
        self.defs.append((node.name, node.lineno))
        self.generic_visit(node)

    def visit_Module(self, node: ast.Module):
        # 记录"带装饰器的定义"行号：FastAPI 路由、@tool、@property 等由框架调用，不算死代码
        self.decorated_lines = set()
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.decorator_list:
                self.decorated_lines.add(n.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            self.names_used.add(node.id)
            self.loaded.add(node.id)
            self._load_counts[node.id] += 1
        else:
            self.names_used.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        self.names_used.add(node.attr)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    self.assigned.append((n.id, node.lineno))
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type is None:
            self.issues.append({"kind": "bare_except", "line": node.lineno,
                                "detail": "裸 except（会吞掉 KeyboardInterrupt/SystemExit）"})
        self.generic_visit(node)

    @staticmethod
    def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
        """except 是否在兜底"模块不存在"（能力探测导入的典型形态）。"""
        t = handler.type
        if t is None:
            return False                    # 裸 except 另有专门检查，这里不豁免
        names = []
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.append(n.id)
            elif isinstance(n, ast.Attribute):
                names.append(n.attr)
        return any(x in ("ImportError", "ModuleNotFoundError") for x in names)

    def visit_Try(self, node: ast.Try):
        """标记"**真正的**能力探测导入"。

        【第二次审查修正·P1】原实现是 `if node.handlers and ln <= max(body_lines)`：
        `ln <= max(body_lines)` 对 body_lines 里的任何行都恒真，于是**任何**
        try/except 里的 import 都被豁免（实测全仓 40 处被自动放过），
        "未使用 import"这项证据因此失真。

        现在只豁免两种可解释的情形：
          ① except 明确捕获 ImportError / ModuleNotFoundError（探测依赖是否存在）；
          ② except 分支里**确实引用了该导入名**（例如 `except Exception: pass` 之前会用到它）。
        其余情况照常按未使用导入报告。
        """
        if not node.handlers:
            self.generic_visit(node)
            return
        import_names: dict[int, set[str]] = {}
        for stmt in node.body:
            for n in ast.walk(stmt):
                if isinstance(n, ast.Import):
                    import_names.setdefault(n.lineno, set()).update(
                        (a.asname or a.name.split(".")[0]) for a in n.names)
                elif isinstance(n, ast.ImportFrom):
                    import_names.setdefault(n.lineno, set()).update(
                        (a.asname or a.name) for a in n.names)
        handler_names: set[str] = set()
        for h in node.handlers:
            for n in ast.walk(h):
                if isinstance(n, ast.Name):
                    handler_names.add(n.id)
                elif isinstance(n, ast.Attribute):
                    handler_names.add(n.attr)
        catches_import_error = any(self._handler_catches_import_error(h) for h in node.handlers)
        for ln, names in import_names.items():
            if catches_import_error or (names & handler_names):
                self.optional_import_lines.add(ln)
        self.generic_visit(node)

    def visit_If(self, node: ast.If):
        # 恒真/恒假：if True / if False / if 1: 等
        try:
            val = ast.literal_eval(node.test)
            if isinstance(val, bool) and node.orelse:
                self.issues.append({"kind": "constant_condition", "line": node.lineno,
                                    "detail": f"字面量条件 if {val} 且带 else（存在死分支）"})
        except Exception:
            pass
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return):
        # 函数体后续语句不可达：由 _check_func 处理
        self.generic_visit(node)

    # ---------- 内部检查 ----------
    def _check_func(self, node: ast.AST):
        args = getattr(node, "args", None)
        if args is not None:
            for d in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    self.issues.append({"kind": "mutable_default", "line": node.lineno,
                                        "detail": f"函数 {node.name} 使用可变默认参数"})
        # 不可达代码：函数体顶层 return/raise 之后还有语句
        body = getattr(node, "body", [])
        for i, stmt in enumerate(body[:-1]):
            if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                nxt = body[i + 1]
                self.issues.append({"kind": "unreachable", "line": nxt.lineno,
                                    "detail": f"{node.name} 中 {type(stmt).__name__} 之后存在不可达语句"})
                break
        # 局部变量赋值后未读取（保守：只查函数第一层，跳过 _ 前缀与解包）
        load_names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                      and isinstance(n.ctx, ast.Load)}
        for stmt in body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and \
                    isinstance(stmt.targets[0], ast.Name):
                nm = stmt.targets[0].id
                if nm.startswith("_") or nm in load_names:
                    continue
                self.issues.append({"kind": "unused_local", "line": stmt.lineno,
                                    "detail": f"{node.name} 中局部变量 {nm} 赋值后未读取"})

    def visit_Dict(self, node: ast.Dict):
        # 收集 registry 里的 TOOLS 键名（形如 "tool_name": {...}）
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)
                and isinstance(k.value, str)]
        if len(keys) >= 5 and all(re.match(r"^[a-z][a-z0-9_]*$", k) for k in keys):
            for k in keys:
                self.tool_names.append((k, node.lineno))
        self.generic_visit(node)


def scan() -> dict:
    files = py_files()
    facts: dict[Path, FileFacts] = {}
    per_file: list[dict] = []
    all_names_used: Counter = Counter()
    by_path: dict[str, list[dict]] = {}

    for p in files:
        rel = str(p.relative_to(PROJECT_ROOT))
        entry = {"file": rel, "lines": 0, "issues": [], "funcs": [], "status": "ok"}
        # 1) 语法检查：用内置 compile()，不落盘（沙箱下写临时 .pyc 会被拒）
        try:
            src = p.read_text(encoding="utf-8")
            compile(src, str(p), "exec")
        except SyntaxError as exc:
            entry["status"] = "compile_error"
            entry["issues"].append({"kind": "syntax_error", "line": exc.lineno or 0,
                                    "detail": f"{exc.msg}"})
        except Exception as exc:
            entry["status"] = "compile_error"
            entry["issues"].append({"kind": "compile_error", "line": 0, "detail": str(exc)[:200]})
        # 2) AST 分析
        try:
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception as exc:
            entry["issues"].append({"kind": "parse_error", "line": 0, "detail": str(exc)[:200]})
            per_file.append(entry)
            continue
        f = FileFacts(p)
        f.visit(tree)
        facts[p] = f
        entry["lines"] = len(src.splitlines())
        entry["funcs"] = f.funcs
        entry["issues"].extend(f.issues)
        # 【第二次审查新增】未定义名称（用与 compile 相同的源码做 symtable 分析）
        entry["issues"].extend(undefined_names(src, str(p)))
        entry["tool_names"] = sorted({k for k, _ in f.tool_names})
        all_names_used.update(f.names_used)
        per_file.append(entry)
        by_path[rel] = entry["issues"]

    # 3) 未使用的 import（模块级，按文件判断）
    for p, f in facts.items():
        entry = next(e for e in per_file if e["file"] == str(p.relative_to(PROJECT_ROOT)))
        for module, name, lineno in f.imports:
            if name == "*" or name.startswith("_"):
                continue
            # `from __future__ import annotations` 是编译期指令，不产生运行时读取
            if module == "__future__":
                continue
            # 该 import 的名字在本文件里是否出现过"读取"（Load）
            #
            # 【审查修正 P2】原来是 `if not (isinstance(node.ctx, ast.Store) and False):`
            # —— `and False` 让条件恒真、Store 上下文从未被排除，
            # 于是 `from x import y` 之后只写 `y = 1`（从未读取）也会被算作"已使用"，
            # unused_import 静默漏报。这里直接用 visit_Name 收集的 Load 计数判断。
            # 注意 import 语句自身不产生 Name 节点，因此无需排除行号。
            # 例外：位于 try 内、except 兜底的**能力探测导入**（如 `import psycopg` 探驱动）
            # 属于有意为之，单独识别后不计为问题。
            if f._load_counts.get(name, 0) == 0 and lineno not in f.optional_import_lines:
                entry["issues"].append({
                    "kind": "unused_import", "line": lineno,
                    "detail": f"import {name}（from {module}）在本文件未被读取使用"})

    # 4) 全仓未被"读取"的模块级定义（疑似死代码）
    #
    # 判据：该名字在【全仓任何文件】里既没有 Name 读取（Load），也没有被**属性访问**
    #       （`rules_store.annotate(...)` / `RS.release_stale()` 这类跨模块调用只会产生
    #        Attribute 节点，不会产生 Name 节点 —— 早先版本因此把 60+ 个在用的函数
    #        全判成死代码，噪声淹没了真正的问题）。
    # 代价是存在漏报（与标准库方法同名时会被当成"已引用"）：对死代码检测而言，
    # 宁可漏报也不能刷屏，否则报告没人看。
    unreferenced: list[dict] = []
    for p, f in facts.items():
        rel = str(p.relative_to(PROJECT_ROOT))
        decorated = getattr(f, "decorated_lines", set())
        for name, lineno in f.defs:
            if lineno in decorated:          # 路由/工具/属性：由框架调用
                continue
            if any(re.match(pat, name) for pat in DEF_EXEMPT_PATTERNS):
                continue
            total_loads = sum(g._load_counts.get(name, 0) for g in facts.values())
            total_attr_uses = sum(1 for g in facts.values() if name in g.names_used and
                                  g._load_counts.get(name, 0) == 0)
            if total_loads == 0 and total_attr_uses == 0:
                unreferenced.append({"file": rel, "line": lineno, "name": name})

    # 5) 数据库路径解析一致性
    db_path_usage: list[dict] = []
    for p, f in facts.items():
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"^PROJECT_ROOT\s*=\s*Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]",
                             src, re.M):
            db_path_usage.append({"file": str(p.relative_to(PROJECT_ROOT)),
                                  "line": src[:m.start()].count("\n") + 1,
                                  "parents_index": int(m.group(1))})

    # 6) registry 工具 vs spec 引用
    registry_tools: set[str] = set()
    for p, f in facts.items():
        if p.name == "tools_registry.py":
            registry_tools |= {k for k, _ in f.tool_names}
    spec_refs: dict[str, list[str]] = {}
    for spec in (PROJECT_ROOT / "workflows").glob("*.json"):
        try:
            doc = json.loads(spec.read_text(encoding="utf-8"))
        except Exception:
            continue
        names = [n.get("config", {}).get("tool_name", "") for n in doc.get("nodes", [])
                 if n.get("kind") == "tool"]
        spec_refs[spec.name] = sorted({n for n in names if n})

    # 7) TODO / 调试残留
    markers: list[dict] = []
    for p in files:
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"TODO|FIXME|XXX|HACK|print\(['\"]DEBUG", line):
                markers.append({"file": str(p.relative_to(PROJECT_ROOT)), "line": i,
                                "text": line.strip()[:120]})

    return {
        "files_scanned": len(files),
        "total_lines": sum(e["lines"] for e in per_file),
        "per_file": per_file,
        "unreferenced_defs": unreferenced,
        "db_path_roots": db_path_usage,
        "registry_tools": sorted(registry_tools),
        "spec_tool_refs": spec_refs,
        "markers": markers,
        "by_path": by_path,
    }


def main() -> int:
    result = scan()
    result["scanned_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # 汇总问题
    kinds: Counter = Counter()
    for e in result["per_file"]:
        for i in e["issues"]:
            kinds[i["kind"]] += 1
    result["issue_counts"] = dict(kinds)

    print(f"=== 静态审查：{result['files_scanned']} 个文件 / {result['total_lines']} 行 ===")
    print(f"问题分类计数: {result['issue_counts']}")
    compile_errs = [e for e in result["per_file"] if e["status"] != "ok"]
    print(f"编译/解析失败: {len(compile_errs)}")
    for e in compile_errs:
        print(f"   ! {e['file']}: {e['issues'][:1]}")

    # 未使用 import
    ui = [dict(i, file=e["file"]) for e in result["per_file"]
          for i in e["issues"] if i["kind"] == "unused_import"]
    print(f"\n未使用 import（{len(ui)}）:")
    for i in ui[:40]:
        print(f"   - {i['file']}:{i['line']} {i['detail']}")

    print(f"\n疑似死代码（模块级定义，全仓无引用）（{len(result['unreferenced_defs'])}）:")
    for i in result["unreferenced_defs"][:40]:
        print(f"   - {i['file']}:{i['line']} {i['name']}")

    # 其它类别
    for kind in ("undefined_name", "unused_local", "bare_except", "mutable_default",
                 "unreachable", "constant_condition"):
        items = [dict(i, file=e["file"]) for e in result["per_file"]
                 for i in e["issues"] if i["kind"] == kind]
        print(f"\n{kind}（{len(items)}）:")
        for i in items[:20]:
            print(f"   - {i['file']}:{i['line']} {i['detail']}")

    # DB 路径一致性
    print(f"\nDB 路径解析（PROJECT_ROOT 的 parents 索引）:")
    idx = Counter(d["parents_index"] for d in result["db_path_roots"])
    for k, v in sorted(idx.items()):
        files = [d["file"] for d in result["db_path_roots"] if d["parents_index"] == k]
        print(f"   parents[{k}] × {v}: {files[:6]}{' ...' if len(files) > 6 else ''}")

    # registry vs spec
    print(f"\nregistry 工具 {len(result['registry_tools'])} 个")
    for name, refs in result["spec_tool_refs"].items():
        missing = [r for r in refs if r and r not in result["registry_tools"]]
        print(f"   {name}: 引用 {refs}；未在 registry 中: {missing or '无'}")

    print(f"\nTODO/调试残留（{len(result['markers'])}）:")
    for m in result["markers"][:15]:
        print(f"   - {m['file']}:{m['line']} {m['text']}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n证据: {EVIDENCE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
