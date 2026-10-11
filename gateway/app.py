#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
轻量网关（FastAPI）：认证 + RBAC + 待办 + 发起审查 + 指标

定位（方案设计 §3.3）：
    网关**不复制业务状态**，只做三件事：认证与角色校验、把请求转成服务层调用、把待办/指标暴露给界面。
    工作流状态仍以编排器为准（单一真相源），避免"网关与工作流各有一套状态"的双真相源问题。

本文件当前实现：
    POST /api/v1/contracts            提交合同（走两段式：判定 + 编排）
    GET  /api/v1/runs                 列出 run（含风险等级与状态）
    GET  /api/v1/runs/{run_id}        单 run 全景（状态/命中规则/待办/审计链）
    GET  /api/v1/pending              待办列表（按角色过滤）
    POST /api/v1/runs/{run_id}/decide 审批（原子认领，并发只有一个生效）
    POST /api/v1/recover              触发启动补偿（重启后重新通知）
    GET  /api/v1/proposals            规则提案列表
    POST /api/v1/proposals/{id}/review 提案审批（主管）
    GET  /api/v1/metrics              指标
    GET  /health                      健康检查

认证与角色：请求头 `X-Auth-Token` + `X-Role`（JWT 全套在 B 项目已实现，此处按任务书从简）。
SSE 进度推送：见 `/api/v1/runs/{run_id}/events`（轮询审计链形成事件流，不侵入框架输出通道）。
"""

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 运行时框架的工作区（数据库与配置在此）；容器内由 PYTHONPATH 与卷决定
#
# 【实跑容器发现·P0】这里必须先**建出**工作区再 chdir，不能"存在才 chdir"：
#   框架 DAO 用 `Path.cwd()/.lemonclaw/lemonclaw.db` 定位库文件，而本项目自己的
#   `_dbpath`/`runtime_store` 固定用 `.runtime/workspace/.lemonclaw/lemonclaw.db`。
#   容器首次启动时 `.runtime/workspace` 还不存在（数据卷是空的）→ 原来就不 chdir →
#   框架把库写到 `/app/.lemonclaw/`，而 `runtime_store`（run 级占用，决定审批能否进行）
#   按规范路径去工作区找 `workflow_runs` → 找不到 → `claim_run` 恒失败 →
#   **任何审批都返回 409「该审批已被他人处理中」**（已在容器里复现）。
#   这正是 `_dbpath.py` 注释里警告过的"两份库并存"。
_RUNTIME_WORKSPACE = PROJECT_ROOT / ".runtime" / "workspace"
_RUNTIME_WORKSPACE.mkdir(parents=True, exist_ok=True)
os.chdir(_RUNTIME_WORKSPACE)
_FRAMEWORK = Path(os.environ.get("LEMONCLAW_HOME")
                  or (PROJECT_ROOT.parent / "lemonclaw-main"))
if _FRAMEWORK.exists() and str(_FRAMEWORK) not in sys.path:
    sys.path.insert(0, str(_FRAMEWORK))

# ---------------------------------------------------------------------------
# 【启动顺序，务必保持】框架的配置类在**导入时**就把 .env 的值固化成类属性默认值
# （lemonclaw-main/config/config.py 用 os.environ.get 作类属性默认值）。
# 因此必须先 load_dotenv、再导入框架模块；否则表现为：
#   OPENAI_API_KEY 为空 → WorkflowManager 构造抛 OpenAIError → 框架吞掉异常返回 None
#   → 编排器不可用（且 ENABLE_MCP 也会是默认 False，岗位工具全丢）。
# 这里用 dotenv 直接加载，不依赖 service.prod_config（那个模块本身会 import 框架）。
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    # 先把"存在但为空"的关键键清掉：dotenv 在 override=False 时不会覆盖它们，
    # 一个空的 OPENAI_API_KEY= 会挡住后续文件里的真实密钥（实测踩过）。
    for _k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_NAME"):
        if not (os.environ.get(_k) or "").strip():
            os.environ.pop(_k, None)
    for _env in (PROJECT_ROOT / ".runtime" / ".lemonclaw" / ".env",
                 _FRAMEWORK / ".lemonclaw" / ".env"):
        if _env.exists():
            load_dotenv(_env, override=False)
    # 仍为空则用 override=True 再兜一次（以框架侧为准，它是部署脚本实际写入的位置）
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        _fw_env = _FRAMEWORK / ".lemonclaw" / ".env"
        if _fw_env.exists():
            load_dotenv(_fw_env, override=True)
except Exception as _exc:      # 缺 dotenv 不致命，但要在日志里能看到
    print(f"[gateway] 加载 .env 失败：{type(_exc).__name__}: {_exc}")

from fastapi import (Body, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile)  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402

from service import auth as auth_mod  # noqa: E402
from service import observability as obs  # noqa: E402
from service.prod_config import settings  # noqa: E402

ROLES = ("specialist", "supervisor", "admin")
WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="合规官「小合」网关", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    s = settings()
    problems = check_auth_posture()
    obs.log_event("info", "gateway.startup", config=s.describe(),
                  config_problems=problems or None)
    if problems:
        # 【审查修正 P2】原实现只写日志、不阻断启动：AUTH_MODE=jwt 且 JWT_SECRET 缺失时
        # 服务照常起来，表现为"所有登录/鉴权都以令牌无效失败"，与 prod_config 承诺的
        # "拒绝启动并给出提示"不一致。现在 fail-fast。
        for p in problems:
            obs.log_event("error", "gateway.config_invalid", problem=p)
        raise RuntimeError("网关配置不可用于启动：" + "；".join(problems))
    try:
        boot = auth_mod.bootstrap_admin()
        if boot.get("created"):
            obs.log_event("warn", "auth.bootstrap_admin_created",
                          username="admin", note=boot.get("warning"))
        # 【审查修正 P2】撤销表只在登出时追加、从不清理（无界增长）。
        # 启动时清一次过期记录，并在明细里体现。
        purged = auth_mod.purge_expired_revocations()
        if purged:
            obs.log_event("info", "auth.revocation_purged", removed=purged)
        # 启动时自愈：释放崩溃残留的审批占用（否则该 run 永远提示"已被他人处理中"）
        try:
            from mcp_server import runtime_store
            released = runtime_store.release_stale()
            if released:
                obs.log_event("warn", "workflow.stale_claim_released", runs=released)
        except Exception as exc:
            obs.log_event("warn", "workflow.stale_claim_check_failed",
                          error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        obs.log_event("error", "auth.bootstrap_failed", error=f"{type(exc).__name__}: {exc}")

    # 【第二次审查修正 P2b】启动即拉起能力服务看门狗：/health 的
    # capability_service_ready 在端口开放后约 1 秒内即为真实值，而不是恒 false。
    # 线程只做 HTTP 探活，不阻塞启动、不编译工作流。
    _start_capability_watchdog()

# 演示用示例合同（前端一键填充）
SAMPLES = {
    "low": {
        "title": "低风险：技术服务合同",
        "text": ("技术服务合同\n"
                 "第一条 甲方委托乙方提供系统运维服务，服务期限一年。\n"
                 "第二条 甲方应在验收合格后 30 日内支付服务费。\n"
                 "第三条 双方应按约定时间完成交付，验收标准以附件一为准。\n"
                 "第四条 乙方按约定开具增值税专用发票。\n"),
    },
    "mid": {
        "title": "中风险：含违约金偏高条款",
        "text": ("技术服务合同\n"
                 "第一条 甲方委托乙方提供系统运维服务，服务期限一年。\n"
                 "第二条 乙方违反保密义务的，违约金为合同总额的 50%。\n"
                 "第三条 甲方应在验收合格后 30 日内支付服务费。\n"),
    },
    "high": {
        "title": "高风险：多项红线条款",
        "text": ("劳动合同\n"
                 "第一条 本合同为固定期限劳动合同，期限三年。\n"
                 "第二条 乙方月工作时长不超过 280 小时，甲方可安排加班。\n"
                 "第三条 甲方有权无条件解除本合同，最终解释权归甲方所有。\n"
                 "第四条 本产品保本，年化收益不低于 5%。\n"
                 "第五条 甲方收集乙方个人信息无需取得同意，亦无需另行授权。\n"),
    },
}


# ---------------------------------------------------------------- 依赖

_orch = None
_orch_error: str | None = None
_capability_ready = False        # 最近一次探测结果，供 /health 轻量读取（不触发探测）
_cap_watchdog_started = False    # 后台探活线程只起一次
_cap_watchdog_lock = threading.Lock()

# 【审查修正】演示模式在非本机访问下属于严重越权风险：
# 演示模式把请求头 X-Role 当作角色，任何能访问端口的人都能自称管理员。
# 因此：当 AUTH_MODE=demo 且服务不绑定在回环地址时，**拒绝启动**，
# 必须显式设置 ALLOW_INSECURE_DEMO=1 才能强行启动（用于内网演示）。
ALLOW_INSECURE_DEMO = str(os.environ.get("ALLOW_INSECURE_DEMO", "")).strip().lower() in (
    "1", "true", "yes", "on")
# 实际监听地址在 __main__ 里由 --host 写入（安装 uvicorn 前设置）
BIND = {"host": os.environ.get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1",
        "port": int(os.environ.get("GATEWAY_PORT", "8790") or 8790)}

# 上传落盘目录（也是 /api/v1/contracts 唯一允许读取 path 的沙箱目录）
UPLOAD_DIR = PROJECT_ROOT / ".runtime" / "uploads"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024      # 单文件 20MB 上限
UPLOAD_CHUNK_BYTES = 1024 * 1024         # 流式落盘分块（1MB）：不把整包读进内存
UPLOAD_OVERHEAD_BYTES = 1024 * 1024      # multipart 头部/分隔符余量（Content-Length 早拒用）
UPLOAD_SUFFIXES = (".pdf", ".docx", ".doc", ".txt", ".md")


def check_auth_posture() -> list[str]:
    """启动期安全检查：返回阻断启动的问题列表。

    覆盖两类问题（前者来自 prod_config，后者是本文件的认证姿态）：
      - AUTH_MODE=jwt 但 JWT_SECRET 缺失/过短；DB_MODE=pg 但无 DATABASE_URL
      - AUTH_MODE=demo 且绑定非回环地址（演示令牌 + X-Role 头 = 任意人可自称管理员）
    """
    s = settings()
    problems: list[str] = list(s.validate())
    local_bind = BIND["host"] in ("127.0.0.1", "localhost", "::1")
    if s.auth_mode == "demo" and not local_bind and not ALLOW_INSECURE_DEMO:
        problems.append(
            f"AUTH_MODE=demo 且绑定在 {BIND['host']}（非回环地址）：演示令牌 + X-Role 头允许任意人自称管理员。"
            "请改用 AUTH_MODE=jwt（推荐），或仅在受控内网演示时设置 ALLOW_INSECURE_DEMO=1")
    return problems


def _safe_upload_name(name: str) -> str:
    """把客户端文件名收敛成"纯文件名"。

    【审查修正 P0】原实现直接把 file.filename 拼进落盘路径，`..\\..\\x.txt`
    或 `C:\\x.py` 可以写出上传目录之外（Windows 上 `Path(dir) / "C:\\x"` 会整体替换路径）。
    """
    base = Path((name or "contract").replace("\\", "/")).name.strip()
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    return base or "contract"


def _resolve_upload_path(raw: str) -> Path:
    """把用户给的 path 解析到上传目录内；越界或不存在一律 400。

    【审查修正 P0】原实现直接 `Path(payload["path"]).read_text()`，
    可以读取服务器任意文件（例如 .runtime/.lemonclaw/.env 里的 API Key）并使其进入
    标注/审计/工作流状态，属于任意文件读取。
    """
    p = Path(raw or "").expanduser()
    if not p.is_absolute():
        p = UPLOAD_DIR / p
    try:
        resolved = p.resolve()
        root = UPLOAD_DIR.resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="路径非法")
    if resolved != root and root not in resolved.parents:
        raise HTTPException(status_code=400, detail="只允许读取已上传目录内的文件（.runtime/uploads）")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在（请先上传）")
    return resolved


def _bounded_int(raw, default: int, lo: int, hi: int) -> int:
    """把外部传入的整型参数收敛到 [lo, hi]；非法值返回默认值。

    【审查修正 P2】原实现 `int(payload.get("max_iterations") or 12)`：
    非数字 → ValueError → 500；超大值 → recursion_limit 极大 → Agent 长时间占用
    线程与模型额度（可被用来打满资源）。
    """
    try:
        v = int(str(raw).strip()) if raw not in (None, "") else default
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _session_id_for(ident: dict, requested: str) -> str:
    """会话号一律带上调用者前缀，禁止跨用户读写/清空他人对话。

    【审查修正 P1】原实现直接采用客户端传入的 session_id，
    任意账号传 `session_id=wb-admin` 就能读他人历史、注入问题甚至清空上下文。
    【第二次审查修正】归属判定改为**精确匹配**：原来允许 `own + "-"` 前缀，
    于是用户 `bob` 可以碰 `bob-2` 的会话（`bob-2` 是另一个合法用户名）。
    """
    sid = (requested or "").strip() or f"wb-{ident['username']}"
    own = f"wb-{ident['username']}"
    if ident["role"] == "admin":
        return sid                                   # 管理员可运维任意会话
    if sid == own:
        return sid
    raise HTTPException(status_code=403,
                        detail=f"会话 {sid} 不属于当前账号（仅可访问 {own}）")


def _discard(path: Path) -> None:
    """删除上传的临时文件（失败不影响主流程）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _start_capability_watchdog() -> None:
    """启动后台低频探活线程，供 `/health` **无阻塞**读取能力服务状态。

    【第二次审查修正·P2】这里修的是上一轮 P0 的"另一半"：把误挂在 helper 上的
    `/api/v1/diagnostics` 装饰器摘回来之后，`orch_health()` 重新可达，
    但它**只有诊断接口（需鉴权）会调**。于是新起的网关在任何人调用诊断之前，
    `/health` 一律报 `capability_service_ready=false` —— 监控系统按这个字段判定
    就会误报，或者反过来被人"顺手改成 true"，两种结果都糟。

    `/health` 又不能自己去探活：上一轮已经确认过，未鉴权端点做 3 秒阻塞探测是
    现成的 DoS 面。两条要求只能靠"**后台定时探测 + 前台只读快照**"同时满足。
    线程只调 `probe_capability()`（一个 3 秒超时的 HTTP 请求），
    **不构造编排器、不编译工作流**，因此不会把重活带到未鉴权路径上。
    """
    global _cap_watchdog_started
    with _cap_watchdog_lock:
        if _cap_watchdog_started:
            return
        _cap_watchdog_started = True

    def _loop() -> None:
        global _capability_ready
        while True:
            try:
                from service.review_orchestrator import probe_capability
                ch = probe_capability()
                _capability_ready = ch.get("status") == "running"
            except Exception:
                _capability_ready = False
            time.sleep(15)

    threading.Thread(target=_loop, name="capability-watchdog", daemon=True).start()


