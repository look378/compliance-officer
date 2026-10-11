#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外部系统连接器（合同来源 / 邮件网关）—— 把 Mock 换成人家的系统，代码不动

设计目标（对应 `docs/12-代码审查报告.md` §6 的第一项门槛）：
    企业环境里"外部系统"至少有三种形态，都要能接：
      1) **共享盘/本地目录**：OA 把合同投到 \\\\fileserver\\compliance-inbox 这类目录 —— 企业里最常见
      2) **HTTP/JSON 接口**：OA 或合同库提供 REST 接口（字段名各家不同，必须可映射）
      3) **测试与演练用的 Mock**：本项目 `mock/mock_office.py`（含故障注入）

因此本模块做三件事：
    A. 定义**与厂商无关的连接器契约**（Protocol）：`list_contracts / fetch_contract / send_mail`
    B. 提供上面三种实现，由环境变量选择（`CONTRACT_SOURCE` / `MAIL_GATEWAY`）
    C. 统一**结构校验与降级语义**：外部系统返回结构不对时必须能被识别为"不可用"，
       绝不能把"字段改名了"当成"没有新合同"（那是静默漏审，是合规系统最不能接受的失败模式）

约定（与方案设计 §7 容错矩阵一致）：
    - 所有方法都不抛异常，返回结构化结果 `{ok, ...}` 或 `{ok: False, degraded: True, error}`
    - 任何"成功"结果都必须带上可核验的字段，调用方不需要猜
    - 幂等依据是**内容哈希**（`content_hash`）：同一份合同重复投递不得重复审查

