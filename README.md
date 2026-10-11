# 合规官「小合」 · Compliance Officer

> 岗位化的 **AI 合规数字员工**：接收合同 → 解析条款 → 检索法条 → 标注风险 → 人工签核 → 出具报告 → 沉淀规则
> 定位**低频高价值文档审查**：日均 < 100 份、审批角色 ≤ 10 人

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.143-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C)](https://langchain-ai.github.io/langgraph/)
[![MCP](https://img.shields.io/badge/MCP-2025--03--26-6E4AFF)](https://modelcontextprotocol.io/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 一、简介

合同合规审查有两个特点：**量不大，但每一份都不能错**。一份合同里的"最终解释权归甲方""违约金 50%""月工时不超过 280 小时"，
漏掉任何一条都可能是真实的合规事故；而人工审查的问题不是不认真，是**口径会漂** —— 同一条款今天判中风险、明天判低风险，换个人又不一样；
被驳回的理由也**留不下来**，下个月同类条款还得再吵一次。

小合把这个过程做成一个岗位：

1. **判定不依赖模型临场发挥**。风险由**版本化规则表 + 法规检索**算出来，每条判定都能回答"命中哪条规则、依据哪条法条"。
   大模型只负责对话与解释 —— 这是它能被评测、被复核的前提。
2. **决策权始终在人**。低风险自动办结；中高风险**必须**经专员初审 + 主管复核两道签核，且**决策与当前等待的节点强绑定**
   （专员批不了主管节点，一个角色不能一步办结）。
3. **规则自己长出来**。人工驳回的理由不会直接写成规则，而是生成**规则提案** → 三层去重/冲突裁决 → 主管批准 → 版本化生效，
   下次同类条款自动命中。规则库是可治理、可回溯的，不是黑盒。

> **本仓库不包含第三方运行时框架的源码。** 底座 [LemonClaw](https://github.com/nl8590687/lemonclaw)（Apache-2.0）以
> **外部依赖**方式引用，需自行 clone（见第四节）；本仓库对它的源码**零改动**，并用 110 个文件的 SHA256 基线证明这一点。

---

## 二、功能一览

### 2.1 文档解析（含表格与扫描件）

支持 `.txt` / `.docx` / `.pdf`（上传或指定路径），输出**条款切分 + 表格抽取（Markdown）+ 置信度**。

| 形态 | 处理方式 |
|---|---|
| 原生文本 PDF / Word | 直接抽文本层，置信度高 |
| **扫描件（整页位图、无文本层）** | 自动转 **OCR**（本机实测接入 RapidOCR，纯 CPU、模型随包分发）；档位与"是否真的用了 OCR"在结果里**显式可见** |
| 无 OCR 引擎可用时 | **不静默当文本**：标记低置信度 + "扫描件"告警，转人工 |

### 2.2 法规检索

自研 **keyword-ngram/v2**：中文 2-gram + 英文/数字词，带**文档频率过滤**与"至少 2 个有区分度 token 重合"的门槛 —— 确定性、零依赖、可单测。
法规库 **54 条**（覆盖劳动用工/数据合规/金融销售/政府采购/建设工程等 15 个领域），支持按领域过滤；
**检索为空时必须显式声明**（`empty_result` + 降级标记），不允许"看起来有依据"。

### 2.3 双层风险判定

| 层 | 依据 | 产出 |
|---|---|---|
| 规则层 | 版本化规则表（正则 + 等级 + 法条引用），9 条（8 生效 + 1 草稿） | `risk_level`（high/mid/low）+ 命中规则编号 |
| 引用层 | 法规检索 | 每条风险的**法条出处**；**无引用不判风险** |

整篇汇总给出 `overall_risk_level`、风险条款数、规则表指纹（`rule_table_digest`）—— 判定结果可复现、可比对。

### 2.4 人工签核闭环

```
提交 → 分诊 → 专员初审（confirm / modify / reject）→ 主管复核（approve / reject）→ 归档
                  ↑ modify 会带着意见回炉重新标注
```

- **按角色待办**：谁该处理什么，一屏可见；**原子认领**（4 个并发入口同时抢，只有 1 次生效，其余拿到可解释的"已被占用"）
- **跨节点决策拦截**：专员对停在主管节点的 run 提 approve → **409 拒绝**（防伪造签核、防单人一步办结）
- **启动补偿**：进程重启后扫描暂停中的 run，为缺失角色补建待办并留通知审计
- **修改可追溯**：`modify` 由服务层重新标注并留 `reannotated` 事件

### 2.5 规则提案闭环（让规则自己长出来）

| 提案情形 | 系统行为 |
|---|---|
| 与现有规则**模式完全相同** | 转「修订」，指向目标规则 |
| 与现有规则**存在包含关系** | 提示相似（给出 containment 与相似度） |
| 同模式但**等级判断冲突** | 转「冲突，需主管裁决」，**禁止直接批准写入** |
| 确实是新的 | 走「新增」；**批准前不参与任何判定** |
| 重复批准 / 重复认领 | 明确拒绝（幂等保护） |

批准后规则**版本号 +1** 并记录批准人/时间/变更说明；规则表支持快照与还原（自检会还原并校验指纹一致）。

### 2.6 主动工作

- **例行巡检**：轮询合同来源（共享盘目录 / HTTP 接口 / Mock），按**内容哈希**幂等去重，自动发起审查并投递通知
- **故障降级**：来源不可达、脏数据、超时都有明确降级语义与可读错误（不中断批次）
- **自排跟进**：审查中发现"需补充材料"时，员工**自己创建 Cron 跟进任务**，到期提醒

### 2.7 报告与证据链

- 报告双形态：**Markdown + PDF**（reportlab；中文字体不可用时显式降级为 Markdown 并说明原因）
- 每个 run 有**完整事件序列**：`review_submitted → todo_created → human_decision → todo_completed → … → review_archived`，
  每一跳记录**谁、何时、什么角色、什么决策**
- 审计链 append-only；结构化日志按 `trace_id` 串联（JSONL，含轮转）

### 2.8 数字员工对话

除固定流水线外，小合能接受**自由指令**（按会话隔离、可并发）：查规则、检索法条、解释某条判定的依据、总结某个 run 的过程。
回复中带规则编号/法条出处，并**显示它调用了哪些岗位工具**（`mcp__compliance__*`），而不是模型自由发挥。

### 2.9 工作台与权限

单文件原生前端（无前端框架、无 CDN，内网离线可用）：合同审查台、待办审批、运行概览、规则库、规则提案、审计台、
指标、账号管理、配置、诊断、员工对话、实时日志流（SSE）。
三档角色（专员 / 主管 / 管理员）× **11 项权限**，切角色即可看到菜单与按钮的差异，越权返回 403。

---

## 三、架构

### 3.1 分层设计

```
工作台        原生 HTML/JS 单文件（fetch + SSE）          127.0.0.1:8790
   ↓
接口层        FastAPI + Uvicorn（认证 RBAC · 待办 · 规则 · 指标 · 审计 · 日志流 · 对话）
   ↓
服务层        编排器 · 账号与 SSO · 连接器 · 主动工作 · 可观测 · 员工对话运行时
   ↓                                    ↘
能力层        本岗位能力服务（15 个 MCP 工具）        独立进程 :8770
   ↓            解析 / 表格 / 检索 / 标注 / 报告 / 规则提案 / 待办审计 / 指标
编排层        工作流（显式状态机：节点 · 条件路由 · 断点续接）
   ↓
底座          运行时框架 LemonClaw（Agent · 技能 · 渠道 · 记忆 · Cron）—— 源码零改动
   ↓
数据层        SQLite（默认，开箱即用） ／ PostgreSQL 16 + pgvector（交付形态）
```

### 3.2 主流程（显式状态机）

岗位能力不是"一次模型调用"，而是一条**可暂停、可恢复、可签核**的图：

```
entry_probe ──→ specialist_review ──→ etl_gate ──→ supervisor_verify ──→ finalize
   （分诊）        （专员初审）          （汇合）        （主管复核）        （归档出报告）
                      ↑   │
                      └───┘  modify：带意见回炉重新标注
```

| 位置 | 路由规则 |
|---|---|
| 分诊 | 低风险 → **不生成任何待办**，直接归档出报告 |
| 专员初审 | `confirm` → 交主管复核；`modify` → 回到分诊重新标注；`reject` → 终态并**转规则提案** |
| 主管复核 | `approve` → 归档；`reject` → 终态 |
| 决策校验 | 决策必须与**当前等待节点**匹配，否则 409（这条是防伪造签核的关键） |
| 恢复 | 一律走同步路径（框架线程池恢复路径在跨进程场景下记录不可靠，已在门禁中实测排除） |

### 3.3 零改动底座（可验证的边界）

岗位能力完全落在本项目进程内，通过**框架一等公民的 MCP 通道**挂载（工具注册为 `mcp__compliance__*`），
因此**不需要改任何框架源码**：

```
<workspace>/
├── compliance_officer/        ← 本仓库（全部自有代码）
│   ├── .lemonclaw/            ← 部署时写入框架约定的挂载点（mcp.json / skills / .env）
│   └── ...
└── lemonclaw-main/            ← 第三方框架（clone 得到，本仓库不含其源码）
```

`scripts/check-framework-integrity.ps1 -Verify` 用 **110 个文件的 SHA256 基线**证明"未改动"，可随时复验。

### 3.4 目录结构

```
compliance_officer/
├── mcp_server/          岗位能力服务（MCP Streamable HTTP + 15 个工具）
│   ├── compliance_mcp_server.py   协议与传输层（JSON-RPC 2.0）
│   ├── tools_registry.py          15 个工具的实现与注册
│   ├── contract_parser.py         条款切分 / 表格抽取
│   ├── ocr.py                     可插拔 OCR（paddle→rapidocr→tesseract→http→none）
│   ├── regulations_store.py       法规库与检索（keyword-ngram）
│   ├── rules_store.py             规则表（版本化 · 提案去重 · 冲突裁决 · 原子写入）
│   ├── queue_store.py             待办与审计（幂等 · 原子认领 · 游标 · 指标）
│   ├── runtime_store.py           运行态（僵尸占用释放）
│   └── db.py / _dbpath.py         双后端存储层（SQLite / PostgreSQL 同一份列定义）
├── service/             业务服务层
│   ├── review_orchestrator.py     编排器（两段式执行 · 决策-节点绑定 · 启动补偿）
│   ├── auth.py / sso.py           账号 · 权限矩阵 · JWT · OIDC SSO
│   ├── connectors.py             合同来源与邮件网关的连接器契约与多实现
│   ├── proactive_worker.py       例行巡检与自排跟进
│   ├── agent_runtime.py          员工对话运行时（按会话隔离）
│   ├── observability.py          结构化日志 · trace_id · 轮转 · 实时流
│   └── prod_config.py            生产配置门禁（缺关键配置拒绝启动）
├── gateway/             接口层
│   ├── app.py                    FastAPI（认证/RBAC/待办/规则/指标/审计/SSE/上传）
│   └── web/index.html            工作台（单文件原生前端）
├── mock/                外部系统替身：mock_office.py（OA+邮箱+故障注入）、mock_idp.py（真实 OIDC IdP）
├── skills/              岗位技能包（SKILL.md + 确定性脚本）
├── workflows/           声明式流程 spec
├── rules/risk_rules.yaml 风险规则表（唯一真相源）
├── data/                法规库 / 评测集 / 规则提案
├── deploy/              PostgreSQL 初始化 SQL（pgvector）
├── scripts/             自检 · 门禁 · 部署 · 运维工具
├── tests/               166 条单测（6 个模块）
└── docs/                20 份文档 + 28 个证据 JSON
```

---

## 四、快速开始

### 4.1 本机 Python（Windows，已实测；无需 Docker 与管理员权限）

```powershell
# ① 底座与本项目并排放在同一工作目录
git clone https://github.com/look378/compliance-officer.git
git clone https://github.com/nl8590687/lemonclaw.git ..\lemonclaw-main
#    也可放别处： $env:LEMONCLAW_HOME = 'D:\path\to\lemonclaw-main'

cd compliance-officer

# ② 虚拟环境与依赖（本项目依赖 + 框架依赖）
uv venv .runtime\venv --python 3.13          # 没有 uv 就 python -m venv .runtime\venv
.\.runtime\venv\Scripts\python.exe -m pip install -r requirements.txt
pwsh -File scripts\d0\install-runtime.ps1

# ③ 配置大模型密钥（写入框架侧 .env；默认指向 DeepSeek，OpenAI 兼容协议）
pwsh -File scripts\deploy-runtime.ps1 -EnableMcp -LlmKeyFrom <你的.env>

# ④ 一键准备试用环境：起服务 + 三组可用性自检（跑完服务保持运行）
pwsh -File scripts\trial-demo.ps1 -Reset

# ⑤ 停止
pwsh -File scripts\stop-demo.ps1
```

### 4.2 Docker Compose（配置已就绪，**容器未实测**）

```bash
git clone https://github.com/look378/compliance-officer.git
git clone https://github.com/nl8590687/lemonclaw.git      # 底座放在同级目录
cd compliance-officer

cp .env.enterprise.example .env      # 至少填 JWT_SECRET；LLM 密钥见第六节
export JWT_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")

docker compose up -d --build
```

| 入口 | 地址 |
|---|---|
| 工作台 | http://127.0.0.1:8790 |
| 岗位能力服务 | http://127.0.0.1:8770/health |
| Mock 外部系统 | http://127.0.0.1:8780/health |

> **诚实的边界**：本机没有 Docker daemon，因此 compose 只做到**配置级校验**
> （`docker compose config` 返回 0、四服务均带健康检查与就绪依赖）。容器实测属于"需要目标环境才能做"的事，见第十节。
>
> compose 会把底座以**只读**方式挂进容器（`${LEMONCLAW_HOME:-../lemonclaw-main}:/lemonclaw:ro`，
> 容器内 `LEMONCLAW_HOME=/lemonclaw`）：能力服务要 `from dao.db import get_db_path`、网关要编译工作流，
> 两者都必须能 import 到底座；`:ro` 顺带从系统层面保证"容器写不进底座"。

### 4.3 打开工作台

浏览器访问 **http://127.0.0.1:8790** → 右上角「认证设置」→ 令牌填 `local-dev-token`，角色选 `specialist` → 保存。

> 演示模式（`AUTH_MODE=demo`）用固定令牌模拟"已登录的某角色"，方便随时**切换身份看权限差异**，不需要账号密码。
> 生产用 `AUTH_MODE=jwt`（账号密码 + JWT）或 `AUTH_MODE=sso`（企业 OIDC 单点登录，见 `docs/15`）。

---

## 五、技术栈

### 5.1 核心技术选型

| 层次 | 选型 | 版本 | 说明 |
|---|---|---|---|
| 语言 | Python | 3.13.9 | 容器 `python:3.13-slim` |
| 接口层 | FastAPI + Uvicorn + Starlette | 0.143 / 0.54 / 1.7 | REST + SSE 流式 |
| 岗位能力通道 | **MCP Streamable HTTP** | 2025-03-26 | JSON-RPC 2.0；框架一等公民工具 |
| 编排 | **LangGraph**（经底座） | 1.2 | 显式状态机、条件路由、断点续接 |
| 底座 | **LemonClaw**（外部依赖） | Apache-2.0 | Agent / 技能 / 渠道 / 记忆 / Cron |
| 大模型 | DeepSeek `deepseek-chat` | — | OpenAI 兼容协议，128K 上下文，temperature 0.2 |
| 数据库 | SQLite（默认） / **PostgreSQL** | 内置 / 16.4 | 一份列定义渲染两套 DDL；`DB_MODE` 切换 |
| PG 驱动 | **pg8000** | 1.31.5 | 纯 Python，免编译（兼容 psycopg3/2） |
| 向量库（二期） | pgvector | pg16 | `regulation_embedding vector(1024)` 已预留 |
| 检索 | **自研 keyword-ngram/v2** | — | 中文 2-gram + 词频过滤，零依赖、可单测 |
| 解析 | pdfplumber / pypdf / python-docx | 0.11.10 / 6.20 / 1.2 | 文本层与表格 |
| OCR | **RapidOCR**（onnxruntime） | 1.2.3 | 纯 CPU、模型随包分发；档位可切换 |
| PDF 光栅化 | pypdfium2 | 5.14.0 | 扫描件送 OCR |
| 报告 | reportlab | 5.0.1 | Markdown + PDF（中文字体） |
| 认证 | PyJWT + PBKDF2-HMAC-SHA256 | 2.15.1 | HS256；口令 20 万次迭代（标准库，零编译依赖） |
| 企业 SSO | OIDC Authorization Code + PKCE | — | 严格校验签名/iss/aud/exp/nonce；组→角色映射 |
| 前端 | **原生 HTML/CSS/JS 单文件** | — | 无框架、无 CDN，内网离线可用 |
| 测试 | pytest + httpx | 9.1.1 / 0.28.1 | 166 条（6 模块） |

### 5.2 五个关键取舍

| 取舍 | 结论与理由 |
|---|---|
| **风险判定不用大模型** | 判定走规则表 + 确定性检索，模型只做对话与解释。合规结论必须可复现、可评测、可引用 —— 这也是评测能拿到 1.00 召回 / 0 误报的原因 |
| **能力走 MCP，不改底座** | 框架对 MCP 是一等公民；岗位能力留在本项目进程内，既拿到"跟随上游更新"的能力，又让"零改动"成为可验证事实（110 文件哈希） |
| **零外部服务优先** | 默认 SQLite 单文件 + 自研检索 + 本地 OCR，`trial-demo.ps1` 一条命令就能给人看；生产再切 PostgreSQL / 企业 OCR 中台（都只改配置） |
| **PBKDF2 而非 bcrypt/argon2** | 标准库实现，受限与内网环境免编译；20 万次迭代满足试点强度，少一层依赖 |
| **原生单文件前端** | 无供应链风险、文本可直接 diff 审查、内网离线可部署；这个项目的可审查性比 UI 花哨更重要 |

### 5.3 部署形态

- **本机形态**：`.runtime/venv` + SQLite，无任何外部服务，适合试用与演示
- **交付形态**：`docker compose`（capability / mock / gateway / postgres 四服务，均带健康检查）；
  `DB_MODE=pg` 时本项目自有表（待办/审计/账号/令牌撤销/登录退避）真正落 PostgreSQL
- **边界**：框架自有的工作流运行态仍由框架 DAO 写 SQLite（"不改框架源码"的必然结果，见 `docs/16`）
- **外部系统**：合同来源 `mock|dir|http`、邮件 `mock|http|smtp|none`，对接真实系统只改 `.env` 与字段映射

---

## 六、配置说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `AUTH_MODE` | `demo` | `demo` 演示令牌 / `jwt` 账号密码 / `sso` 企业单点登录（可并存） |
| `JWT_SECRET` | — | `jwt`/`sso` 模式**必填**（长度 ≥16），否则**拒绝启动** |
| `GATEWAY_AUTH_TOKEN` | `local-dev-token` | 仅 demo 模式的演示令牌 |
| `DB_MODE` | `sqlite` | `sqlite` 单文件 / `pg` PostgreSQL（需 `DATABASE_URL`） |
| `DATABASE_URL` | — | 例：`postgresql://compliance:***@127.0.0.1:5432/compliance` |
| `CONTRACT_SOURCE` | `mock` | `mock` / `dir`（共享盘）/ `http`（OA 接口） |
| `MAIL_GATEWAY` | `mock` | `mock` / `none` / `smtp` / `http` |
| `OCR_ENGINE` | `auto` | `auto` / `paddle` / `rapidocr` / `tesseract` / `http` / `none` |
| `OCR_MAX_PAGES` / `OCR_DPI` | `30` / `200` | 防超大件拖垮；精度与耗时权衡 |
| `OPENAI_BASE_URL` / `MODEL_NAME` | DeepSeek / `deepseek-chat` | 兼容任何 OpenAI 协议端点 |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_ROLE_MAP` | — | SSO 配置；角色映射支持点号路径（`realm_access.roles`） |
| `LOG_MODE` / `LOG_MAX_BYTES` | `console` / 8 MB | 结构化日志与轮转 |
| `HTTP_TRUST_ENV` | `false` | 在反向代理之后设 `true` 才采信 `X-Forwarded-For` |

完整样例（含每一项的取值含义与验收口径）见 **`.env.enterprise.example`**；
写错取值会被 `prod_config.validate()` 拦下并拒绝启动 —— 这是刻意设计，避免"带病上线"。

---

## 七、演示剧本（3 分钟）

前置：已按第四节启动。以下为逐句演示流程。

**00:00 低风险全自动** — 合同审查台 → 点「低风险示例」→ 提交。
预期：`completed / low`，**没有生成任何待办**。
> 讲一句：干净合同不惊动任何人。合规系统最怕的不是漏判，是把人耗在没风险的东西上。

**00:30 中风险转人工** — 点「中风险示例」（含"违约金 50%"）→ 提交。
预期：`paused / mid`，停在**合规专员**；待办审批里出现这一条。
> 讲一句：风险等级决定要不要人看，而不是人来决定要不要看。

**00:50 专员初审** — 待办审批（角色 specialist）→ 认领 → 确认。
预期：流程**没有结束**，转到了主管复核。
> 讲一句：一个角色不能一步办结。审批占用是原子抢占的，两个人同时点只有一个成功。

**01:20 故意越权（值得演）** — 保持 specialist 角色，对这条 run 点「通过」。
预期：**409 拒绝** —— "决策与当前等待节点不匹配"。
> 讲一句：这条不是防手滑，是防伪造签核：专员不能替主管签字。

**01:50 主管签署** — 角色切 supervisor → 待办审批 → 通过 → 归档。
预期：`completed`；进运行概览能看到完整证据链（谁、何时、什么角色、什么决策）。
> 讲一句：每一步都留痕，事后能回答"这单为什么这么判、谁签的"。

**02:20 规则自己长出来** — 规则提案：专员提一条 → 系统做去重/冲突裁决 → 切 supervisor 批准 → 回规则库看版本号 +1。
> 讲一句：人工驳回的理由不会直接变成规则，那样会污染判定；走提案-审批-版本化，规则库才敢信。

**02:50 员工对话** — 员工对话：问「最终解释权为什么有风险，依据哪条法条」。
预期：回复带规则编号/法条，并显示调用了 `search_regulations` / `annotate_clause` 等岗位工具。

**03:00 收尾** — 指标页看待办/决策分布/规则指纹；诊断页看数据库/连接器/OCR/规则表健康。

---

## 八、自检与测试

```powershell
# 单元测试（6 个模块 166 条：能力服务 / 连接器 / OCR / SSO / 数据库后端 / 审查工具自身）
.\.runtime\venv\Scripts\python.exe -m pytest tests -q

# 全量门禁（G1–G7 + 决策节点绑定 + 框架零改动），共 10 项
pwsh -File scripts\d0\run-all-gates.ps1

# 分阶段端到端自检
.\.runtime\venv\Scripts\python.exe scripts\d0\p2_tools_e2e.py    # 15 个工具的关键与失败路径（26 项）
.\.runtime\venv\Scripts\python.exe scripts\d0\p3_review_flow.py  # 四条流程路径（9 项）
.\.runtime\venv\Scripts\python.exe scripts\d0\p4_human_loop.py   # 待办闭环 / 并发互斥 / 启动补偿（10 项）
.\.runtime\venv\Scripts\python.exe scripts\d0\p5_rule_proposal.py# 规则提案三层去重与冲突（13 项）
.\.runtime\venv\Scripts\python.exe scripts\d0\p6_proactive.py    # 巡检/降级/连接器（12 项）
.\.runtime\venv\Scripts\python.exe scripts\d0\p7_eval.py         # 标注质量评测（25 份合同）
.\.runtime\venv\Scripts\python.exe scripts\d0\p7_gateway.py      # 网关业务流（12 项）
.\.runtime\venv\Scripts\python.exe scripts\d0\p7_webui.py        # 工作台可用性（30 项）
.\.runtime\venv\Scripts\python.exe scripts\d0\p7_production.py   # 身份/账号/配置/日志/对话（12 项）

# 真实组件（需本机具备条件）
.\.runtime\venv\Scripts\python.exe scripts\d0\pg_real_db.py      # 真 PostgreSQL 端到端（18 项）
.\.runtime\venv\Scripts\python.exe scripts\d0\sso_e2e.py         # 真 OIDC SSO 端到端（20 项）

# 代码质量
.\.runtime\venv\Scripts\python.exe scripts\tools\static_audit.py # 静态审查（AST + symtable）
.\.runtime\venv\Scripts\python.exe scripts\tools\secret_scan.py  # 推送前密钥扫描
pwsh -File scripts\check-framework-integrity.ps1 -Verify         # 底座零改动（110 文件 SHA256）
```

测试编队覆盖：协议正确性（MCP 握手/工具调用/错误）、解析与 OCR 档位、检索命中与空检索声明、
风险等级与引用完整性、待办幂等与原子认领、决策-节点绑定、并发（线程与跨进程）、断点续接、
规则提案去重与冲突裁决、连接器与故障降级、数据库双后端方言、认证与 SSO（PKCE/签名/nonce/停用账号）、
网关 RBAC 与上传边界。

---

## 九、效果指标口径

评测集：**25 份合同 / 81 条条款**（预埋风险条款 28、干净条款 54；含 11 例表格）。

| 指标 | 结果 | 口径 |
|---|---|---|
| 风险召回 | **1.00** | 预埋风险条款被规则命中的比例（漏报 0） |
| 误报 | **0 处** | 干净条款被判为有风险的数量 |
| 等级一致性 | **1.00** | 命中条款的风险等级与标注期望一致 |
| 类型识别率 | **0.96** | 合同类型（劳动/技术/采购…）判断正确率 |
| 表格抽取 | 成功率 1.00 / **单元格正确率 100%**（11 例） | 表格转 Markdown 后逐单元格比对 |
| 人工介入率 | **0.76**（19/25） | 需人工签核的合同占比（高风险与中风险都转人工） |

> 指标由 `scripts/d0/p7_eval.py` 可复现产出，原始结果在 `docs/evidence/p7-eval.json`。
> 判定完全由规则与检索给出，**不经过大模型**，因此这些数字是可重复的，不是"跑一次好看"。

---

## 十、已知限制

1. **未对接任何真实企业系统**：连接器（`dir` / `http` / `smtp`）已就绪并有端到端回归，但**现场字段名与鉴权未联调**，
   对接真实 OA / 合同库 / 邮件网关需要按其接口做映射（契约见 `docs/13`）。
2. **未接贵司 IdP**：SSO 已用标准 OIDC 实现的本地 IdP 跑通（真 RSA 签名 / 真 PKCE / 20 项断言），
   但未接过 Keycloak / Azure AD / Okta 实例。
3. **单实例**：SSO 的一次性 `state` 与 ticket 在进程内存；多实例共享与框架侧运行态共享未做。
4. **Docker 容器未真跑**：本机无 Docker daemon，compose 仅通过配置级校验（四服务健康检查与就绪依赖已声明，
   容器内底座的只读挂载已补齐）。真跑一遍容器需要在有 Docker 的环境验证。
5. **规则为确定性匹配**：不做否定语义理解，"本协议不设最终解释权条款"这类含否定词的正向表述可能被告警 ——
   **有意取舍**：宁可让人工多复核一次，也不放过真实违规。
6. **法规库需业务核验**：条文来自公开整理，正式上线前应由合规岗对照监管原文核验。
7. **大模型只做解释**：员工对话的回答仍需人工确认后才可对外使用。

---

## 十一、二期方向

| 项 | 现状 | 二期 |
|---|---|---|
| 法规向量检索 | keyword-ngram 词面检索 | 接入 pgvector + bge-large-zh，混合检索与重排（表已预留） |
| 企业对接 | 连接器就绪 + 字段映射 | 现场联调 OA / 合同库 / 邮件网关，补齐鉴权方式 |
| 身份 | 本地账号 + 标准 OIDC | 贵司 IdP 联调 + SCIM 组织同步 |
| 部署 | 单实例 + compose | 多实例（共享会话状态）+ K8s + 审计落湖 |
| 规则治理 | 提案到批准全流程 | 规则回归集自动跑批：改规则前先在历史合同上验证影响面 |
| 文档解析 | 文本/Word/PDF + OCR | OCR 版面还原（表格结构、印章、手写签名） |

---

## 十二、说明

- 本项目为**独立设计与实现**。仓库内**不含任何真实密钥**：`.env` 已在 `.gitignore` 中，
  并提供 `scripts/tools/secret_scan.py` 作为推送前守卫（只扫 git 索引，占位符自动降噪，豁免项须写入 `.secret-scan-allow` 并注明理由）。
- 仓库内的示例合同、法规与规则**全部为自编或公开整理**，不含任何真实企业合同与个人信息。
- 底座 [LemonClaw](https://github.com/nl8590687/lemonclaw) 为 **Apache License 2.0**，
  不随本仓库分发，其著作权与许可归原作者；本项目对其源码**零改动**并以 SHA256 基线留证。
- 文档索引：`docs/00`～`docs/20`（立项 → 方案 → 各阶段报告 → 架构 → 演示剧本 → 评测 → 使用说明书 →
  两轮代码审查 → 企业对接契约 / SSO / PostgreSQL → 真实验证记录 → 技术栈 → 试用指引），
  每阶段的原始证据在 `docs/evidence/`（28 个 JSON）。

## License

[MIT](LICENSE)