def orch():
    """获取编排器（首次调用时编译工作流）。

    注意：编译依赖"岗位能力服务在线"（图里的 MCP 工具在编译期取）。
    若此刻能力服务未就绪，这里会失败——因此**不在失败时缓存实例**，
    下一次请求会重试，避免网关因启动顺序问题永久不可用。
    """
    global _orch, _orch_error
    if _orch is not None:
        return _orch
    try:
        from service.review_orchestrator import ReviewOrchestrator
        o = ReviewOrchestrator()
        o.ensure_compiled()
        _orch, _orch_error = o, None
    except Exception as exc:
        _orch_error = f"{type(exc).__name__}: {exc}"
        obs.log_event("error", "orchestrator.init_failed", error=_orch_error)
        raise
    return _orch


def orch_health() -> dict:
    """不抛异常地探测编排器状态；同时刷新能力服务就绪标记供 /health 轻量读取。"""
    global _capability_ready
    try:
        o = orch()
        ch = o.capability_health()
        _capability_ready = ch.get("status") == "running"
        return {"ready": True, "error": None, "version": o._graph[0] if o._graph else None,
                "capability_service": ch, "capability_ready": _capability_ready}
    except Exception as exc:
        _capability_ready = False
        return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}


def _resolve_identity(authorization, x_auth_token: str, x_role: str,
                      query_token: str = "") -> dict:
    """解析调用者身份。

    两种模式：
      jwt  ：Authorization: Bearer <token>（或 X-Auth-Token / ?token= 带 JWT）→ 从令牌取角色
      demo ：X-Auth-Token 固定演示令牌 + X-Role 指定角色
    返回 {username, role, name}，失败抛 401。

    为何支持 query_token：浏览器 EventSource 不能自定义请求头，
    实时日志流只能把令牌放进查询串（仅用于 SSE 端点）。
    """
    s = settings()
    if not isinstance(authorization, str):
        authorization = ""
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    token = token or (x_auth_token or "").strip() or (query_token or "").strip()

    # 【审查修正 P0】jwt / sso 模式下**彻底关闭**演示分支。
    # 原实现是 `if s.auth_mode == "jwt" and token and token != s.demo_token`：
    # 只要请求头带上内置默认演示令牌（"local-dev-token"），条件为假 → 落到演示分支 →
    # 直接采纳客户端自报的 X-Role，JWT 校验完全不被调用，任何能访问端口的人都能自称 admin。
    # 现在两种模式严格互斥：jwt/sso 模式下所有令牌（含演示令牌）都必须通过 verify_token。
    if s.auth_mode in ("jwt", "sso"):
        if not token:
            raise HTTPException(status_code=401,
                                detail="未授权：请登录后使用 Authorization: Bearer <token>")
        info = auth_mod.verify_token(token)
        if not info.get("ok"):
            raise HTTPException(status_code=401, detail=info.get("errors") or ["令牌无效"])
        return {"username": info["username"], "role": info["role"],
                "name": info.get("name", ""), "via": "jwt"}

    # 演示模式：固定演示令牌 + X-Role 指定角色（仅限本机/显式授权场景，见 check_auth_posture）
    if not token or token != s.demo_token:
        raise HTTPException(status_code=401,
                            detail="未授权：X-Auth-Token 不正确")
    role = (x_role or "specialist").lower()
    if role not in ROLES:
        raise HTTPException(status_code=403, detail=f"未知角色：{role}")
    return {"username": f"demo-{role}", "role": role, "name": f"演示账号（{role}）",
            "via": "demo"}


