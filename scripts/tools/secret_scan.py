#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
推送前密钥扫描（本项目自有工具）

为什么需要：这个项目要读 `.env`（大模型密钥、SMTP 口令、Webhook 令牌），
而"把 .env 提交上去"是最高频、后果最严重的一类事故 —— 一旦 push 出去，
即使立刻删除，历史里也还在（必须 rewrite history + 轮换密钥）。

做法：**不对整个工作区扫描，只扫"会被推上去的东西"** —— 用 `git ls-files`
（= 索引里的文件，也就是 GitHub 上会出现的文件集），因此不会误报被 .gitignore
排除的本机文件，也不会漏掉"已经 git add 但还没提交"的危险文件。

用法：
    python scripts/tools/secret_scan.py            # 扫索引（即将推送的内容）
    python scripts/tools/secret_scan.py --all      # 连工作区未跟踪文件一起扫（更严）
    python scripts/tools/secret_scan.py --env .runtime/.lemonclaw/.env
                                                   # 额外用真实 .env 的值做精确比对

降噪（重要）：报告里全是假阳性，人就会开始无视报告。因此本工具做了两层收敛：
    1) **模板值自动豁免**：若某个"密钥值"同时出现在已提交的模板
       `.env.enterprise.example` 里，那它按定义是公开占位符（例如演示令牌
       `local-dev-token`），不再报；
    2) **显式允许清单** `.secret-scan-allow`：一行一条正则，带 `#` 注释说明为什么安全。
       要豁免什么必须写下来 —— 这比"静默忽略"更可审计。

退出码：0 = 干净；1 = 有命中（可直接用作 pre-push 钩子）
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT_ROOT / ".env.enterprise.example"
ALLOW_FILE = PROJECT_ROOT / ".secret-scan-allow"

# 形态匹配：即使没有真实 .env 可比对，也能抓出"看起来就是密钥"的东西
PATTERNS = [
    ("OpenAI/DeepSeek 一类密钥", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("Bearer JWT", re.compile(r"Bearer\s+eyJ[A-Za-z0-9_\-\.]{20,}")),
    ("赋值型口令", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"][^'\"\s]{6,}['\"]")),
    ("赋值型密钥", re.compile(r"(?i)\b(api[_-]?key|secret|access[_-]?token)\s*[:=]\s*"
                              r"['\"][^'\"\s]{12,}['\"]")),
]

TEXT_SUFFIXES = {".py", ".ps1", ".md", ".json", ".yml", ".yaml", ".ini", ".cfg", ".toml",
                 ".txt", ".example", ".sh", ".bat", ".cmd", ".html", ".js", ".css", ".env",
                 ".gitignore", ".gitattributes", ".sql", ".csv", ""}
MAX_BYTES = 4 * 1024 * 1024


def mask(s: str, keep: int = 4) -> str:
    s = " ".join(s.strip().split())
    return (s[:keep] + "***" + s[-2:]) if len(s) > keep + 2 else "***"


def tracked_files(all_files: bool) -> list[Path]:
    """索引里的文件（即将推送的内容）。--all 时并入工作区未忽略文件。"""
    cmd = ["git", "ls-files"] + (["--cached", "--others", "--exclude-standard"] if all_files
                                 else ["--cached"])
    try:
        out = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60).stdout
    except Exception as exc:
        print(f"[secret] 无法执行 git（{type(exc).__name__}: {exc}）；改用工作区遍历")
        return [p for p in PROJECT_ROOT.rglob("*") if p.is_file()]
    return [PROJECT_ROOT / line.strip() for line in out.splitlines() if line.strip()]


def env_secret_values(env_path: Path) -> list[tuple[str, str]]:
    """从真实 .env 里取出密钥值，用于**精确比对**（值一样就是泄漏，最可靠）。

    自动降噪：同时收集**已提交模板** `.env.enterprise.example` 里的同名值，
    相同的一律剔除 —— 会写进公开模板的值，按定义不是秘密（典型：演示令牌）。
    """
    out: list[tuple[str, str]] = []
    if not env_path.exists():
        return out
    public_values = {v for _, v in _raw_secret_values(TEMPLATE)}
    for k, v in _raw_secret_values(env_path):
        if v in public_values:
            print(f"[secret] 跳过占位符值：{k}（与已提交模板 .env.enterprise.example 相同）")
            continue
        out.append((k, v))
    return out


def _raw_secret_values(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and re.search(r"KEY|SECRET|TOKEN|PASSWORD", k, re.I) and len(v) >= 8:
            out.append((k, v))
    return out


def allowed_patterns() -> list[tuple[str, re.Pattern]]:
    """读取显式允许清单（一行一条正则，# 起注释）。"""
    out: list[tuple[str, re.Pattern]] = []
    if not ALLOW_FILE.exists():
        return out
    for raw in ALLOW_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append((line, re.compile(line)))
        except re.error as exc:
            print(f"[secret] 允许清单里有非法正则，已跳过：{line}（{exc}）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="推送前密钥扫描")
    ap.add_argument("--all", action="store_true", help="连未跟踪（未忽略）文件一起扫")
    ap.add_argument("--env", default=".runtime/.lemonclaw/.env",
                    help="真实 .env 路径（用于精确值比对；不存在则跳过）")
    args = ap.parse_args()

    files = tracked_files(args.all)
    secrets = env_secret_values(PROJECT_ROOT / args.env)
    allows = allowed_patterns()
    scope = "索引 + 未跟踪未忽略文件" if args.all else "索引（= 即将推送的内容）"
    print(f"[secret] 扫描范围：{scope}，共 {len(files)} 个文件")
    print(f"[secret] 精确比对用密钥：{len(secrets)} 个"
          f"{'（' + ', '.join(k for k, _ in secrets) + '）' if secrets else '（无）'}"
          f"；允许清单：{len(allows)} 条")

    hits: list[tuple[str, int, str, str]] = []
    exempt: list[tuple[str, int, str]] = []
    for f in files:
        if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if f.stat().st_size > MAX_BYTES:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = str(f.relative_to(PROJECT_ROOT))

        def record(line_no: int, label: str, sample: str) -> None:
            line = text.splitlines()[line_no - 1] if 0 < line_no <= len(text.splitlines()) else ""
            for _, pat in allows:
                if pat.search(line) or pat.search(sample):
                    exempt.append((rel, line_no, label))
                    return
            hits.append((rel, line_no, label, mask(sample)))
            return

        for name, val in secrets:
            for m in re.finditer(re.escape(val), text):
                record(text[:m.start()].count("\n") + 1, f"真实密钥值泄漏（{name}）", val)
        for label, pat in PATTERNS:
            for m in pat.finditer(text):
                record(text[:m.start()].count("\n") + 1, label, m.group(0))

    uniq = sorted(set(hits))
    print(f"\n[secret] 命中 {len(uniq)} 处（另有 {len(set(exempt))} 处按允许清单豁免）：")
    for rel, line_no, label, sample in uniq:
        print(f"  {rel}:{line_no}  {label}  {sample}")
    if not uniq:
        print("  （无）—— 可以安全推送")
        return 0
    print("\n[secret] 若确认是误报（演示令牌/占位符/自检用一次性口令），"
          "把该行的正则加进 .secret-scan-allow 并写明理由；\n"
          "          若确属真密钥：git rm --cached <file>、加进 .gitignore，"
          "并**轮换该密钥**（历史里的旧值仍然有效）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
