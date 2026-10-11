#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生产化配置（本项目自有）

集中管理"上线前必须显式确认"的开关，避免演示默认值被带进生产：
    AUTH_MODE       demo | jwt | sso  —— 演示令牌 / 本地账号 / 企业单点登录（OIDC）
    DB_MODE         sqlite | pg     —— 单机开发 或 交付/生产数据库
    JWT_SECRET      生产必须显式设置（未设置且 AUTH_MODE=jwt 时拒绝启动并给出提示）
    TOKEN_TTL_MIN   访问令牌有效期（分钟）
    RATE_LIMIT_RPM  网关每 IP 每分钟请求上限（0 关闭）
    LOG_MODE        console | json  —— 文本或结构化 JSON 日志
    HTTP_TRUST_ENV  是否信任代理头（X-Forwarded-For）

外部系统连接器（详见 docs/13-企业系统对接契约.md）：
    CONTRACT_SOURCE     mock | dir | http  —— 合同来源（默认 mock，开箱即用）
    MAIL_GATEWAY        mock | http | smtp | none —— 通知通道（默认 mock）
    其余地址/字段映射/鉴权项由 service/connectors.py 直接读取，见该契约文档第 2–3 节。

读取顺序：环境变量 > .runtime/.lemonclaw/.env > 内置默认值。
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT_ROOT / ".runtime"
LEMONCLAW_DIR = RUNTIME / ".lemonclaw"
# 运行时框架位置：优先 LEMONCLAW_HOME，默认同级兄弟目录 lemonclaw-main（换路径不用改代码）
FRAMEWORK = Path(os.environ.get("LEMONCLAW_HOME")
                 or (PROJECT_ROOT.parent / "lemonclaw-main"))


def _load_env_file() -> None:
    """把 .runtime/.lemonclaw/.env 与框架侧 .env 读进进程环境（不覆盖已有值）。"""
    from dotenv import load_dotenv
    for p in (LEMONCLAW_DIR / ".env", FRAMEWORK / ".lemonclaw" / ".env"):
        if p.exists():
            load_dotenv(p, override=False)


def _b(name: str, default: bool = False) -> bool:
    return str(os.environ.get(name, str(default))).strip().lower() in ("1", "true", "yes", "on")


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


class Settings:
    """运行时配置快照（进程启动时读取一次）。"""

    def __init__(self) -> None:
        _load_env_file()
        self.auth_mode = os.environ.get("AUTH_MODE", "demo").strip().lower()
        self.jwt_secret = os.environ.get("JWT_SECRET", "").strip()
        self.jwt_issuer = os.environ.get("JWT_ISSUER", "compliance-officer").strip()
        self.token_ttl_min = _i("TOKEN_TTL_MIN", 480)
        self.db_mode = os.environ.get("DB_MODE", "sqlite").strip().lower()
        self.database_url = os.environ.get("DATABASE_URL", "").strip()
        self.rate_limit_rpm = _i("RATE_LIMIT_RPM", 0)
        self.log_mode = os.environ.get("LOG_MODE", "console").strip().lower()
        self.trust_proxy = _b("HTTP_TRUST_ENV", False)
        self.demo_token = os.environ.get("GATEWAY_AUTH_TOKEN", "local-dev-token")
        # 外部系统连接器（只读两个"选哪种"的开关，其余细节见 service/connectors.py）
        self.contract_source = os.environ.get("CONTRACT_SOURCE", "mock").strip().lower()
        self.mail_gateway = os.environ.get("MAIL_GATEWAY", "mock").strip().lower()

    # ---- 校验 ----
    def validate(self) -> list[str]:
        """返回阻断启动的问题列表（空表示可以启动）。"""
        problems: list[str] = []
        if self.auth_mode not in ("demo", "jwt", "sso"):
            problems.append(f"AUTH_MODE 取值非法：{self.auth_mode}（应为 demo|jwt|sso）")
        if self.auth_mode in ("jwt", "sso"):
            # SSO 模式同样用本项目签发的 JWT 承载会话，因此也需要密钥
            if not self.jwt_secret or len(self.jwt_secret) < 16:
                problems.append(
                    f"AUTH_MODE={self.auth_mode} 时必须设置 JWT_SECRET（长度 ≥16）。"
                    "生成方式：python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
        if self.auth_mode == "sso":
            from service.sso import SsoConfig
            problems.extend(SsoConfig.from_env().problems())
        if self.db_mode not in ("sqlite", "pg"):
            problems.append(f"DB_MODE 取值非法：{self.db_mode}（应为 sqlite 或 pg）")
        if self.db_mode == "pg" and not self.database_url:
            problems.append("DB_MODE=pg 时必须设置 DATABASE_URL")
        # 连接器取值必须可识别：写错就报错，避免"以为接了真系统、其实还在用 Mock"
        if self.contract_source not in ("mock", "dir", "http"):
            problems.append(f"CONTRACT_SOURCE 取值非法：{self.contract_source}（应为 mock|dir|http）")
        if self.mail_gateway not in ("mock", "http", "smtp", "none"):
            problems.append(f"MAIL_GATEWAY 取值非法：{self.mail_gateway}（应为 mock|http|smtp|none）")
        return problems

    def describe(self) -> dict:
        return {
            "auth_mode": self.auth_mode,
            "jwt_secret_set": bool(self.jwt_secret),
            "token_ttl_min": self.token_ttl_min,
            "db_mode": self.db_mode,
            "database_url_set": bool(self.database_url),
            "rate_limit_rpm": self.rate_limit_rpm,
            "log_mode": self.log_mode,
            "trust_proxy": self.trust_proxy,
            "demo_token_in_use": self.auth_mode == "demo",
            "contract_source": self.contract_source,
            "mail_gateway": self.mail_gateway,
        }


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