def auth(x_auth_token: str = Header(default=""),
         x_role: str = Header(default="specialist"),
         authorization: str = Header(default="")) -> str:
    """兼容旧签名的角色获取（保留给既有接口），返回角色字符串。

    【审查修正 P1】原来签名里没有 `authorization` 参数 —— 这是**实测发现的功能性缺陷**：
    仍然使用本函数的 10 个接口（/contracts、/contracts/upload、/runs、/runs/{id}、
    /pending、/decide、/recover、/proposals、/proposals/{id}/review、/metrics、/runs/{id}/events）
    在 `AUTH_MODE=jwt` 下**全部 401**：Bearer 令牌根本读不到，
    只有走 `identity()` 的少数几个接口能用 JWT。
    现在三个身份来源（Bearer / X-Auth-Token / query）都能被读取，并且要求调用方显式传入
    `authorization` 头（见各接口的 `authorization=authz`）。
    """
    if not isinstance(authorization, str):
        authorization = ""
    return _resolve_identity(authorization, x_auth_token, x_role)["role"]


def ident_from_headers(authorization: str, x_auth_token: str, x_role: str) -> dict:
    """给「不使用 Depends 的旧接口」提供身份（返回完整身份，含 username）。"""
    if not isinstance(authorization, str):
        authorization = ""
    return _resolve_identity(authorization, x_auth_token, x_role)


def identity(authorization: str = Header(default=""),
             x_auth_token: str = Header(default=""),
             x_role: str = Header(default="specialist")) -> dict:
    """标准身份依赖：只认请求头（Authorization / X-Auth-Token）。

    【第二次审查修正·P2】原先这里还接受 `?token=` 查询参数（"为了 EventSource"），
    但它被约 25 个普通端点共用 —— 令牌会进入访问日志、浏览器历史与 Referer。
    现在查询串令牌**只**由 SSE 专用依赖 `identity_with_query_token` 接受。
    """
    return _resolve_identity(authorization, x_auth_token, x_role, "")


