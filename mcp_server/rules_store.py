#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
风险规则表读写与提案流程（本项目自有，MCP 服务使用）

唯一真相源：`rules/risk_rules.yaml`
审计方式：条目内联审计字段（source / approved_by / approved_at / changelog），不单独建历史表
写入方式：临时文件 + 原子替换（避免并发写坏 YAML）

核心业务纪律（方案设计 §3.3 / 任务书 §4.3）：
    1) 人工驳回**绝不直接改规则库**：只能生成「规则提案」，经主管批准后才版本化写入
    2) 去重：模式精确匹配优先；否则 difflib 相似度 > 0.85 视为同一规则 -> 转「修订提案」（version+1）
    3) 冲突：同一模式但 severity 不同 -> 标注 conflict_with，交由主管人工裁决，禁止静默覆盖
    4) 不做语义嵌入去重、不做自动冲突消解（一/二期边界，见任务书）
"""

import difflib
import hashlib
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import yaml

# 本文件位于 <project>/mcp_server/，因此项目根是上一级
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = PROJECT_ROOT / "rules" / "risk_rules.yaml"
PROPOSALS_DIR = PROJECT_ROOT / "data" / "proposals"
SIMILARITY_THRESHOLD = 0.85

# 【审查修正 P1】规则表是**唯一真相源**，而 approve/set_status/delete_rule/restore
# 都是"读全表 → 改内存 → 整表写回"的读改写序列。没有互斥时：
#   两个并发审批各自读到同一版本 → 后写者覆盖前写者 → **已批准的规则凭空消失**。
# 进程内用锁串行化（跨进程仍以 version 校验兜底，见 approve 的版本前置检查）。
_WRITE_LOCK = threading.RLock()

# 提案编号格式（生成处：RP-YYYYMMDDHHMMSS-xxxx，见 propose() 的 datetime 格式化）
# 【审查修正】这里原来写成 `RP-\d{8}-\d{6}-[0-9a-f]{4}`（日期与时间之间多了个连字符），
# 而 propose() 实际生成的是 `RP-<14位数字>-<4位十六进制>` —— 格式白名单与生成器的格式不一致，
# 结果**新建的提案一律无法 approve/reject**（proposal_path 判为非法编号）。
PROPOSAL_ID_RE = re.compile(r"^RP-\d{14}-[0-9a-f]{4}$")


def proposal_path(proposal_id: str) -> Path:
    """把提案编号安全地解析为文件路径。

    【安全修正】此前直接用 `PROPOSALS_DIR / f"{proposal_id}.json"`，
    传入 `../../docs/evidence/xxx` 这类编号即可读写任意 JSON 文件
    （HTTP 与 MCP 入口都只做透传、不校验 schema）。
    现在：先做格式白名单，再解析后断言父目录就是提案目录。
    """
    if not isinstance(proposal_id, str) or not PROPOSAL_ID_RE.match(proposal_id):
        raise ValueError(f"提案编号格式非法：{proposal_id!r}（应为 RP-YYYYMMDDHHMMSS-xxxx）")
    base = PROPOSALS_DIR.resolve()
    p = (PROPOSALS_DIR / f"{proposal_id}.json").resolve()
    if p.parent != base:
        raise ValueError(f"提案路径越界：{proposal_id!r}")
    return p

SEVERITY_ORDER = {"low": 1, "mid": 2, "high": 3}
VALID_SEVERITY = set(SEVERITY_ORDER)
VALID_STATUS = {"draft", "active", "deprecated"}


# ------------------------------------------------------------------ 读

def load_all(force: bool = False) -> list[dict]:
    """读取规则表。

    不做进程内缓存：规则表是**唯一真相源**，且自检/回滚/提案写入都会改它，
    一旦缓存就会出现"磁盘已还原、内存仍是旧值"的不一致（P2 自检实测踩过）。
    YAML 很小，每次读取的代价可以忽略。
    """
    if not RULES_PATH.exists():
        return []
    raw = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or []
    return [r for r in raw if isinstance(r, dict) and r.get("id")]


def active() -> list[dict]:
    return [r for r in load_all() if r.get("status") == "active" and r.get("pattern")]


def digest(rules: list[dict] | None = None) -> str:
    rules = active() if rules is None else rules
    payload = "|".join(f"{r['id']}:{r.get('version', 1)}:{r['pattern']}:{r.get('severity')}"
                       for r in sorted(rules, key=lambda x: x["id"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def get(rule_id: str) -> dict | None:
    for r in load_all():
        if r["id"] == rule_id:
            return r
    return None


# ------------------------------------------------------------------ 标注

def annotate(clause_text: str, clause_id: str = "") -> dict:
    """规则通道标注。命中必须带 rule_id 与 severity；有引用才算风险。"""
    hits = []
    for rule in active():
        try:
            m = re.search(rule["pattern"], clause_text or "")
        except re.error:
            continue                      # 坏正则不得让整条链路失败
        if not m:
            continue
        hits.append({
            "rule_id": rule["id"],
            "rule_version": rule.get("version", 1),
            "severity": rule.get("severity", "mid"),
            "match": m.group(0),
            "regulation_ref": rule.get("regulation_ref"),
            "suggestion": rule.get("suggestion"),
            "source": rule.get("source", "seed"),
            "priority": rule.get("priority", 0),
        })
    if hits:
        level = max(hits, key=lambda h: SEVERITY_ORDER.get(h["severity"], 0))["severity"]
    else:
        level = "low"
    return {
        "clause_id": clause_id,
        "clause_text": clause_text,
        "risk_level": level,
        "risk_hits": hits,
        "has_citation": all(bool(h.get("regulation_ref")) for h in hits) if hits else True,
        "rule_table_digest": digest(),
        "advisory_only": True,
    }


# ------------------------------------------------------------------ 去重

def _norm_pattern(p: str) -> str:
    """模式归一化：抹平正则写法差异，让"语义相同、写法不同"能被识别为同一规则。

    处理：去空白、统一转义写法、统一字符类写法、**去掉量词**（量词不影响"是不是同一条规则"）。

    【审查修正 P2】原实现是 `s.replace("\\\\", "")`（删掉**所有**反斜杠），
    这把转义字符变成了普通字符：`\\.`（字面点）与 `.`（任意字符）归一化后相同 —— 误判；
    而 `[0-9]{1,}` 与 `\\d+` 这类等价写法因残留字符不同 —— 漏判。
    现在：只移除"转义用的反斜杠"，并把量词一并抹除（`\\d{1,}` ≡ `\\d+` ≡ `\\d`），
    这样字符类与转义写法能真正归一，同时不会把"匹配数字"和"匹配字母 d"混为一谈。
    """
    s = re.sub(r"\s+", "", (p or "").strip())
    s = s.replace("[0-9]", r"\d").replace("[0-9]{1,}", r"\d")
    s = re.sub(r"\{\d*,?\d*\}", "", s)                   # 量词 {m,n} / {m,} / {m}
    s = re.sub(r"(?<=[0-9A-Za-z\u4e00-\u9fff\)\]])[+*?]", "", s)   # 量词 + * ?（不碰 (?: (?= 等分组）
    s = re.sub(r"\\(?=[^0-9A-Za-z])", "", s)             # 去掉转义反斜杠（\. \- \| …）
    s = re.sub(r"(?<![0-9A-Za-z\\.])\\(?=[0-9A-Za-z])", "", s)   # 去掉多余的单反斜杠（非转义序列）
    return s


def _literal_core(p: str) -> str:
    """提取模式中的"字面核心"（去掉元字符与转义序列），用于包含关系判定。

    例：`最终解释权归.{0,4}方所有` -> `最终解释权归方所有`
        `月.{0,4}\\d+\\s*(小时)`   -> `月小时`（\\d/\\s 等转义序列一并剔除）
    """
    s = _norm_pattern(p)
    # 顺序很重要：先把转义类整体抹成空（否则 `\d` 会残留一个 `d` 变成字面核心的一部分，
    # 让"月工时上限"这类模式的核心里混进字母，包含判定跟着失真）
    s = re.sub(r"\\[A-Za-z]", "", s)          # 转义序列 \d \s \w 等
    s = re.sub(r"\{\d*,?\d*\}", "", s)        # 量词 {m,n}
    s = re.sub(r"\[[^\]]*\]", "", s)          # 字符类
    s = re.sub(r"[.?*+^$]", "", s)            # 通配与锚点（'.' 必须去掉，否则核心串里残留点号）
    s = s.replace("|", "")                    # 或分支：拼接后仍保留全部字面词
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", s)   # 仅保留字面字符

def _longest_common(a: str, b: str) -> str:
    """最长公共子串（用于判定"包含关系"）。"""
    if not a or not b:
        return ""
    m = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return a[m.a:m.a + m.size]


# 包含判定阈值（实测校准）：
#   1) 公共字面核心 ≥4 个字符（中文风险特征词普遍 ≥4 字：最终解释权/保本收益/自动续约…）
#   2) 公共部分至少占"较短核心"的 60%（否则只是共享前缀，不是同一规则）
# 反例说明：`自动续约|自动顺延` 与 `自动续约.{0,6}无需.{0,4}通知` 只共享前缀"自动续约"
# （4/8=50% < 60%），属于**不同规则**，必须走"新增"而不是"修订"——
# D0/P2 自检都因此误判过，故加占比约束。
CONTAINMENT_MIN_COMMON = 4
CONTAINMENT_MIN_COVERAGE = 0.6


def find_similar(pattern: str, threshold: float = SIMILARITY_THRESHOLD) -> dict:
    """三层去重检测（从强到弱）：

        1) exact       归一化后完全相同            -> 修订提案
        2) containment 一方的字面核心包含另一方     -> 修订提案（防止同义规则重复入库）
        3) similar     difflib 相似度 > threshold  -> 修订提案

    第 2 层是实测补上的：中文正则的写法差异会让 difflib 相似度偏低
    （例：`最终解释权` vs `最终解释权归.{0,4}方所有` 仅 0.5），
    若只靠阈值判定，会导致**同义规则以"新规则"身份重复入库、同一文本命中多条规则**。
    这里仍不做语义嵌入去重（保持一期边界）。
    """
    target = _norm_pattern(pattern)
    target_core = _literal_core(pattern)
    exact, best, best_ratio, contained = None, None, 0.0, None
    for rule in load_all():
        raw = rule.get("pattern", "")
        cand = _norm_pattern(raw)
        if cand and cand == target:
            exact = rule
            break
        cand_core = _literal_core(raw)
        if not contained and cand_core and target_core:
            common = _longest_common(target_core, cand_core)
            shorter = min(len(target_core), len(cand_core))
            if len(common) >= CONTAINMENT_MIN_COMMON and shorter and \
                    len(common) / shorter >= CONTAINMENT_MIN_COVERAGE:
                contained = rule
        ratio = difflib.SequenceMatcher(None, target, cand).ratio() if cand else 0.0
        if ratio > best_ratio:
            best, best_ratio = rule, ratio
    if exact:
        return {"exact": exact, "containment": None, "similar": None, "ratio": 1.0, "layer": "exact"}
    if contained:
        return {"exact": None, "containment": contained, "similar": None,
                "common_core": _longest_common(target_core, _literal_core(contained.get("pattern", ""))),
                "ratio": round(best_ratio, 4), "layer": "containment"}
    if best and best_ratio > threshold:
        return {"exact": None, "containment": None, "similar": best,
                "ratio": round(best_ratio, 4), "layer": "similar"}
    return {"exact": None, "containment": None, "similar": None,
            "ratio": round(best_ratio, 4), "layer": "none"}


def conflict_check(pattern: str, severity: str) -> list[dict]:
    """同模式但等级不同的冲突（需主管裁决）。"""
    target = _norm_pattern(pattern)
    out = []
    for rule in load_all():
        if _norm_pattern(rule.get("pattern", "")) == target and rule.get("severity") != severity:
            out.append({"rule_id": rule["id"], "severity": rule.get("severity"),
                        "version": rule.get("version", 1), "status": rule.get("status")})
    return out


def _next_rule_id() -> str:
    ids = []
    for r in load_all():
        m = re.match(r"RR-(\d+)$", str(r.get("id", "")))
        if m:
            ids.append(int(m.group(1)))
    return f"RR-{(max(ids) + 1) if ids else 1:03d}"


# ------------------------------------------------------------------ 提案

def propose(reject_reason: str, pattern: str, severity: str, regulation_ref: str,
            suggestion: str, contract_id: str = "", proposed_by: str = "",
            evidence: str = "") -> dict:
    """生成规则提案（不直接改规则库）。

    返回：{proposal_id, kind(revise|new), target_rule_id, version, conflict_with, ...}
    """
    problems = []
    if not (reject_reason or "").strip():
        problems.append("驳回原因不能为空")
    if not (pattern or "").strip():
        problems.append("待新增/修订的匹配模式不能为空")
    else:
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"匹配模式不是合法正则：{exc}")
    if severity not in VALID_SEVERITY:
        problems.append(f"severity 必须是 {sorted(VALID_SEVERITY)} 之一")
    if problems:
        return {"ok": False, "errors": problems}

    dup = find_similar(pattern)
    conflicts = conflict_check(pattern, severity)
    proposal_id = f"RP-{datetime.now().strftime('%Y%m%d%H%M%S')}-{__import__('uuid').uuid4().hex[:4]}"
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)

    def _persist(obj: dict) -> dict:
        (proposal_path(obj['proposal_id'])).write_text(
            __import__("json").dumps(obj, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        return obj

    # 冲突优先级高于去重：同一模式下等级不一致是**必须先由人裁决**的分歧，
    # 不能被"自动转修订提案"悄悄吸收掉。
    if conflicts:
        return _persist({
            "ok": True,
            "proposal_id": proposal_id,
            "kind": "conflict",
            "target_rule_id": conflicts[0]["rule_id"],
            "proposed_rule_id": conflicts[0]["rule_id"],
            "version": None,
            "pattern": pattern, "severity": severity,
            "regulation_ref": regulation_ref, "suggestion": suggestion,
            "reason": reject_reason, "contract_id": contract_id, "evidence": evidence,
            "proposed_by": proposed_by or "unknown",
            "status": "pending_supervisor",
            "dedup": {"layer": "conflict", "exact_rule": None, "containment_rule": None,
                      "similar_rule": None, "similarity": None, "threshold": SIMILARITY_THRESHOLD,
                      "note": "同模式但等级不同，需主管人工裁决"},
            "conflict_with": conflicts,
            "conflict_requires_manual": True,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "存在同模式异等级冲突：禁止静默覆盖，请主管裁决后再生成修订提案",
        })

    if dup["exact"]:
        target = dup["exact"]
        kind = "revise"
        new_version = int(target.get("version", 1)) + 1
        note = f"模式与 {target['id']} 归一化后完全一致，转为修订提案（version {target.get('version',1)} -> {new_version}）"
    elif dup.get("containment"):
        target = dup["containment"]
        kind = "revise"
        new_version = int(target.get("version", 1)) + 1
        note = (f"与 {target['id']} 存在包含关系（字面核心互相包含），"
                f"按同义规则处理并转为修订提案（version {target.get('version',1)} -> {new_version}）")
    elif dup["similar"]:
        target = dup["similar"]
        kind = "revise"
        new_version = int(target.get("version", 1)) + 1
        note = (f"与 {target['id']} 模式相似度 {dup['ratio']} > {SIMILARITY_THRESHOLD}，"
                f"转为修订提案（version {target.get('version',1)} -> {new_version}）")
    else:
        target = None
        kind = "new"
        new_version = 1
        note = "未发现同模式、包含关系或高相似规则，作为新规则提案"

    proposal = {
        "ok": True,
        "proposal_id": proposal_id,
        "kind": kind,
        "target_rule_id": target["id"] if target else None,
        "proposed_rule_id": target["id"] if target else _next_rule_id(),
        "version": new_version,
        "pattern": pattern,
        "severity": severity,
        "regulation_ref": regulation_ref,
        "suggestion": suggestion,
        "reason": reject_reason,
        "contract_id": contract_id,
        "evidence": evidence,
        "proposed_by": proposed_by or "unknown",
        "status": "pending_supervisor",
        "dedup": {"exact_rule": dup["exact"]["id"] if dup["exact"] else None,
                  "containment_rule": (dup.get("containment") or {}).get("id"),
                  "similar_rule": dup["similar"]["id"] if dup["similar"] else None,
                  "layer": dup.get("layer"),
                  "similarity": dup["ratio"], "threshold": SIMILARITY_THRESHOLD,
                  "note": note},
        "conflict_with": conflicts,
        "conflict_requires_manual": bool(conflicts) and kind == "conflict",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return _persist(proposal)


def list_proposals() -> list[dict]:
    import json
    if not PROPOSALS_DIR.exists():
        return []
    out = []
    for p in sorted(PROPOSALS_DIR.glob("RP-*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


# ------------------------------------------------------------------ 写入

def _atomic_write(rules: list[dict]) -> None:
    """原子替换 YAML（同目录临时文件 + os.replace）。

    【审查修正 P2】原来用 `shutil.move(tmp, RULES_PATH)`：Windows 上 os.rename 对已存在
    目标必抛 FileExistsError，shutil.move 于是退化为 copy2 + unlink —— 写盘与删除之间有
    窗口期，此刻读取会拿到半个文件。`os.replace` 在 Windows/POSIX 上都是真原子替换。
    """
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(RULES_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# 合规风险规则表（唯一真相源）—— 由规则提案流程写入，禁止并发手工修改\n")
            # default=str：YAML 会把形如 2026-10-09 的值解析成 date，写回时统一为字符串，
            # 避免"读进来是 date、写回去是 str"造成类型漂移
            yaml.safe_dump(rules, fh, allow_unicode=True, sort_keys=False, width=200,
                           default_flow_style=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, RULES_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def set_status(rule_id: str, status: str, actor: str = "", reason: str = "") -> dict:
    """变更规则状态（用于二期灰度与自检回滚）。正常业务路径应走提案审批。"""
    if status not in VALID_STATUS:
        return {"ok": False, "errors": [f"status 必须是 {sorted(VALID_STATUS)} 之一"]}
    with _WRITE_LOCK:
        rules = load_all()
        for r in rules:
            if r.get("id") == rule_id:
                old = r.get("status")
                r["status"] = status
                r["changelog"] = (f"{r.get('changelog','')}｜状态由 {old} 变更为 {status}"
                                  f"（{actor or 'system'}：{reason or '未说明'}）").strip("｜")
                _atomic_write(rules)
                return {"ok": True, "rule_id": rule_id, "from": old, "to": status,
                        "new_digest": digest()}
    return {"ok": False, "errors": [f"规则不存在：{rule_id}"]}


def delete_rule(rule_id: str, actor: str = "", reason: str = "") -> dict:
    """删除规则（仅用于自检清理与误入库回滚；正式流程用 deprecated）。"""
    with _WRITE_LOCK:
        rules = load_all()
        kept = [r for r in rules if r.get("id") != rule_id]
        if len(kept) == len(rules):
            return {"ok": False, "errors": [f"规则不存在：{rule_id}"]}
        _atomic_write(kept)
        return {"ok": True, "rule_id": rule_id, "actor": actor, "reason": reason,
                "new_digest": digest()}


def restore(rules: list[dict]) -> dict:
    """整表还原（供自检/回滚使用）。"""
    with _WRITE_LOCK:
        _atomic_write(rules)
        return {"ok": True, "count": len(rules), "new_digest": digest()}


def approve(proposal_id: str, approved_by: str) -> dict:
    """主管批准提案 -> 版本化写入规则表。

    冲突（同模式异等级）必须由调用方确认已人工裁决，否则拒绝写入。
    """
    import json
    try:
        path = proposal_path(proposal_id)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)]}
    if not path.exists():
        return {"ok": False, "errors": [f"提案不存在：{proposal_id}"]}
    proposal = json.loads(path.read_text(encoding="utf-8"))
    if proposal.get("status") == "approved":
        return {"ok": False, "errors": [f"提案已批准过（{proposal_id}），拒绝重复写入"]}
    if not (approved_by or "").strip():
        return {"ok": False, "errors": ["approved_by 不能为空（审计要求）"]}
    # active 规则必须有法条引用：把"无引用不判风险"前移到**写入关口**，
    # 否则会产出永远命中却给不出依据的规则（P2 自检实测踩过：提案缺引用时写入了空引用规则）
    if not (proposal.get("regulation_ref") or "").strip():
        return {"ok": False, "errors": [
            "提案缺少 regulation_ref：按『无引用不判风险』原则，禁止写入无法条依据的 active 规则"]}
    if proposal.get("kind") == "conflict":
        return {"ok": False, "errors": [
            "该提案为『同模式异等级冲突』，必须由主管人工裁决后重新生成提案，禁止直接写入"]}
    if proposal.get("kind") not in ("new", "revise"):
        return {"ok": False, "errors": [f"未知提案类型：{proposal.get('kind')!r}"]}
    with _WRITE_LOCK:
        return _approve_locked(proposal_id, approved_by, path, proposal)


def _approve_locked(proposal_id: str, approved_by: str, path: Path,
                    proposal: dict) -> dict:
    """approve 的临界区（持有 _WRITE_LOCK）。"""
    import json
    # 重读提案状态：校验在锁外完成，锁内必须确认它没有被并发改判（approve/reject 竞态）
    fresh = json.loads(path.read_text(encoding="utf-8"))
    if fresh.get("status") in ("approved", "rejected"):
        return {"ok": False, "errors": [
            f"提案已被处理（当前状态 {fresh.get('status')}），拒绝重复写入"]}
    proposal = fresh
    if proposal.get("kind") == "revise":
        target = get(proposal["target_rule_id"])
        if target is None:
            return {"ok": False, "errors": [f"目标规则不存在：{proposal['target_rule_id']}"]}
        if int(target.get("version", 1)) >= int(proposal["version"]):
            return {"ok": False, "errors": [
                f"提案版本 {proposal['version']} 不高于现值 {target.get('version')}，"
                "可能存在并发修订，请重新生成提案"]}
    else:
        # 【审查修正 P1】新增提案的编号必须仍然空闲：propose() 生成编号到 approve() 之间，
        # 可能已有别的提案先被批准并占用了同一个 RR-xxx。原实现对此毫无检查，
        # （配合下面那行被误用的"过滤"语句）会**静默覆盖/删除**已生效的规则。
        occupied = get(proposal["proposed_rule_id"])
        if occupied is not None:
            return {"ok": False, "errors": [
                f"规则编号 {proposal['proposed_rule_id']} 已被占用"
                f"（version {occupied.get('version')}，status {occupied.get('status')}）："
                "请重新生成提案（编号在提交时分配，审批时不再重新分配）"]}

    rules = load_all()
    # 【审查修正 P1】原来这里有
    #     rules = [r for r in rules if r.get("id") != proposal["proposed_rule_id"]
    #              or proposal["kind"] == "revise"]
    # 对 kind="new" 而言它等价于"无条件删掉同编号的既有规则"，然后下面又在
    # 同编号存在时改写它 —— 与"新增"语义矛盾，且会静默丢弃一条规则。
    # 现在：新增走上面的占用校验（冲突即拒绝），修订才允许按编号替换。
    stamp = datetime.now().strftime("%Y-%m-%d")
    entry = {
        "id": proposal["proposed_rule_id"],
        "version": proposal["version"],
        "status": "active",
        "pattern": proposal["pattern"],
        "severity": proposal["severity"],
        "priority": 8 if proposal["severity"] == "high" else (6 if proposal["severity"] == "mid" else 3),
        "regulation_ref": proposal.get("regulation_ref") or "",
        "suggestion": proposal.get("suggestion") or "",
        "source": "proposal",
        "approved_by": approved_by,
        "approved_at": stamp,
        "changelog": (f"来自提案 {proposal_id}（合同 {proposal.get('contract_id') or 'n/a'}）；"
                      f"驳回原因：{proposal.get('reason','')[:120]}"),
    }
    replaced = False
    for i, r in enumerate(rules):
        if r.get("id") == entry["id"]:
            entry["source"] = "seed+proposal" if r.get("source", "").startswith("seed") else "proposal"
            rules[i] = entry
            replaced = True
            break
    if not replaced:
        rules.append(entry)

    _atomic_write(rules)
    proposal["status"] = "approved"
    proposal["approved_by"] = approved_by
    proposal["approved_at"] = stamp
    path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "rule_id": entry["id"], "version": entry["version"],
            "replaced": replaced, "new_digest": digest(),
            "rule": entry}


def reject(proposal_id: str, rejected_by: str, reason: str = "") -> dict:
    import json
    try:
        path = proposal_path(proposal_id)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)]}
    if not path.exists():
        return {"ok": False, "errors": [f"提案不存在：{proposal_id}"]}
    with _WRITE_LOCK:
        proposal = json.loads(path.read_text(encoding="utf-8"))
        # 【审查修正 P2】原实现允许把已批准（规则已写入）的提案再标成 rejected：
        # 规则表里是生效状态、提案文件里是被驳回 —— 审计链路自相矛盾。
        if proposal.get("status") == "approved":
            return {"ok": False, "errors": [
                f"提案已批准（规则 {proposal.get('proposed_rule_id')} 已写入规则表），"
                "不能改判为驳回；如需撤销请生成修订/废弃提案"]}
        if proposal.get("status") == "rejected":
            return {"ok": False, "errors": [f"提案已被驳回（{proposal_id}），拒绝重复操作"]}
        proposal["status"] = "rejected"
        proposal["rejected_by"] = rejected_by
        proposal["rejected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        proposal["reject_reason"] = reason
        path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "proposal_id": proposal_id}


def diff_since(rules_before: list[dict]) -> dict:
    """返回规则表差异（供审计展示）。"""
    before = {r["id"]: r for r in rules_before}
    after = {r["id"]: r for r in load_all()}
    added = [k for k in after if k not in before]
    removed = [k for k in before if k not in after]
    changed = [k for k in after if k in before and before[k] != after[k]]
    return {"added": added, "removed": removed, "changed": changed,
            "digest_before": _digest_of(rules_before), "digest_after": digest()}


def _digest_of(rules: list[dict]) -> str:
    payload = "|".join(f"{r['id']}:{r.get('version',1)}:{r.get('pattern')}:{r.get('severity')}"
                       for r in sorted(rules, key=lambda x: x["id"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
