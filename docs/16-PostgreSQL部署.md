# PostgreSQL 部署说明（DB_MODE=pg）

> 面向 **贵司 DBA / 运维**。本文说明「小合」哪些数据会进 PG、怎么部署、怎么验证，
> 以及**哪些数据仍然在 SQLite 里**（这一点必须说清楚，避免上线后以为"全上 PG 了"）。

代码实现：`mcp_server/db.py`（统一数据访问层，双后端）。
自动化验证：`tests/test_db_backend.py`（18 项）。

---

## 1. 迁移边界（先看这张表，避免误解）

| 数据 | `DB_MODE=pg` 后落在哪 | 说明 |
|---|---|---|
| 待办队列 `compliance_todo` | **PostgreSQL** | 人审闭环的唯一真相源 |
| 审计/证据链 `compliance_audit` | **PostgreSQL** | append-only |
| 账号体系 `compliance_user` | **PostgreSQL** | 含 SSO 自动建档的账号 |
| 令牌撤销 `compliance_token_revocation` | **PostgreSQL** | 登出/改密水位 |
| 登录退避 `compliance_login_fail` | **PostgreSQL** | 按账号/IP 维度 |
| 规则表 `rules/risk_rules.yaml` | 仍为 **YAML 文件**（唯一真相源） | 设计如此：规则要能被人审阅与评审 |
| 工作流运行态 `workflow_runs` / `checkpoints` / `writes` | **仍为本机 SQLite** | 由底座 `dao/db.py` 直接读写 |

> 因此：**单实例部署**用 PG 完全没问题（审计与待办持久化、可备份、可多进程读数）；
> **多实例部署**还需要框架侧支持共享运行态，属二期，已记入就绪度清单。

---

## 2. 部署步骤

### 2.1 起库（已有 PG 的跳过）

```bash
# 方式 A：用本仓 docker-compose（含 pgvector，便于二期做向量检索）
docker compose up -d postgres

# 方式 B：贵司已有 PG，只需建库与账号
psql -c "CREATE DATABASE compliance;"
psql -c "CREATE USER compliance WITH PASSWORD '<强口令>';"
psql -c "GRANT ALL PRIVILEGES ON DATABASE compliance TO compliance;"
```

### 2.2 装驱动

```powershell
.\.runtime\venv\Scripts\python.exe -m pip install "psycopg[binary]"
# 或： python -m pip install psycopg2-binary
```

> 本项目**不会**在缺驱动时偷偷退回 SQLite —— 会直接报
> `DB_MODE=pg 需要 PostgreSQL 驱动…`。这是刻意的：静默降级会让"以为上了 PG"变成假象。

### 2.3 配环境变量

```ini
DB_MODE=pg
DATABASE_URL=postgresql://compliance:<强口令>@127.0.0.1:5432/compliance
```

### 2.4 建表（幂等，可反复执行）

启动任一服务即自动建表；也可单独执行：

```powershell
.\.runtime\venv\Scripts\python.exe -c "from mcp_server import db; db.ensure_tables(); print(db.describe())"
```

期望输出包含 `"db_mode": "pg"`、`"driver": "psycopg3"`（或 psycopg2）、`"driver_ok": true`。

---

## 3. 验证清单（建议写进上线验收单）

```powershell
# 1) 后端与驱动自检（应显示 pg + driver_ok=true）
.\.runtime\venv\Scripts\python.exe -c "from mcp_server import db; print(db.describe())"

# 2) 数据层测试（方言转换、幂等、原子领取、游标不重不漏）
.\.runtime\venv\Scripts\python.exe -m pytest tests\test_db_backend.py -q

# 3) 业务闭环端到端（会真实写库：待办/审计/账号）
.\.runtime\venv\Scripts\python.exe scripts\d0\p4_human_loop.py
.\.runtime\venv\Scripts\python.exe scripts\d0\p5_rule_proposal.py

# 4) 直接查 PG 确认数据真的在里面
psql "$env:DATABASE_URL" -c "select count(*) from compliance_todo;"
psql "$env:DATABASE_URL" -c "select count(*) from compliance_audit;"
psql "$env:DATABASE_URL" -c "select username, role, status from compliance_user;"

# 5) 全量回归（必须全绿）
pwsh -File scripts/d0/run-all-gates.ps1
```

**验收口径**：

1. `db.describe()` 显示 PG 且驱动正常；缺驱动/缺 URL 时启动**明确失败**；
2. 第 3 步跑完后，PG 里的 `compliance_todo` / `compliance_audit` 计数**大于 0**；
3. 并发审批仍然"只有一次生效"（`compliance_todo` 的 `status` 不会出现双写）；
4. 审计游标查询不重不漏（`list_audit_since`）；
5. 第 5 步全量回归全绿。

---

## 4. 备份与留存（建议）

| 对象 | 建议 |
|---|---|
| PG 库 | 每日 `pg_dump`；审计表按月分区或归档（append-only，只增不减） |
| `rules/risk_rules.yaml` | 纳入版本库评审流程（规则变更必须走提案审批） |
| `docs/evidence/*.json` | 每次回归的留证，按迭代归档 |
| 本机 SQLite（工作流运行态） | 随容器/主机一起备份；**不是**审计依据，审计以 PG 为准 |

`cleanup_runs.py --purge-audit` 会删审计：生产环境建议**禁用**该开关，
审计保留策略由贵司制度决定（默认不删）。

---

## 5. 已知限制与后续

| 项 | 现状 | 后续 |
|---|---|---|
| 工作流运行态仍在 SQLite | 由底座 DAO 决定 | 二期：由框架侧提供可插拔存储，或在服务层加"运行态镜像表" |
| 缺少迁移工具（Alembic） | 表结构由 `db.ensure_tables()` 幂等创建；列变更需人工 `ALTER` | 建议引入 Alembic 管理版本化迁移 |
| 连接池 | 按线程缓存单连接（依赖最小） | 高并发场景建议接入 `psycopg_pool` |
| pgvector | compose 已提供镜像，本期未使用 | 二期用于法规向量检索与重排 |
| 多实例互斥 | 待办层已用"单条 UPDATE 原子抢占"，可跨实例；运行态尚未 | 二期引入 PG advisory lock 统一互斥 |