def identity_with_query_token(authorization: str = Header(default=""),
                              x_auth_token: str = Header(default=""),
                              x_role: str = Header(default="specialist"),
                              token: str = Query(default="")) -> dict:
    """**仅供 SSE**（浏览器 EventSource 不能自定义请求头，只能把令牌放查询串）。"""
    return _resolve_identity(authorization, x_auth_token, x_role, token)


def require(role: str, allowed: tuple) -> None:
    if role not in allowed:
        raise HTTPException(status_code=403,
                            detail=f"角色 {role} 无权执行该操作（需要 {list(allowed)}）")


def require_perm(ident: dict, action: str) -> None:
    """按权限矩阵校验（动作 -> 允许角色，见 service/auth.py:PERMISSIONS）。"""
    if not auth_mod.has_permission(ident["role"], action):
        raise HTTPException(status_code=403, detail={
            "error": f"角色 {ident['role']} 无权限：{action}",
            "allowed_roles": list(auth_mod.PERMISSIONS.get(action, ())),
        })


def client_ip(request) -> str:
    """取**真实客户端 IP**：配置了 HTTP_TRUST_ENV（反向代理场景）时优先 X-Forwarded-For。

    【第二次审查修正·P1】登录/SSO 回调原先直接取 `request.client.host`：
    网关在反向代理/NAT 之后时那是**代理的 IP**，于是
      - 审计里的来源 IP 全是反代地址（追溯失效）；
      - 更严重的是登录失败退避按 `ip:<代理IP>` 计数 —— 任意人对任意用户名错 5 次
        就会锁住该 IP 下**所有账号（含管理员）** 300 秒，可反复触发成登录 DoS。
    现在与 trace 中间件用同一套解析口径（trust_proxy 时才信任代理头）。
    """
    if request is None:
        return ""
    try:
        if settings().trust_proxy:
            fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            if fwd:
                return fwd
        return request.client.host if request.client else ""
    except Exception:
        return ""


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """每个请求生成 trace_id，写结构化日志并回写响应头（全链路可追溯）。"""
    trace = obs.new_trace_id()
    obs.set_context(trace, path=request.url.path, method=request.method,
                    client=client_ip(request))
    started = time.time()
    try:
        response = await call_next(request)
    except Exception as exc:
        obs.log_event("error", "http.unhandled", error=f"{type(exc).__name__}: {exc}")
        raise
    elapsed = int((time.time() - started) * 1000)
    obs.log_event("warn" if response.status_code >= 400 else "info", "http.request",
                  status=response.status_code, elapsed_ms=elapsed)
    response.headers["X-Trace-Id"] = trace
    return response


# ---------------------------------------------------------------- 接口

@app.get("/health")
def health() -> dict:
    """存活探针：**只做无副作用的轻量检查**。

    【审查修正 P2】原实现未鉴权却触发重活：构造 agent runtime、触发编排器
    define_workflow 全量重编译、并对能力服务做 3 秒阻塞探测 —— 未授权调用即可
    拖垮网关（也是容器健康检查被用作 DoS 面的经典问题）。
    重诊断信息移到需鉴权的 GET /api/v1/diagnostics。

    【第二次审查修正 P2b】`capability_service_ready` 现在**有意义**了：
    它由后台看门狗线程（`_start_capability_watchdog`）每 15 秒刷新一次快照，
    本函数仍然只读快照、不发起任何网络调用。首次调用会顺手把看门狗线程起起来
    （幂等、立即返回），因此该字段最迟约 1 秒后就会反映真实状态。
    """
    _start_capability_watchdog()
    return {"status": "running", "gateway": "1.0.0",
            "orchestrator_ready": _orch is not None and _orch_error is None,
            "capability_service_ready": bool(_capability_ready),
            "auth_mode": settings().auth_mode}


