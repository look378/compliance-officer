#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
可观测性：结构化日志 + 请求 trace_id + 实时日志流

为什么需要（方案设计 §3.5）：
    合规系统出了问题必须能回答"哪一次请求、哪个环节、什么时间、谁触发的"。
    因此：每个 HTTP 请求生成 trace_id 并回写响应头；日志按 JSON 行落盘；
    工作台可实时订阅日志流，无需登录服务器看文件。

对外接口：
    log_event(level, event, trace_id, **fields)   写一条结构化日志
    new_trace_id()                                生成 trace_id
    tail(lines)                                   读取最近日志
    subscribe()                                   增量读取（供 SSE）
    set_context / get_context                     请求内 trace 上下文

【审查修正 P2】trace 上下文原来用 `threading.local()`：
    FastAPI 的 async 中间件在事件循环线程里 set_context，而 `def` 端点在线程池的
    工作线程里执行 —— threading.local 不跨线程，实测端点内读到的 trace_id 恒为空，
    于是"哪一次请求"这个字段在最需要它的地方（登录、改密、发起审查、Agent 调用）全部丢失。
    改用 `contextvars`（随 asyncio 任务与线程池调用复制传播）；并保留一份 threading.local
    兜底，供非异步上下文的调用方（自检脚本）显式使用。
