#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Mock 外部系统（FastAPI）：企业 OA 文件服务 + 企业邮箱 + 统一故障注入

为什么需要（方案设计 §7 容错矩阵、任务书 §5）：
    真实 OA/邮箱不可得，但**容错能力必须可验证**。本服务把"外部系统会怎么坏"
    固化成可复现的开关，让降级路径能被自动化测试覆盖，而不是只写在文档里。

故障注入（请求头 X-Mock-Mode）：
    timeout   模拟外部系统无响应（服务端休眠 MOCK_TIMEOUT_SLEEP 秒，默认 35s > 客户端超时）
    error     返回 5xx
    bad_data  返回结构错误但 HTTP 200（最阴险的一类故障：调用方以为成功）
    empty     返回结构合法但内容为空
    duplicate 同一请求被重复投递（验证幂等键）

所有外部依赖点均标注 TODO[生产对接]。

启动：
    .runtime\\venv\\Scripts\\python.exe mock_office.py --port 8780
"""

import argparse
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.path.join(PROJECT_ROOT, ".runtime", "mock-oa")
MAILBOX = os.path.join(STORE_DIR, "_mailbox.jsonl")
MOCK_TIMEOUT_SLEEP = 35.0          # 大于客户端常见超时（框架 MCP/HTTP 默认 30~60s）
DEFAULT_FAULT = ""

app = FastAPI(title="Mock 企业 OA 与邮箱", version="1.0.0")
_lock = threading.Lock()


# ------------------------------------------------------------------ 工具

def _fault(mode: str) -> str:
    return (mode or DEFAULT_FAULT or "").strip().lower()


def _guard(mode: str, endpoint: str) -> JSONResponse | None:
    """统一故障注入前置处理。返回非 None 表示本次请求已被故障短路。"""
    m = _fault(mode)
    if not m:
        return None
    if m == "timeout":
        # TODO[生产对接]: 真实系统中这里是网络不可达/服务无响应，需要客户端超时与重试
        time.sleep(MOCK_TIMEOUT_SLEEP)
        return JSONResponse({"error": "timeout(模拟)", "endpoint": endpoint}, status_code=504)
    if m == "error":
        # TODO[生产对接]: 真实系统 5xx
        return JSONResponse({"error": "internal_error(模拟)", "endpoint": endpoint}, status_code=500)
    if m == "bad_data":
        # TODO[生产对接]: 真实系统字段改名/类型变化，调用方必须做结构校验
        return JSONResponse({"unexpected": ["这不是约定的结构"], "endpoint": endpoint},
                            status_code=200)
    if m == "empty":
        return None      # 由各端点自行返回空集合（结构合法、内容为空）
    if m == "duplicate":
        return None      # 由各端点自行重复一次副作用
    return None


def _ensure_dir() -> None:
    os.makedirs(STORE_DIR, exist_ok=True)


def _meta_path(contract_id: str) -> str:
    return os.path.join(STORE_DIR, f"{contract_id}.meta.json")


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _load_meta(contract_id: str) -> dict | None:
    p = _meta_path(contract_id)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_meta(meta: dict) -> None:
    with open(_meta_path(meta["contract_id"]), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)


def _append_mail(item: dict) -> None:
    _ensure_dir()
    with open(MAILBOX, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ OA 文件服务

@app.get("/health")
def health() -> dict[str, Any]:
    _ensure_dir()
    files = [f for f in os.listdir(STORE_DIR) if f.endswith(".meta.json")]
    return {"status": "running", "service": "mock-oa+mailbox", "version": "1.0.0",
            "contracts": len(files), "fault_default": DEFAULT_FAULT or None,
            "timeout_sleep_seconds": MOCK_TIMEOUT_SLEEP}


@app.post("/oa/contracts")
async def upload_contract(request: Request,
                          x_mock_mode: str = Header(default=""),
                          x_contract_id: str = Header(default="")) -> JSONResponse:
    """上传合同。幂等键 = 内容哈希；重复投递返回同一条记录（duplicate 模式会再写一次）。"""
    fault = _guard(x_mock_mode, "upload_contract")
    if fault is not None:
        return fault
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="空请求体")

    content_hash = _content_hash(body)
    contract_id = x_contract_id or f"CT-{datetime.now().strftime('%Y%m%d')}-{content_hash[:6]}"
    _ensure_dir()
    existing = [f for f in os.listdir(STORE_DIR)
                if f.endswith(".meta.json") and _load_meta(f[:-10]) and
                _load_meta(f[:-10]).get("content_hash") == content_hash]
    if existing:
        meta = _load_meta(existing[0][:-10])
        return JSONResponse({"contract_id": meta["contract_id"], "deduplicated": True,
                             "content_hash": content_hash, "size": meta["size"]})

    with open(os.path.join(STORE_DIR, f"{contract_id}.bin"), "wb") as fh:
        fh.write(body)
    meta = {"contract_id": contract_id, "content_hash": content_hash, "size": len(body),
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "mock-oa"}
    _save_meta(meta)

    if _fault(x_mock_mode) == "duplicate":
        # 模拟外部系统重复投递：第二条记录同内容不同 ID，用于验证幂等键与去重
        dup_id = f"{contract_id}-DUP"
        meta2 = dict(meta, contract_id=dup_id)
        _save_meta(meta2)

    return JSONResponse({"contract_id": contract_id, "deduplicated": False,
                         "content_hash": content_hash, "size": len(body)})


@app.get("/oa/contracts")
def list_contracts(x_mock_mode: str = Header(default=""),
                   since: str = Header(default="")) -> JSONResponse:
    """列出合同（收件目录扫描用）。empty 模式返回空列表。"""
    fault = _guard(x_mock_mode, "list_contracts")
    if fault is not None:
        return fault
    _ensure_dir()
    items = []
    for name in sorted(os.listdir(STORE_DIR)):
        if not name.endswith(".meta.json"):
            continue
        meta = _load_meta(name[:-10])
        if not meta:
            continue
        if since and meta.get("uploaded_at", "") < since:
            continue
        items.append({k: meta[k] for k in ("contract_id", "size", "uploaded_at", "content_hash")})
    if _fault(x_mock_mode) == "empty":
        items = []
    return JSONResponse({"count": len(items), "items": items,
                         "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@app.get("/oa/contracts/{contract_id}")
def download_contract(contract_id: str, x_mock_mode: str = Header(default="")):
    """下载合同原文。"""
    fault = _guard(x_mock_mode, "download_contract")
    if fault is not None:
        return fault
    meta = _load_meta(contract_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"合同不存在：{contract_id}")
    path = os.path.join(STORE_DIR, f"{contract_id}.bin")
    if not os.path.exists(path):
        # TODO[生产对接]: 真实系统的元数据与文件可能不一致（脏数据），调用方需容错
        raise HTTPException(status_code=410, detail="元数据存在但文件缺失（模拟脏数据）")
    return FileResponse(path, media_type="application/octet-stream",
                        filename=f"{contract_id}.bin")


@app.get("/oa/contracts/{contract_id}/meta")
def contract_meta(contract_id: str, x_mock_mode: str = Header(default="")) -> JSONResponse:
    fault = _guard(x_mock_mode, "contract_meta")
    if fault is not None:
        return fault
    meta = _load_meta(contract_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"合同不存在：{contract_id}")
    return JSONResponse(meta)


# ------------------------------------------------------------------ 企业邮箱

@app.post("/mail/send")
def send_mail(payload: dict = Body(...), x_mock_mode: str = Header(default="")) -> JSONResponse:
    """发送邮件（模拟 SMTP 发信）。duplicate 模式会写两封，用于验证去重/幂等。"""
    fault = _guard(x_mock_mode, "send_mail")
    if fault is not None:
        return fault
    for field in ("to", "subject"):
        if not payload.get(field):
            raise HTTPException(status_code=400, detail=f"缺少字段：{field}")
    item = {
        "message_id": f"msg_{uuid.uuid4().hex[:10]}",
        "to": payload["to"], "subject": payload["subject"],
        "body": (payload.get("body") or "")[:2000],
        "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "idempotency_key": payload.get("idempotency_key", ""),
    }
    _append_mail(item)
    if _fault(x_mock_mode) == "duplicate":
        item2 = dict(item, message_id=f"msg_{uuid.uuid4().hex[:10]}", duplicated=True)
        _append_mail(item2)
    return JSONResponse({"ok": True, "message_id": item["message_id"],
                         "to": item["to"], "sent_at": item["sent_at"]})


@app.get("/mail/inbox")
def inbox(limit: int = 50, x_mock_mode: str = Header(default="")) -> JSONResponse:
    """读取已发送邮件（用于渠道验证）。"""
    fault = _guard(x_mock_mode, "inbox")
    if fault is not None:
        return fault
    items = []
    if os.path.exists(MAILBOX):
        with open(MAILBOX, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue      # TODO[生产对接]: 真实邮箱不会出现半行 JSON，此处仅容错
    items = items[-max(1, limit):]
    if _fault(x_mock_mode) == "empty":
        items = []
    return JSONResponse({"count": len(items), "items": items})


@app.post("/mail/inject")
def inject_inbound(payload: dict = Body(...),
                   x_mock_mode: str = Header(default="")) -> JSONResponse:
    """注入一封"收到的"邮件（模拟业务方回信），用于驱动人工审批场景。"""
    fault = _guard(x_mock_mode, "inject_inbound")
    if fault is not None:
        return fault
    item = {"message_id": f"in_{uuid.uuid4().hex[:10]}", "direction": "inbound",
            "from": payload.get("from", "external@example.com"),
            "subject": payload.get("subject", "(无主题)"),
            "body": (payload.get("body") or "")[:2000],
            "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _append_mail(item)
    return JSONResponse({"ok": True, "message_id": item["message_id"]})


# ------------------------------------------------------------------ 运行

def main() -> int:
    global DEFAULT_FAULT, MOCK_TIMEOUT_SLEEP
    parser = argparse.ArgumentParser(description="Mock 企业 OA 与邮箱服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--fault", default=os.environ.get("MOCK_FAULT", ""),
                        help="全局默认故障：timeout|error|bad_data|empty|duplicate")
    parser.add_argument("--timeout-sleep", type=float, default=MOCK_TIMEOUT_SLEEP)
    args = parser.parse_args()

    DEFAULT_FAULT = args.fault
    MOCK_TIMEOUT_SLEEP = args.timeout_sleep

    _ensure_dir()
    import uvicorn
    print(f"[mock] OA+邮箱 监听 http://{args.host}:{args.port}"
          f"（故障注入头 X-Mock-Mode；默认故障={DEFAULT_FAULT or '无'}）")
    print(f"[mock] 数据目录：{STORE_DIR}；timeout 模式休眠 {MOCK_TIMEOUT_SLEEP}s")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