def _sso_describe_safe() -> dict:
    """SSO 配置摘要（**仅供 diagnostics 内部调用**，不含密钥）。

    【第二次审查修正·P0】此前 `@app.get("/api/v1/diagnostics")` 装饰器被误加在本函数上
    （加新 helper 时把装饰器挤位），导致：
      ① 该端点**无任何鉴权**即可访问（匿名可读到 SSO 的 issuer/client_id/role_map 等内部配置）；
      ② 真正带 `Depends(identity)+require_perm("user:manage")` 的 `diagnostics()` 成了死代码，
         管理员诊断（数据库/连接器/OCR/规则/僵尸占用/日志）完全不可达；
      ③ `orch_health()` 再无人调用 → `_capability_ready` 恒为 False → `/health` 永远报
         capability_service_ready=false。
    现在装饰器只挂在 diagnostics 上，本函数保持为普通 helper。
    """
    try:
        from service import sso as sso_mod
        return sso_mod.describe()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _rules_health() -> dict:
    """规则表健康度（诊断用）：把"唯一真相源"的规模与版本摊开给运维看。"""
    try:
        from mcp_server import rules_store
        all_rules = rules_store.load_all()
        active = rules_store.active()
        proposals = rules_store.list_proposals()
        pending = [p for p in proposals if p.get("status") == "pending_supervisor"]
        return {"digest": rules_store.digest(), "active": len(active), "total": len(all_rules),
                "proposals_total": len(proposals), "proposals_pending": len(pending),
                "path": str(rules_store.RULES_PATH)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/v1/diagnostics")
def diagnostics(ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    """详细诊断（需 admin）：配置快照、编排器/能力服务/Agent 就绪度与问题清单。

    含数据库后端、外部系统连接器、OCR 档位、SSO 配置、规则表健康度与日志轮转状态。
    """
    require_perm(ident, "user:manage")
    from mcp_server import runtime_store
    from mcp_server import ocr as ocr_mod
    from mcp_server import db as db_mod
    from service import connectors
    s = settings()
    oh = orch_health()
    ar = agent_runtime()
    stale_claims = [r["run_id"] for r in runtime_store.list_pending()
                    if (r.get("claimed_by") or "").strip() and runtime_store.claim_is_stale(r["run_id"])]
    return JSONResponse({
        "config": s.describe(),
        "config_problems": check_auth_posture(),
        "connectors": connectors.describe(),
        "ocr": ocr_mod.describe(),
        "database": db_mod.describe(),
        "sso": _sso_describe_safe(),
        "rules": _rules_health(),
        "orchestrator": oh,
        "capability_service": (orch().capability_health() if oh["ready"]
                               else {"status": "unreachable",
                                     "reason": "orchestrator not ready"}),
        "agent_runtime": {"ready": ar.ready, "init_error": ar.init_error},
        # 崩溃残留的审批占用（>RUN_CLAIM_LEASE_SEC 未被释放）；启动时会自动清理
        "stale_claims": stale_claims,
        "logs": obs.log_stats(),
        "auth_hint": ("Authorization: Bearer <token>（登录获取）"
                      if s.auth_mode == "jwt" else "X-Auth-Token + X-Role")})


@app.post("/api/v1/maintenance")
def maintenance(payload: dict = Body(default={}), ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    """维护动作（需 admin）：释放过期审批占用、清理过期令牌撤销记录、轮转日志。

    这些动作原先只能靠重启进程触发；企业运维需要"不重启也能自愈"的入口。
    """
    require_perm(ident, "user:manage")
    actions = payload.get("actions") or ["stale_claims", "revocations"]
    done: dict = {}
    if "stale_claims" in actions:
        from mcp_server import runtime_store
        done["released_stale_claims"] = runtime_store.release_stale()
    if "revocations" in actions:
        done["purged_revocations"] = auth_mod.purge_expired_revocations()
    if "rotate_logs" in actions:
        done["logs"] = obs.rotate_now()
    obs.log_event("info", "maintenance.executed", by=ident["username"], actions=actions,
                  detail=done)
    return JSONResponse({"ok": True, "actions": actions, "detail": done})


# ---------------- 认证 ----------------

@app.get("/api/v1/auth/sso/config")
def sso_config() -> JSONResponse:
    """SSO 是否启用与可公开的配置（前端据此显示"企业登录"按钮）。"""
    from service import sso as sso_mod
    d = sso_mod.describe()
    # 只暴露前端需要的字段，issuer/client_id 等内部信息不下发
    return JSONResponse({"enabled": d["mode_enabled"],
                         "authorize_endpoint": "/api/v1/auth/sso/login",
                         "problems": d["problems"] if d["mode_enabled"] else []})


@app.get("/api/v1/auth/sso/login")
def sso_login_start(next: str = Query(default="")):
    """发起 SSO 登录：返回 authorization_url（前端跳转或由 302 直接跳）。

    安全说明：`next` 只允许站内相对路径，避免开放重定向被用作钓鱼跳板。
    """
    from service import sso as sso_mod
    if settings().auth_mode != "sso":
        raise HTTPException(status_code=400,
                            detail=f"当前 AUTH_MODE={settings().auth_mode}，未启用企业 SSO")
    safe_next = next if next.startswith("/") and not next.startswith("//") else ""
    info, err = sso_mod.start_login(sso_mod.SsoConfig.from_env(), next_url=safe_next)
    if err:
        obs.log_event("error", "auth.sso_start_failed", error=err)
        raise HTTPException(status_code=502, detail=f"SSO 发起失败：{err}")
    obs.log_event("info", "auth.sso_started", state=info["state"][:8])
    return JSONResponse({"ok": True, **info})


@app.get("/api/v1/auth/sso/callback")
def sso_callback(code: str = Query(default=""), state: str = Query(default=""),
                 error: str = Query(default=""), error_description: str = Query(default=""),
                 request: Request = None):
    """IdP 回调：校验 state → 交换令牌 → 严格校验 ID Token → 落地本地账号 → 签发本项目 JWT。

    成功后 302 回工作台并把令牌放在 URL fragment（`#token=...`）：
    fragment 不会进入服务端访问日志与 Referer，比查询串更安全。
    """
    from service import sso as sso_mod
    s = settings()
    if s.auth_mode != "sso":
        raise HTTPException(status_code=400, detail=f"当前 AUTH_MODE={s.auth_mode}，未启用企业 SSO")
    if error:
        raise HTTPException(status_code=401,
                            detail=f"IdP 返回错误：{error}（{error_description}）")
    ip = client_ip(request)
    cfg = sso_mod.SsoConfig.from_env()

    pending, perr = sso_mod.consume_state(state)
    if perr:
        obs.log_event("warn", "auth.sso_state_invalid", error=perr)
        raise HTTPException(status_code=400, detail=perr)
    if not code:
        raise HTTPException(status_code=400, detail="缺少授权码 code")

    tokens, terr = sso_mod.exchange_code(cfg, code, pending["verifier"])
    if terr:
        obs.log_event("error", "auth.sso_token_exchange_failed", error=terr)
        raise HTTPException(status_code=401, detail=terr)

    claims, cerr = sso_mod.verify_id_token(cfg, tokens["id_token"], nonce=pending["nonce"])
    if cerr:
        obs.log_event("error", "auth.sso_idtoken_invalid", error=cerr)
        raise HTTPException(status_code=401, detail=cerr)

    ident, ierr = sso_mod.identity_from_claims(cfg, claims)
    if ierr:
        raise HTTPException(status_code=401, detail=ierr)

    res = auth_mod.sso_login(ident["username"], ident["role"], display_name=ident["name"],
                             email=ident["email"], subject=ident["subject"],
                             auto_provision=cfg.auto_provision, ip=ip,
                             detail={"role_reason": ident["role_reason"],
                                     "iss": claims.get("iss")})
    if not res.get("ok"):
        raise HTTPException(status_code=401, detail=res.get("errors"))
    obs.log_event("info", "auth.sso_login_ok", username=res.get("username"),
                  role=res.get("role"), created=res.get("created"),
                  role_reason=ident["role_reason"])

    # 回跳工作台（令牌放 fragment，避免进入服务端日志）
    target = pending.get("next") or "/"
    sep = "&" if "#" in target else "#"
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"{target}{sep}token={res['access_token']}&via=sso",
                            status_code=302)


@app.post("/api/v1/auth/login")
def login(payload: dict = Body(...), request: Request = None) -> JSONResponse:
    ip = client_ip(request)
    res = auth_mod.login((payload.get("username") or "").strip(),
                         payload.get("password") or "", ip=ip)
    if not res.get("ok"):
        raise HTTPException(status_code=401, detail=res.get("errors"))
    obs.log_event("info", "auth.login_ok", username=res.get("username"), role=res.get("role"))
    return JSONResponse(res)


@app.post("/api/v1/auth/logout")
def logout(payload: dict = Body(default={}),
           authorization: str = Header(default=""),
           x_auth_token: str = Header(default="")) -> JSONResponse:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else x_auth_token
    res = auth_mod.logout(token or "", reason=(payload or {}).get("reason") or "user_logout")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("errors"))
    return JSONResponse(res)


@app.get("/api/v1/auth/me")
def whoami(ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    return JSONResponse({"ok": True, **ident,
                         "permissions": {k: (ident["role"] in v)
                                         for k, v in auth_mod.PERMISSIONS.items()}})


# ---------------- 账号管理（管理员） ----------------

@app.get("/api/v1/users")
def list_users(ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "user:manage")
    return JSONResponse({"count": len(auth_mod.list_users()), "users": auth_mod.list_users()})


@app.post("/api/v1/users")
def create_user(payload: dict = Body(...), ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "user:manage")
    res = auth_mod.create_user((payload.get("username") or "").strip(),
                               payload.get("password") or "",
                               (payload.get("role") or "").strip(),
                               payload.get("display_name") or "",
                               actor=ident["username"])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("errors"))
    obs.log_event("info", "auth.user_created", target=res.get("username"),
                  role=res.get("role"), by=ident["username"])
    return JSONResponse(res)


@app.post("/api/v1/users/{username}/status")
def set_user_status(username: str, payload: dict = Body(...),
                    ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "user:manage")
    res = auth_mod.set_user_status(username, (payload.get("status") or "").strip(),
                                   actor=ident["username"])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("errors"))
    return JSONResponse(res)