"""

import contextvars
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / ".runtime" / "logs"
APP_LOG = LOG_DIR / "app.jsonl"

# 日志轮转参数：单文件上限（字节）与保留的历史份数
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(8 * 1024 * 1024)))
LOG_BACKUPS = int(os.environ.get("LOG_BACKUPS", "5"))

_write_lock = threading.Lock()
_ctx = threading.local()                       # 兼容非 async 调用方
_trace_var: contextvars.ContextVar = contextvars.ContextVar("compliance_trace", default=None)


def new_trace_id() -> str:
    return f"tr_{int(time.time() * 1000):x}_{os.urandom(3).hex()}"


def set_context(trace_id: str, **kw) -> None:
    """设置当前请求的 trace 上下文（contextvars 为主，threading.local 兼容）。"""
    _trace_var.set({"trace_id": trace_id, **kw})
    _ctx.trace_id = trace_id
    _ctx.extra = kw


def get_context() -> dict:
    cur = _trace_var.get()
    if cur:
        return dict(cur)
    return {"trace_id": getattr(_ctx, "trace_id", ""), **getattr(_ctx, "extra", {})}


def reset_context() -> None:
    """清空上下文（请求结束或测试隔离用）。"""
    _trace_var.set(None)
    _ctx.trace_id = ""
    _ctx.extra = {}


def _rotate_if_needed() -> None:
    """按大小轮转：app.jsonl -> app.jsonl.1 -> ... -> app.jsonl.N（N 份上限）。

    【审查修正 P2】原来日志文件无任何大小/天数上限，长期运行会无限增长。
    """
    try:
        if not APP_LOG.exists() or APP_LOG.stat().st_size < LOG_MAX_BYTES:
            return
        _shift_backups()
        os.replace(APP_LOG, APP_LOG.with_suffix(APP_LOG.suffix + ".1"))
    except OSError:
        pass          # 轮转失败不能影响写日志


def log_event(level: str, event: str, **fields) -> dict:
    """写一条结构化日志（JSON 行）。level: debug|info|warn|error"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "level": level,
        "event": event,
        **get_context(),
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _write_lock:
        _rotate_if_needed()
        with open(APP_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    if os.environ.get("LOG_MODE", "console").lower() != "json":
        print(f"[{record['ts']}] {level.upper():5s} {event} "
              f"{json.dumps({k: v for k, v in fields.items()}, ensure_ascii=False, default=str)[:200]}")
    return record


def _parse_lines(lines: list[str]) -> list[dict]:
    out = []
    for r in lines:
        r = r.strip()
        if not r:
            continue
        try:
            out.append(json.loads(r))
        except Exception:
            out.append({"ts": "", "level": "raw", "event": r[:300]})
    return out


def tail(lines: int = 200) -> list[dict]:
    """读取最近 N 条日志（JSON 解析失败的坏行降级为 raw）。

    【审查修正 P2】原实现 `fh.readlines()[-lines:]` 会把**整个文件**读进内存
    （网关每次 /api/v1/logs 都如此，日志越大越吃内存）。现在用 deque 有界缓冲逐行流式读取。
    """
    if not APP_LOG.exists():
        return []
    try:
        buf: deque[str] = deque(maxlen=max(1, int(lines)))
        with open(APP_LOG, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                buf.append(line)
        return _parse_lines(list(buf))
    except Exception:
        return []


def subscribe(poll: float = 1.0, idle_limit: int = 120):
    """增量生成日志（供 SSE 使用）。idle_limit 为空闲轮数上限，避免连接长期占用。

    【审查修正 P2】原实现有两个缺陷：
      1) 文本模式下用 `stat().st_size`（字节）当 `seek()` 偏移：并发写入时可能读到
         **未写完的半行**，json 解析失败后按 raw 推送，而 `pos = fh.tell()` 已越过该半行，
         剩余部分会在下一轮被当成另一条 raw 事件推送（重复/撕裂）。
      2) 没有"保留未完成行"的概念，写法再变也修不好。
    现在改为**二进制读 + 只按完整行前进**：行尾没有换行符的部分留在缓冲区，
    下一轮补齐后再解析，游标永不越过半行。
    """
    pos = APP_LOG.stat().st_size if APP_LOG.exists() else 0
    pending = b""
    idle = 0
    while idle < idle_limit:
        if not APP_LOG.exists():
            time.sleep(poll)
            idle += 1
            continue
        size = APP_LOG.stat().st_size
        if size < pos:          # 日志被轮转/清空
            pos, pending = 0, b""
        if size > pos:
            with open(APP_LOG, "rb") as fh:
                fh.seek(pos)
                chunk = fh.read(size - pos)
            pos = size
            pending += chunk
            *complete, pending = pending.split(b"\n")
            if complete:
                text = b"\n".join(complete).decode("utf-8", errors="replace")
                for rec in _parse_lines(text.split("\n")):
                    yield rec
            idle = 0
        else:
            idle += 1
            time.sleep(poll)
    yield {"level": "info", "event": "stream.idle_timeout"}


def log_stats() -> dict:
    """日志文件统计（供诊断端点展示轮转是否生效）。"""
    files = []
    for p in sorted(LOG_DIR.glob("app.jsonl*")):
        try:
            files.append({"name": p.name, "bytes": p.stat().st_size})
        except OSError:
            continue
    return {"file": str(APP_LOG), "max_bytes": LOG_MAX_BYTES, "backups": LOG_BACKUPS,
            "files": files, "total_bytes": sum(f["bytes"] for f in files)}


def rotate_now() -> dict:
    """强制轮转（自检 / 运维 maintenance 调用）。

    【第二次审查修正·P2】原来直接把日志 `os.replace` 成 `.1`：
      ① 不做位移 → 覆盖掉上一份 `.1` 备份（手工轮转一次就丢一代历史）；
      ② Windows 上若日志正被读取/写入方持有句柄，`os.replace` 抛 PermissionError 且无人捕获
         → `/api/v1/maintenance` 直接 500。
    现在复用与自动轮转同一套位移逻辑，并吞掉"被占用"这类临时失败（返回是否成功 + 原因）。
    """
    with _write_lock:
        try:
            if not APP_LOG.exists() or APP_LOG.stat().st_size == 0:
                # 没有内容可轮转：语义上不是失败（调用方通常是定时/手动运维触发）
                return {**log_stats(), "rotated": True, "skipped": "日志为空或尚不存在，无需轮转"}
            _shift_backups()
            os.replace(APP_LOG, APP_LOG.with_suffix(APP_LOG.suffix + ".1"))
            return {**log_stats(), "rotated": True}
        except OSError as exc:
            # 被其它进程占用（或权限不足）：不抛异常，如实报告，让维护接口仍返回 200
            return {**log_stats(), "rotated": False,
                    "reason": f"{type(exc).__name__}: {exc}"}


def _shift_backups() -> None:
    """把 app.jsonl.N 依次后移一位，给新的 .1 让位（最老的一份丢弃）。"""
    for i in range(LOG_BACKUPS - 1, 0, -1):
        src = APP_LOG.with_suffix(APP_LOG.suffix + f".{i}")
        dst = APP_LOG.with_suffix(APP_LOG.suffix + f".{i + 1}")
        if src.exists():
            try:
                os.replace(src, dst)
            except OSError:
                pass