配置（`.env` / 环境变量）：
    CONTRACT_SOURCE      mock | dir | http        （默认 mock，保持开箱即用）
    MAIL_GATEWAY         mock | http | smtp       （默认 mock；smtp 需已装 smtplib 之外的依赖则自行扩展）
    MOCK_URL             Mock 服务地址（默认 http://127.0.0.1:8780）
    CONTRACT_DIR         共享盘/收件目录（dir 模式）
    CONTRACT_DIR_RECURSIVE  是否递归扫描子目录（默认 false）
    OA_BASE_URL / OA_LIST_PATH / OA_FETCH_PATH   HTTP 模式的接口地址（支持 {contract_id} 占位）
    OA_LIST_ITEMS_KEY / OA_ID_FIELD / OA_HASH_FIELD / OA_TIME_FIELD / OA_SIZE_FIELD
                         接口字段映射（默认按本项目契约：items/contract_id/content_hash/uploaded_at/size）
    OA_AUTH_HEADER / OA_AUTH_TOKEN              接口鉴权（如 Authorization: Bearer xxx）
    OA_TIMEOUT_S / OA_VERIFY_TLS                 超时与证书校验
    MAIL_HTTP_URL                                邮件网关 HTTP 地址（MAIL_GATEWAY=http）
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_STARTTLS
"""

from __future__ import annotations

import hashlib
import json
import os
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol


# ============================================================ 结果类型

@dataclass
class ListResult:
    """收件目录列表结果。`ok=False` 时 `degraded=True` 且必须带 `error`。"""

    ok: bool
    items: list[dict] = field(default_factory=list)
    degraded: bool = False
    error: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "items": self.items, "degraded": self.degraded,
                "error": self.error, "count": len(self.items), "source": self.source}


@dataclass
class FetchResult:
    """单份合同原文结果。"""

    ok: bool
    content: bytes = b""
    error: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "size": len(self.content), "error": self.error,
                "source": self.source}


@dataclass
class MailResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "message_id": self.message_id, "error": self.error,
                "source": self.source}


def content_hash(data: bytes) -> str:
    """幂等键：内容哈希（与 Mock/自检口径一致，取前 16 位十六进制）。"""
    return hashlib.sha256(data).hexdigest()[:16]


# ============================================================ 契约

class ContractSource(Protocol):
    """合同来源连接器契约（清单 / 取文）。"""

    name: str
    def list_contracts(self) -> ListResult: ...
    def fetch_contract(self, contract_id: str) -> FetchResult: ...


class MailGateway(Protocol):
    """邮件网关连接器契约。"""

    name: str
    def send_mail(self, to: str, subject: str, body: str,
                  idempotency_key: str = "") -> MailResult: ...


# ============================================================ 通用 HTTP 工具

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _http_json(url: str, method: str = "GET", data: bytes | None = None,
               headers: dict | None = None, timeout: float = 15.0,
               verify_tls: bool = True) -> tuple[int, Any, str]:
    """发一次 HTTP 请求，返回 (状态码, 解析后的 JSON 或 None, 错误串)。不抛异常。"""
    import ssl
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    ctx = None
    if url.lower().startswith("https") and not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body), ""
            except Exception:
                return r.status, None, f"响应不是 JSON（前 120 字符：{body[:120]!r}）"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return exc.code, None, f"HTTP {exc.code}：{body}"
    except urllib.error.URLError as exc:
        return 0, None, f"不可达：{exc.reason}"
    except Exception as exc:                       # 含超时
        return 0, None, f"{type(exc).__name__}: {exc}"


# ============================================================ 实现 1：Mock

class MockContractSource:
    """Mock OA（本项目 `mock/mock_office.py`）。支持故障注入，用于验证降级路径。"""

    name = "mock"

    def __init__(self, base: str = "", fault: str = "", timeout: float = 15.0):
        self.base = (base or _env("MOCK_URL", "http://127.0.0.1:8780")).rstrip("/")
        self.fault = fault
        self.timeout = timeout

    def _headers(self, fault: str = "") -> dict:
        f = fault or self.fault
        return {"X-Mock-Mode": f} if f else {}

    def list_contracts(self) -> ListResult:
        code, data, err = _http_json(f"{self.base}/oa/contracts",
                                     headers=self._headers(), timeout=self.timeout)
        if err or not isinstance(data, dict):
            return ListResult(False, degraded=True, source=self.name,
                              error=err or "响应结构异常")
        if "items" not in data:
            # 结构校验：字段改名/换系统必须显式失败，不能被当成"没有新合同"
            return ListResult(False, degraded=True, source=self.name,
                              error=f"收件目录返回结构异常：{sorted(data.keys())}")
        return ListResult(True, items=list(data.get("items") or []), source=self.name)

    def fetch_contract(self, contract_id: str) -> FetchResult:
        # 【第二次审查修正·P2】同样把 `/` 转义掉，避免 contract_id 里的 `../` 打到别的路径
        url = f"{self.base}/oa/contracts/{urllib.parse.quote(contract_id, safe='')}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return FetchResult(True, content=r.read(), source=self.name)
        except urllib.error.HTTPError as exc:
            return FetchResult(False, source=self.name,
                               error=f"HTTP {exc.code}：下载失败（{contract_id}）")
        except Exception as exc:
            return FetchResult(False, source=self.name,
                               error=f"{type(exc).__name__}: {exc}")


class MockMailGateway:
    name = "mock"

    def __init__(self, base: str = "", fault: str = "", timeout: float = 15.0):
        self.base = (base or _env("MOCK_URL", "http://127.0.0.1:8780")).rstrip("/")
        self.fault = fault
        self.timeout = timeout

    def send_mail(self, to: str, subject: str, body: str,
                  idempotency_key: str = "") -> MailResult:
        payload = json.dumps({"to": to, "subject": subject, "body": body,
                              "idempotency_key": idempotency_key},
                             ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.fault:
            headers["X-Mock-Mode"] = self.fault
        code, data, err = _http_json(f"{self.base}/mail/send", method="POST", data=payload,
                                     headers=headers, timeout=self.timeout)
        if err or not isinstance(data, dict) or not data.get("ok"):
            return MailResult(False, source=self.name, error=err or str(data)[:200])
        return MailResult(True, message_id=str(data.get("message_id") or ""), source=self.name)


# ============================================================ 实现 2：共享盘/本地目录

class DirectoryContractSource:
    """共享盘 / 本地收件目录（企业里最常见的形态）。

    目录约定（把这份约定给 OA 管理员即可）：
        <CONTRACT_DIR>/
            合同编号.ext            原文（.pdf/.docx/.doc/.txt/.md）
            合同编号.ext.meta.json  可选元数据：{"contract_id","uploaded_at","source"}
    未提供元数据时：合同编号取文件名（去扩展名），时间取文件 mtime，大小取文件大小。
    幂等键统一为文件内容哈希（不依赖 OA 是否提供）。
    """

    name = "dir"

    SUFFIXES = (".pdf", ".docx", ".doc", ".txt", ".md")

    def __init__(self, root: str = "", recursive: bool = False):
        self.root = Path(root or _env("CONTRACT_DIR", "")).expanduser()
        self.recursive = recursive or _env_bool("CONTRACT_DIR_RECURSIVE", False)
        # 【第二次审查修正】目录索引缓存：原来 `fetch_contract` 每次都重新遍历目录并逐个
        # 读 `.meta.json`（共享盘上每个文件一次网络往返），一批 200 份合同会读上千次。
        # 现在按"目录 mtime + 文件数"做键缓存 `contract_id -> 路径`，目录一变即失效。
        self._index: dict[str, Path] = {}
        self._index_key: tuple = ()

    # ---- 内部 ----
    def _dir_signature(self) -> tuple:
        try:
            st = self.root.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return ()

    def _build_index(self) -> dict[str, Path]:
        key = self._dir_signature()
        if key and key == self._index_key and self._index:
            return self._index
        idx: dict[str, Path] = {}
        for p in self._iter_files():
            meta = self._read_meta(p)
            cid = str(meta.get("contract_id") or p.stem)
            idx.setdefault(cid, p)          # 同编号取先出现的那份，行为可预期
            idx.setdefault(p.stem, p)       # 文件名兜底（可无元数据）
        self._index, self._index_key = idx, key
        return idx

    def _iter_files(self):
        if not self.root.is_dir():
            return []
        pat = "**/*" if self.recursive else "*"
        return sorted(p for p in self.root.glob(pat)
                      if p.is_file() and p.suffix.lower() in self.SUFFIXES
                      and not p.name.startswith("."))

    def _orphan_meta_paths(self) -> list[Path]:
        """只有元数据、没有原文的条目（企业共享盘常见脏数据）。

        必须**能被列出来**：否则调用方看到的是"少了一份合同"，
        而不是"这份合同取不到原文需要人工处理"——后者才是可运维的行为。
        """
        if not self.root.is_dir():
            return []
        pat = "**/*" if self.recursive else "*"
        out = []
        for mp in sorted(self.root.glob(pat)):
            if not mp.is_file() or not mp.name.endswith(".meta.json"):
                continue
            src_name = mp.name[: -len(".meta.json")]
            if (mp.parent / src_name).is_file():
                continue
            out.append(mp)
        return out

    @staticmethod
    def _read_meta(path: Path) -> dict:
        mp = path.with_name(path.name + ".meta.json")
        if not mp.is_file():
            return {}
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            return {}      # 元数据坏掉不影响取原文（幂等键来自内容哈希）

    # ---- 契约 ----
    def list_contracts(self) -> ListResult:
        if not self.root.exists():
            return ListResult(False, degraded=True, source=self.name,
                              error=f"收件目录不存在：{self.root}（请检查 CONTRACT_DIR / 共享盘挂载）")
        if not self.root.is_dir():
            return ListResult(False, degraded=True, source=self.name,
                              error=f"CONTRACT_DIR 不是目录：{self.root}")
        items = []
        for p in self._iter_files():
            meta = self._read_meta(p)
            try:
                st = p.stat()
            except OSError as exc:
                # 单条不可读不影响整批（与容错矩阵一致）
                items.append({"contract_id": p.stem, "error": f"无法读取：{exc}"})
                continue
            items.append({
                "contract_id": str(meta.get("contract_id") or p.stem),
                "size": int(meta.get("size") or st.st_size),
                "uploaded_at": str(meta.get("uploaded_at")
                                   or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))),
                "content_hash": "",           # 目录模式按需计算（见 fetch_contract）
                "_path": str(p),
                "source": str(meta.get("source") or self.name),
            })
        # 只有元数据、没有原文 → 作为"脏数据条目"列出（上层会尝试取文并计入 failed）
        for mp in self._orphan_meta_paths():
            # 【第二次审查修正·P2】原来切的是 `mp.name[: -len(".meta.json.meta.json")]`
            # （多减一个后缀，实测切出空串），导致缺 contract_id 的脏数据以**空编号**上报、
            # 运维丢失文件名线索。正确做法是先去 ".meta.json" 得到原文文件名。
            src_name = mp.name[: -len(".meta.json")]
            meta = self._read_meta(Path(str(mp)[: -len(".meta.json")]))
            items.append({
                "contract_id": str(meta.get("contract_id") or Path(src_name).stem),
                "size": 0,
                "uploaded_at": str(meta.get("uploaded_at") or ""),
                "content_hash": "",
                "_path": "",
                "source": str(meta.get("source") or self.name),
                "dirty": True,
                "dirty_reason": "只有元数据 (.meta.json) 没有原文，需人工核对共享盘同步",
            })
        return ListResult(True, items=items, source=self.name)

    def fetch_contract(self, contract_id: str) -> FetchResult:
        # 走索引（O(1)）；索引按目录签名失效，目录一变自动重建
        hit = self._build_index().get(contract_id)
        if hit is not None:
            return self._read(hit)
        return FetchResult(False, source=self.name, error=f"合同不存在：{contract_id}")

    def _read(self, p: Path) -> FetchResult:
        try:
            return FetchResult(True, content=p.read_bytes(), source=self.name)
        except OSError as exc:
            return FetchResult(False, source=self.name, error=f"读取失败：{exc}")


# ============================================================ 实现 3：HTTP/JSON 接口

class HttpContractSource:
    """通用 HTTP/JSON 合同来源（OA / 合同库 REST 接口）。

    企业接口的字段名千差万别，因此**全部可映射**（默认值即本项目 Mock 的契约）：
        OA_LIST_PATH        清单接口路径，默认 /oa/contracts
        OA_FETCH_PATH       下载接口路径，默认 /oa/contracts/{contract_id}
        OA_LIST_ITEMS_KEY   清单数组字段名，默认 items
        OA_ID_FIELD / OA_HASH_FIELD / OA_TIME_FIELD / OA_SIZE_FIELD
        OA_AUTH_HEADER/OA_AUTH_TOKEN   鉴权头（如 Authorization / Bearer xxx）

    对**结构不符**的处理：清单里缺少 `contract_id` 的条目会被标 `error` 并在上层计为单条失败，
    整个响应不是对象或数组字段不是数组时整体判 `degraded`（宁可报错，不可漏审）。
    """

    name = "http"

    def __init__(self, base: str = "", list_path: str = "", fetch_path: str = "",
                 headers: dict | None = None, timeout: float = 0.0, verify_tls: bool = True):
        self.base = (base or _env("OA_BASE_URL", "")).rstrip("/")
        self.list_path = list_path or _env("OA_LIST_PATH", "/oa/contracts")
        self.fetch_path = fetch_path or _env("OA_FETCH_PATH", "/oa/contracts/{contract_id}")
        self.timeout = timeout or _env_float("OA_TIMEOUT_S", 15.0)
        self.verify_tls = verify_tls if verify_tls is not None else _env_bool("OA_VERIFY_TLS", True)
        self.headers = dict(headers or {})
        ah, at = _env("OA_AUTH_HEADER"), _env("OA_AUTH_TOKEN")
        if ah and at:
            self.headers.setdefault(ah, at)
        self.items_key = _env("OA_LIST_ITEMS_KEY", "items")
        self.f_id = _env("OA_ID_FIELD", "contract_id")
        self.f_hash = _env("OA_HASH_FIELD", "content_hash")
        self.f_time = _env("OA_TIME_FIELD", "uploaded_at")
        self.f_size = _env("OA_SIZE_FIELD", "size")

    def list_contracts(self) -> ListResult:
        if not self.base:
            return ListResult(False, degraded=True, source=self.name,
                              error="未配置 OA_BASE_URL（CONTRACT_SOURCE=http 时必须提供）")
        code, data, err = _http_json(f"{self.base}{self.list_path}",
                                     headers=self.headers, timeout=self.timeout,
                                     verify_tls=self.verify_tls)
        if err or not isinstance(data, dict):
            return ListResult(False, degraded=True, source=self.name,
                              error=err or f"清单响应结构异常（{type(data).__name__}）")
        # 企业接口常常把数组包在 data/result 里：items_key 支持点号路径（如 data.records）
        node: Any = data
        for part in [p for p in str(self.items_key).split(".") if p]:
            if not isinstance(node, dict) or part not in node:
                return ListResult(False, degraded=True, source=self.name,
                                  error=f"清单响应缺少路径 {self.items_key!r}（顶层字段：{sorted(data.keys())}）")
            node = node[part]
        raw_items = node
        if not isinstance(raw_items, list):
            return ListResult(False, degraded=True, source=self.name,
                              error=f"清单字段 {self.items_key!r} 不是数组（实际 {type(raw_items).__name__}）")
        items = []
        for it in raw_items:
            if not isinstance(it, dict) or not it.get(self.f_id):
                items.append({"contract_id": "", "error": f"条目缺少 {self.f_id} 字段：{str(it)[:120]}"})
                continue
            items.append({
                "contract_id": str(it[self.f_id]),
                "size": it.get(self.f_size) or 0,
                "uploaded_at": str(it.get(self.f_time) or ""),
                "content_hash": str(it.get(self.f_hash) or ""),
                "source": self.name,
            })
        return ListResult(True, items=items, source=self.name)

    def fetch_contract(self, contract_id: str) -> FetchResult:
        if not self.base:
            return FetchResult(False, source=self.name, error="未配置 OA_BASE_URL")
        # 【第二次审查修正·P2】`urlsafe` 而不是 `quote`：`quote` 默认不转义 `/`，
        # 上游给的 contract_id 若含 `../` 就会被拼进取文 URL，带着 OA_AUTH_TOKEN
        # 打到 OA 主机上的任意路径（越界取文）。`quote(safe="")` 把 `/` 也转义掉。
        path = self.fetch_path.replace("{contract_id}",
                                       urllib.parse.quote(contract_id, safe=""))
        url = path if path.lower().startswith("http") else f"{self.base}{path}"
        req = urllib.request.Request(url, headers=self.headers)
        import ssl
        ctx = None
        if url.lower().startswith("https") and not self.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                return FetchResult(True, content=r.read(), source=self.name)
        except urllib.error.HTTPError as exc:
            return FetchResult(False, source=self.name,
                               error=f"HTTP {exc.code}：下载失败（{contract_id}）")
        except Exception as exc:
            return FetchResult(False, source=self.name,
                               error=f"{type(exc).__name__}: {exc}")


class HttpMailGateway:
    """邮件网关 HTTP 接口（企业常见：内部邮件网关/中台提供 /mail/send）。"""

    name = "http"

    def __init__(self, url: str = "", headers: dict | None = None, timeout: float = 0.0):
        self.url = url or _env("MAIL_HTTP_URL", "")
        self.timeout = timeout or _env_float("OA_TIMEOUT_S", 15.0)
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        ah, at = _env("MAIL_AUTH_HEADER"), _env("MAIL_AUTH_TOKEN")
        if ah and at:
            self.headers.setdefault(ah, at)

    def send_mail(self, to: str, subject: str, body: str,
                  idempotency_key: str = "") -> MailResult:
        if not self.url:
            return MailResult(False, source=self.name,
                              error="未配置 MAIL_HTTP_URL（MAIL_GATEWAY=http 时必须提供）")
        payload = json.dumps({"to": to, "subject": subject, "body": body,
                              "idempotency_key": idempotency_key},
                             ensure_ascii=False).encode("utf-8")
        code, data, err = _http_json(self.url, method="POST", data=payload,
                                     headers=self.headers, timeout=self.timeout)
        if err:
            return MailResult(False, source=self.name, error=err)
        mid = ""
        if isinstance(data, dict):
            mid = str(data.get("message_id") or data.get("id") or "")
        return MailResult(True, message_id=mid, source=self.name)


class SmtpMailGateway:
    """SMTP 直发（企业邮箱最常见）。

    只做最基本的发信与鉴权；真实环境请按贵司邮件网关要求扩展（如 OAuth2、内部中继白名单）。
    """

    name = "smtp"

    def __init__(self, host: str = "", port: int = 0, user: str = "", password: str = "",
                 sender: str = "", starttls: bool | None = None, timeout: float = 0.0):
        self.host = host or _env("SMTP_HOST", "")
        self.port = int(port or _env("SMTP_PORT", "587") or 587)
        self.user = user or _env("SMTP_USER", "")
        self.password = password or _env("SMTP_PASSWORD", "")
        self.sender = sender or _env("SMTP_FROM", self.user or "compliance@example.com")
        self.starttls = _env_bool("SMTP_STARTTLS", True) if starttls is None else starttls
        # 【第二次审查修正】原来靠 `hasattr(self, "timeout")` 取值 —— 该属性从未定义，
        # 于是永远走 else 的硬编码 20 秒（可工作的死代码，但配置不生效、意图不清）。
        # 现在统一读 SMTP_TIMEOUT_S / OA_TIMEOUT_S，与其它连接器一致。
        self.timeout = timeout or _env_float("SMTP_TIMEOUT_S", _env_float("OA_TIMEOUT_S", 20.0))

    def send_mail(self, to: str, subject: str, body: str,
                  idempotency_key: str = "") -> MailResult:
        if not self.host:
            return MailResult(False, source=self.name,
                              error="未配置 SMTP_HOST（MAIL_GATEWAY=smtp 时必须提供）")
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        if idempotency_key:
            msg["X-Idempotency-Key"] = idempotency_key
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                if self.starttls:
                    s.starttls()
                if self.user:
                    s.login(self.user, self.password)
                s.send_message(msg)
            return MailResult(True, message_id=msg.get("Message-ID") or "", source=self.name)
        except Exception as exc:
            return MailResult(False, source=self.name, error=f"{type(exc).__name__}: {exc}")


class NullMailGateway:
    """显式关闭通知（演练/离线环境）。"""

    name = "none"

    def send_mail(self, to: str, subject: str, body: str,
                  idempotency_key: str = "") -> MailResult:
        return MailResult(True, message_id="", source=self.name)


# ============================================================ 工厂

def _mock_kw(kw: dict, target) -> dict:
    """把调用方传参归一化成目标连接器接受的形参。

    【第二次审查修正·P1】原来把 `mock_base` 无条件改名成 `base`：
    `DirectoryContractSource` / `HttpMailGateway` / `SmtpMailGateway` 都不接受 `base`，
    于是 `ProactiveWorker()` 在 `CONTRACT_SOURCE=dir`、`MAIL_GATEWAY=http|smtp`
    （也就是**企业唯一真实形态**）下构造即 TypeError，主动巡检/通知彻底不可用。
    现在按目标类的真实形参过滤：认识的才传，不认识的一律丢掉（并保留 mock_base→base 映射）。
    """
    import inspect
    out = dict(kw)
    if "mock_base" in out and "base" not in out:
        out["base"] = out.pop("mock_base")
    try:
        params = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return out
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if accepts_kwargs:
        return out
    return {k: v for k, v in out.items() if k in params}


def build_contract_source(kind: str = "", **kw) -> ContractSource:
    """按配置构建合同来源连接器（未知类型**报错**而不是静默回退到 mock）。"""
    k = (kind or _env("CONTRACT_SOURCE", "mock")).lower()
    if k == "mock":
        return MockContractSource(**_mock_kw(kw, MockContractSource))
    if k == "dir":
        return DirectoryContractSource(**_mock_kw(kw, DirectoryContractSource))
    if k == "http":
        return HttpContractSource(**_mock_kw(kw, HttpContractSource))
    raise ValueError(f"未知 CONTRACT_SOURCE：{k!r}（可选 mock|dir|http）")


def build_mail_gateway(kind: str = "", **kw) -> MailGateway:
    k = (kind or _env("MAIL_GATEWAY", "mock")).lower()
    if k == "mock":
        return MockMailGateway(**_mock_kw(kw, MockMailGateway))
    if k == "http":
        return HttpMailGateway(**_mock_kw(kw, HttpMailGateway))
    if k == "smtp":
        return SmtpMailGateway(**_mock_kw(kw, SmtpMailGateway))
    if k in ("none", "off", "disable"):
        return NullMailGateway()
    raise ValueError(f"未知 MAIL_GATEWAY：{k!r}（可选 mock|http|smtp|none）")


def describe() -> dict:
    """当前连接器配置（供 /api/v1/diagnostics 与自检展示，不泄露密钥明文）。"""
    return {
        "contract_source": _env("CONTRACT_SOURCE", "mock").lower(),
        "contract_dir": _env("CONTRACT_DIR") or None,
        "oa_base_url": _env("OA_BASE_URL") or None,
        "oa_auth_configured": bool(_env("OA_AUTH_TOKEN")),
        "mock_url": _env("MOCK_URL", "http://127.0.0.1:8780"),
        "mail_gateway": _env("MAIL_GATEWAY", "mock").lower(),
        "mail_http_configured": bool(_env("MAIL_HTTP_URL")),
        "smtp_host": _env("SMTP_HOST") or None,
        "smtp_auth_configured": bool(_env("SMTP_PASSWORD")),
    }