@app.post("/api/v1/users/{username}/password")
def change_password(username: str, payload: dict = Body(...),
                    ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    """本人改密（需旧密码）；管理员可代改（需显式管理员身份，见 auth.change_password）。"""
    is_admin = auth_mod.has_permission(ident["role"], "user:manage")
    if not is_admin and ident["username"] != username:
        raise HTTPException(status_code=403, detail="只能修改自己的密码")
    # 【审查修正 P2】把权限判断**传进** auth.change_password 内部校验（不再外置）：
    # 只有真正以管理员身份代改时才省略旧密码校验。
    admin_reset = is_admin and ident["username"] != username
    res = auth_mod.change_password(username, payload.get("old_password") or "",
                                   payload.get("new_password") or "",
                                   actor=ident["username"] if admin_reset else "",
                                   actor_role=ident["role"] if admin_reset else "")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("errors"))
    return JSONResponse(res)


# ---------------- 审计台 ----------------

@app.get("/api/v1/audit")
def audit_console(event: str = Query(default=""), actor: str = Query(default=""),
                  run_id: str = Query(default=""), limit: int = Query(default=100, ge=1, le=500),
                  ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "audit:read")
    from mcp_server import queue_store
    # 【审查修正 P2】原来取的是最早的 500 条再切前 limit 条 → 审计台永远只显示最旧事件。
    # 审计场景需要"最新优先"：先取最新 500 条，筛完再按时间倒序展示。
    if run_id:
        rows = queue_store.list_audit(run_id=run_id, limit=500, newest_first=True)
    else:
        rows = queue_store.list_audit_recent(limit=500)
    if event:
        rows = [r for r in rows if event in (r.get("event") or "")]
    if actor:
        rows = [r for r in rows if actor in (r.get("actor") or "")]
    rows = list(reversed(rows))[:limit]
    return JSONResponse({"count": len(rows), "events": rows})


@app.get("/api/v1/auth/events")
def auth_events(limit: int = Query(default=100, ge=1, le=500),
                ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "user:manage")
    return JSONResponse({"events": auth_mod.auth_events(limit=limit)})


# ---------------- 实时日志与配置 ----------------

@app.get("/api/v1/logs")
def logs(lines: int = Query(default=200, ge=1, le=1000),
         ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "audit:read")
    return JSONResponse({"lines": obs.tail(lines)})


@app.get("/api/v1/logs/stream")
async def logs_stream(request: Request, ident: dict = Depends(identity_with_query_token)):  # noqa: F821
    """SSE：实时日志流。

    【审查修正 P2】原实现是同步生成器：obs.subscribe() 内部用 time.sleep 阻塞，
    每个连接会独占一个 AnyIO 工作线程最长 120 秒，几十个连接即可耗尽默认线程池
    拖垮全部接口。现在改为 async 生成器 + asyncio.to_thread 拉取，并检测断连。
    """
    require_perm(ident, "audit:read")
    gen = obs.subscribe()

    async def agen():
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    item = await asyncio.to_thread(next, gen)
                except StopIteration:
                    break
                yield f"event: log\ndata: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
            yield "event: done\ndata: {}\n\n"
        finally:
            gen.close()

    return StreamingResponse(agen(), media_type="text/event-stream")


@app.get("/api/v1/config")
def config_view(ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    """展示当前生效配置与生产化检查结果（管理员）。"""
    require_perm(ident, "user:manage")
    s = settings()
    return JSONResponse({"config": s.describe(), "problems": s.validate(),
                         "deployment_checklist": DEPLOY_CHECKLIST})


DEPLOY_CHECKLIST = [
    {"item": "AUTH_MODE=jwt 且设置 JWT_SECRET", "why": "演示令牌不能用于生产"},
    {"item": "DB_MODE=pg 且 DATABASE_URL 指向生产库", "why": "单机 SQLite 不适合多人并发"},
    {"item": "对接真实 OA / 合同库 / 邮件网关", "why": "Mock 仅用于演示与容错验证"},
    {"item": "按需开启 OCR 档位", "why": "扫描件当前只能转人工"},
    {"item": "配置 LOG_MODE=json 并接入日志平台", "why": "结构化日志便于检索与告警"},
    {"item": "修改初始管理员密码", "why": "首次启动会创建 admin/admin12345"},
    {"item": "明确合规责任边界（人工签核不可省）", "why": "AI 结论仅供参考"},
]


# ---------------- 数字员工对话 ----------------

_agent = None


def agent_runtime():
    global _agent
    if _agent is None:
        from service.agent_runtime import runtime
        _agent = runtime()
    return _agent


@app.get("/api/v1/agent/info")
def agent_info(ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "agent:task")
    d = agent_runtime().describe()
    return JSONResponse(d)


@app.post("/api/v1/agent/task")
def agent_task(payload: dict = Body(...), ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "agent:task")
    res = agent_runtime().chat((payload.get("task") or ""),
                               session_id=_session_id_for(ident, payload.get("session_id")),
                               actor=ident["username"],
                               max_iterations=_bounded_int(payload.get("max_iterations"), 12, 1, 24))
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("errors"))
    return JSONResponse(res)


@app.get("/api/v1/agent/history")
def agent_history(session_id: str = Query(default=""),
                  ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "agent:task")
    res = agent_runtime().history(_session_id_for(ident, session_id))
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("errors"))
    return JSONResponse(res)


@app.post("/api/v1/agent/reset")
def agent_reset(payload: dict = Body(default={}), ident: dict = Depends(identity)) -> JSONResponse:  # noqa: F821
    require_perm(ident, "agent:task")
    res = agent_runtime().reset(_session_id_for(ident, (payload or {}).get("session_id")))
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("errors"))
    return JSONResponse(res)


@app.post("/api/v1/contracts")
def submit_contract(payload: dict = Body(...),
                    authorization: str = Header(default=""),
                    x_auth_token: str = Header(default=""),
                    x_role: str = Header(default="specialist")) -> JSONResponse:
    role = auth(x_auth_token, x_role, authorization)
    require(role, ("specialist", "admin"))
    text = payload.get("text") or ""
    if not text and payload.get("path"):
        # 【审查修正 P0】原实现直接读任意路径，可把 .env 等敏感文件送入审查流程
        text = _resolve_upload_path(str(payload["path"])).read_text(
            encoding="utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(status_code=400, detail="缺少合同文本（text 或 path）")
    res = orch().submit(text, submitted_by=payload.get("submitted_by") or f"{role}@gateway")
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("errors"))
    return JSONResponse(res)


