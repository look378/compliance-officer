#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
交付物校验：docker-compose.yml 语法与结构

为什么单独写：本工具只做**配置级**校验（没有 Docker 守护进程也能跑，适合 CI 前置检查）；交付形态的**容器真跑**另有端到端自检（`scripts/d0/p8_docker_e2e.py`，见 docs/17 §4）。原说明：
因此把校验做成纯静态检查：YAML 可解析 + 必需服务齐全 + 端口/健康检查/卷声明完整。
若本机有 docker CLI，可额外用 `docker compose config` 做一次官方校验（见输出提示）。

用法： python scripts/tools/check_compose.py
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = PROJECT_ROOT / "docker-compose.yml"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "p7-delivery-check.json"

REQUIRED_SERVICES = {
    "capability": {"ports": ["8770"], "desc": "岗位能力服务（MCP 工具 + 规则库 + 待办）"},
    "gateway": {"ports": ["8790"], "desc": "轻量网关（认证/RBAC/待办/指标/SSE）"},
    "mock": {"ports": ["8780"], "desc": "Mock 外部系统（OA + 邮箱 + 故障注入）"},
    "postgres": {"ports": ["5432"], "desc": "PostgreSQL 16 + pgvector（交付形态数据库）"},
}


def main() -> int:
    # 【审查修正 P2】这里原来有 `args = parser.parse_args()` 但没有任何参数、也从不使用；
    # 删掉（无参解析只会在将来误用 argv 时埋雷）。
    parser = argparse.ArgumentParser(description="校验 docker-compose 交付物")
    parser.parse_args()

    problems: list[str] = []
    result: dict = {"check": "P7-delivery", "file": str(COMPOSE)}

    if not COMPOSE.exists():
        print(f"FAIL: 缺少 {COMPOSE}")
        return 1
    try:
        import yaml
        doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        result["yaml_parsed"] = True
    except Exception as exc:
        print(f"FAIL: YAML 解析失败：{exc}")
        return 1

    services = doc.get("services") or {}
    result["services"] = list(services.keys())
    for name, spec in REQUIRED_SERVICES.items():
        svc = services.get(name)
        if svc is None:
            problems.append(f"缺少服务：{name}（{spec['desc']}）")
            continue
        ports = [str(p) for p in (svc.get("ports") or [])]
        if not any(any(req in p for p in ports) for req in spec["ports"]):
            problems.append(f"{name} 未暴露预期端口 {spec['ports']}（当前 {ports}）")
        result.setdefault("service_detail", {})[name] = {
            "image": svc.get("image"), "ports": ports,
            "has_healthcheck": bool(svc.get("healthcheck")),
            "has_depends_on": bool(svc.get("depends_on")),
            "restart": svc.get("restart"),
        }

    volumes = list((doc.get("volumes") or {}).keys())
    result["volumes"] = volumes
    if not volumes:
        problems.append("未声明命名卷（数据库与运行态数据应持久化）")

    # Dockerfile 存在性
    for df in ("Dockerfile.python",):
        if not (PROJECT_ROOT / df).exists():
            problems.append(f"缺少 {df}")
    result["dockerfiles"] = [p.name for p in PROJECT_ROOT.glob("Dockerfile*")]

    # 可选：有 docker CLI 时做官方校验（不需要守护进程即可解析配置）
    #
    # 【第二次审查修正·P2】两点修正：
    #   ① 显式 `encoding="utf-8"`：Windows 默认用 GBK 读子进程输出，遇到中文/特殊字符
    #      会在读取线程里抛 UnicodeDecodeError，导致 stderr 恒为空、报错信息丢失
    #      （实测就是这个异常掩盖了真正的校验失败原因）。
    #   ② 用**一次性占位密钥**跑校验：compose 里 `JWT_SECRET: ${JWT_SECRET:?...}` 是刻意的
    #      "没配就拒绝启动"护栏，直接跑会因缺变量返回非 0；这里注入占位值以校验**配置结构本身**。
    docker = None
    probe_env = {**os.environ, "JWT_SECRET": "compose-config-probe-secret-0123456789"}
    try:
        docker = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=90, cwd=str(PROJECT_ROOT),
                                env=probe_env)
        stderr = (docker.stderr or "")
        result["docker_compose_config"] = {"returncode": docker.returncode, "stderr": stderr[:400],
                                          "note": "校验时注入了占位 JWT_SECRET（护栏要求）"}
        low = stderr.lower()
        if docker.returncode != 0 and "cannot connect" not in low:
            problems.append(f"docker compose config 校验失败：{stderr[:200]}")
    except FileNotFoundError:
        result["docker_compose_config"] = {"skipped": "本机无 docker CLI"}
    except Exception as exc:
        result["docker_compose_config"] = {"skipped": f"{type(exc).__name__}: {exc}"}

    result["problems"] = problems
    result["verdict"] = "PASS" if not problems else "FAIL"

    print("=== 交付物校验（docker-compose）===")
    print(f"  YAML 解析: {'OK' if result['yaml_parsed'] else 'FAIL'}")
    print(f"  服务: {result['services']}")
    for name, d in (result.get("service_detail") or {}).items():
        print(f"    - {name}: ports={d['ports']} 健康检查={d['has_healthcheck']} "
              f"依赖声明={d['has_depends_on']}")
    print(f"  命名卷: {volumes}")
    print(f"  Dockerfile: {result['dockerfiles']}")
    print(f"  docker compose config: {result.get('docker_compose_config')}")
    for p in problems:
        print(f"  ! {p}")
    print(f"  判定: {result['verdict']}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"  证据: {EVIDENCE}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