# ---------------------------------------------------------------- 界面

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """合规官工作台（单页应用，零外部依赖，内联 CSS/JS）。"""
    page = WEB_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>界面文件缺失</h1><p>缺少 gateway/web/index.html</p>",
                            status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/v1/samples")
def samples() -> JSONResponse:
    """演示用示例合同（前端一键填充）。"""
    return JSONResponse({"samples": SAMPLES})


@app.post("/api/v1/contracts/upload")
async def upload_contract(request: Request,
                          file: UploadFile = File(...),
                          authorization: str = Header(default=""),
                          x_auth_token: str = Header(default=""),
                          x_role: str = Header(default="specialist")) -> JSONResponse:
    """上传合同文件（PDF / Word / 文本），解析后发起审查。

    【第二次审查修正·P1】原先先 `await file.read()` 把**整包读进内存**再判断大小：
    任一已登录的专员传一个超大文件就能把网关内存打满（OOM）。
    现在先看 `Content-Length` 早拒，再**分块流式落盘**并边写边计数，超限立刻中止并删除半成品。
    """
    role = auth(x_auth_token, x_role, authorization)
    require(role, ("specialist", "admin"))
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        raise HTTPException(status_code=415,
                            detail=f"不支持的文件类型：{suffix}（支持 {'/'.join(UPLOAD_SUFFIXES)}）")
    declared = 0
    try:
        declared = int(request.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared and declared > MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"请求体过大（{declared} 字节，上限约 {MAX_UPLOAD_BYTES} 字节）")

    tmp_dir = UPLOAD_DIR
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 【审查修正 P0】文件名先净化、再用随机名落盘：
    # 原实现 `f"{ts}-{file.filename}"` 可被 `..\..\x.txt` / `C:\x.py` 穿越出上传目录。
    safe_name = _safe_upload_name(file.filename or "contract")
    tmp_path = tmp_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}-{safe_name}"
    written = 0
    try:
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件过大（已接收 {written} 字节，上限 {MAX_UPLOAD_BYTES} 字节）")
                fh.write(chunk)
    except HTTPException:
        _discard(tmp_path)                 # 超限：删掉半成品，不留垃圾
        raise
    except Exception as exc:
        _discard(tmp_path)
        raise HTTPException(status_code=400,
                            detail=f"上传中断：{type(exc).__name__}: {exc}")
    if not written:
        _discard(tmp_path)
        raise HTTPException(status_code=400, detail="文件为空")

    if suffix in (".txt", ".md"):
        text = tmp_path.read_text(encoding="utf-8", errors="replace")
        _discard(tmp_path)
        res = orch().submit(text, submitted_by=f"{role}@gateway")
    elif suffix in (".pdf", ".docx", ".doc"):
        # 走能力服务的解析通道（read_contract_file 由网关本地实现，避免额外网络往返）
        from mcp_server import tools_registry as T
        try:
            parsed = T._parse_contract(path=str(tmp_path))
        finally:
            _discard(tmp_path)                   # 解析结果已在内存，临时文件不再需要
        if not parsed.get("ok"):
            raise HTTPException(status_code=400, detail=parsed.get("errors") or ["解析失败"])
        clauses = parsed.get("clauses") or []
        text = "\n".join(c.get("text", "") for c in clauses)
        if not text.strip():
            raise HTTPException(status_code=422,
                                detail="未从文件中提取到文本（可能是扫描件；当前为无 OCR 档位，请转人工）")
        res = orch().submit(text, submitted_by=f"{role}@gateway")
        res["parsed"] = {"source_format": parsed.get("source_format"),
                         "clause_count": parsed.get("clause_count"),
                         "tables_found": parsed.get("tables_found"),
                         "confidence": parsed.get("confidence"),
                         "warnings": parsed.get("warnings")}
    else:
        raise HTTPException(status_code=415, detail=f"不支持的文件类型：{suffix}（支持 .pdf/.docx/.txt/.md）")

    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("errors"))
    res["file_name"] = file.filename
    return JSONResponse(res)


@app.get("/api/v1/rules")
def rules(authorization: str = Header(default=""),
          x_auth_token: str = Header(default=""),
          x_role: str = Header(default="specialist")) -> JSONResponse:
    """规则表快照（含版本与审计字段），供界面展示"规则如何演进"。

    【审查修正 P2】原来只校验"令牌正确"、不校验角色权限；
    现按权限矩阵收敛为 rules:read（顺带把 Bearer 令牌接进来，jwt 模式下可用）。
    """
    ident = ident_from_headers(authorization, x_auth_token, x_role)
    require_perm(ident, "rules:read")
    from mcp_server import rules_store
    items = []
    for r in rules_store.load_all():
        # 注意：YAML 会把 2026-10-09 这类值解析成 date 对象，直接返回会让 JSON 序列化失败
        items.append({
            "id": r.get("id"), "version": r.get("version"), "status": r.get("status"),
            "severity": r.get("severity"), "pattern": r.get("pattern"),
            "regulation_ref": r.get("regulation_ref"), "suggestion": r.get("suggestion"),
            "source": r.get("source"), "approved_by": r.get("approved_by"),
            "approved_at": str(r.get("approved_at") or ""), "changelog": r.get("changelog"),
        })
    return JSONResponse({"digest": rules_store.digest(), "active": len(rules_store.active()),
                         "total": len(rules_store.load_all()), "rules": items})


@app.get("/api/v1/runs")
def list_runs(status: str = Query(default="", description="paused|completed|error|all"),
              limit: int = Query(default=50, ge=1, le=200),
              authorization: str = Header(default=""),
              x_auth_token: str = Header(default=""),
              x_role: str = Header(default="specialist")) -> JSONResponse:
    auth(x_auth_token, x_role, authorization)
    o = orch()
    if not o.graph_ready:
        o.ensure_compiled()
    rows = o.manager.dao.list_runs(workflow_id=None,
                                   status=(status or None), limit=limit)
    items = []
    for r in rows:
        st = o.status(r.run_id) if o.graph_ready else {}
        items.append({"run_id": r.run_id, "status": st.get("status") or r.status,
                      "risk_level": st.get("risk_level"), "doc_id": st.get("doc_id"),
                      "specialist_note": st.get("specialist_note"),
                      "supervisor_note": st.get("supervisor_note"),
                      "hit_rule_ids": st.get("hit_rule_ids"),
                      "updated_at": str(r.updated_at), "error": r.error})
    return JSONResponse({"count": len(items), "items": items})


@app.get("/api/v1/runs/{run_id}")
def run_detail(run_id: str, authorization: str = Header(default=""),
               x_auth_token: str = Header(default=""),
               x_role: str = Header(default="specialist")) -> JSONResponse:
    ident = ident_from_headers(authorization, x_auth_token, x_role)
    require_perm(ident, "contract:read")
    st = orch().status(run_id)
    if not st.get("ok"):
        raise HTTPException(status_code=404, detail=st.get("errors"))
    return JSONResponse(st)


@app.get("/api/v1/pending")
def pending(role: str = Query(default=""), status: str = Query(default="open"),
            authorization: str = Header(default=""),
            x_auth_token: str = Header(default=""),
            x_role: str = Header(default="specialist")) -> JSONResponse:
    caller = auth(x_auth_token, x_role, authorization)
    # 【审查修正 P1】原写法 `role if (role and caller == "admin") else (role or caller)`
    # 反了：非管理员传 ?role=supervisor 反而能看主管待办；管理员不带 role 时只看 admin。
    # 正确语义：管理员可指定角色（不指定=全部）；其他人只能看自己的角色。
    target = (role or "") if caller == "admin" else caller
    return JSONResponse(orch().pending(role=target, status=status))


@app.post("/api/v1/runs/{run_id}/decide")
def decide(run_id: str, payload: dict = Body(...),
           authorization: str = Header(default=""),
           x_auth_token: str = Header(default=""),
           x_role: str = Header(default="specialist")) -> JSONResponse:
    role = auth(x_auth_token, x_role, authorization)
    decision = (payload.get("decision") or "").strip()
    actor = payload.get("actor") or f"{role}@gateway"
    res = orch().decide(run_id, decision, actor, role, note=payload.get("note") or "",
                        reannotate_text=payload.get("reannotate_text") or "")
    if not res.get("ok"):
        # 并发冲突用 409，参数/权限问题用 400/403
        # 【审查修正 P2】把"决策与当前等待节点不匹配"也归入 409（状态冲突）：
        # 它不是请求格式错误，而是"这个 run 现在不该由你来处理"，语义上是冲突。
        msg = " ".join(res.get("errors") or [])
        code = 409 if ("已被" in msg or "状态为" in msg or "不匹配" in msg or "没有等待中" in msg) else 400
        raise HTTPException(status_code=code, detail=res.get("errors"))
    return JSONResponse(res)


@app.post("/api/v1/recover")
def recover(authorization: str = Header(default=""),
            x_auth_token: str = Header(default=""),
            x_role: str = Header(default="specialist")) -> JSONResponse:
    # 【审查修正 P2】原默认值 "admin" 属于 fail-open：演示模式下不带头即拿管理员权限。
    # 默认值一律取最小权限角色，真实角色仍由令牌/请求头决定。
    ident = ident_from_headers(authorization, x_auth_token, x_role)
    require_perm(ident, "system:recover")
    return JSONResponse(orch().recover_on_startup())


@app.get("/api/v1/proposals")
def proposals(status: str = Query(default=""),
              authorization: str = Header(default=""),
              x_auth_token: str = Header(default=""),
              x_role: str = Header(default="specialist")) -> JSONResponse:
    # 【审查修正 P2】原实现只 auth() 不校验权限，且默认角色给的是 supervisor（fail-open），
    # 任意角色都能读到提案里的合同号、驳回原因与冲突信息。现按权限矩阵收敛。
    role = auth(x_auth_token, x_role, authorization)
    require(role, ("supervisor", "admin"))
    return JSONResponse(orch().proposals(status=status))


@app.post("/api/v1/proposals/{proposal_id}/review")
def review_proposal(proposal_id: str, payload: dict = Body(...),
                    authorization: str = Header(default=""),
                    x_auth_token: str = Header(default=""),
                    x_role: str = Header(default="specialist")) -> JSONResponse:
    role = auth(x_auth_token, x_role, authorization)
    require(role, ("supervisor", "admin"))
    res = orch().review_proposal(proposal_id, (payload.get("decision") or "").strip(),
                                 payload.get("actor") or f"{role}@gateway",
                                 note=payload.get("note") or "")
    if not res.get("ok"):
        raise HTTPException(status_code=409 if "已被" in " ".join(res.get("errors") or [])
                            else 400, detail=res.get("errors"))
    return JSONResponse(res)


@app.get("/api/v1/metrics")
def metrics(authorization: str = Header(default=""),
            x_auth_token: str = Header(default=""),
            x_role: str = Header(default="specialist")) -> JSONResponse:
    ident = ident_from_headers(authorization, x_auth_token, x_role)
    require_perm(ident, "metrics:read")
    return JSONResponse(orch().metrics())


@app.get("/api/v1/runs/{run_id}/events")
async def run_events(run_id: str, request: Request,
                     authorization: str = Header(default=""),
                     x_auth_token: str = Header(default=""),
                     x_role: str = Header(default="specialist"),
                     token: str = Query(default="")):
    """SSE：把该 run 的审计链以事件流形式推送（轮询式，不侵入框架输出通道）。

    说明：框架的流式输出硬绑定终端通道（D0 实读），因此网关侧采用"审计链事件流"，
    语义上等价于进度推送，且与证据链同源。浏览器 EventSource 不能自定义请求头，
    因此本端点（与 /api/v1/logs/stream）额外接受 `?token=`；其余端点只认请求头。

    【审查修正 P2】
      1) 原实现是**同步**生成器：每个 SSE 连接会独占一个 AnyIO 工作线程最多 120 秒，
         几十个连接即可耗尽默认线程池、拖垮全部接口。改为 async 生成器 + asyncio.sleep。
      2) 原实现用 `events[seen:]`（limit=200 的定长切片）做增量：单 run 审计超过 200 条后
         `seen` 恒为 200，**新事件永远不会再推送**。改为按 audit_id 游标查询。
      3) 增加客户端断连检测，避免僵尸连接空转。
    """
    ident = _resolve_identity(authorization, x_auth_token, x_role, token)
    require_perm(ident, "contract:read")      # 【审查修正 P2】原先只验令牌不验权限
    from mcp_server import queue_store

    async def gen():
        cursor = ""
        for _ in range(600):                    # 最多 10 分钟，避免无限占用
            if await request.is_disconnected():
                return
            events = queue_store.list_audit_since(run_id=run_id, cursor=cursor)
            for e in events:
                yield f"event: audit\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n"
                cursor = e.get("audit_id") or cursor
            st = orch().status(run_id)
            yield (f"event: status\ndata: "
                   f"{json.dumps({'status': st.get('status')}, ensure_ascii=False)}\n\n")
            if st.get("status") in ("completed", "error", "cancelled"):
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(1.0)
        yield "event: done\ndata: {\"reason\": \"timeout\"}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def main() -> int:
    parser = argparse.ArgumentParser(description="合规官轻量网关")
    parser.add_argument("--host", default=BIND["host"])
    parser.add_argument("--port", type=int, default=BIND["port"])
    args = parser.parse_args()
    # 绑定地址在启动自检里要用到（demo 模式 + 非回环 = 拒绝启动）
    BIND["host"], BIND["port"] = args.host, args.port
    import uvicorn
    print(f"[gateway] 监听 http://{args.host}:{args.port}"
          f"（认证头 X-Auth-Token，角色头 X-Role）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
