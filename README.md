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

小合的判定口径、审批纪律、规则治理与全部业务能力都在本项目内实现；运行时底座（Agent / 工作流 / 技能 / 渠道）
是一个开源依赖，与 FastAPI、LangGraph 一样按依赖安装（见第四节）。

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
底座          运行时框架 LemonClaw（Agent · 技能 · 渠道 · 记忆 · Cron）
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

### 3.3 岗位能力如何挂载（MCP 一等公民）

岗位能力没有写进流程代码里，而是做成一个**独立的 MCP 服务**（`mcp_server/`，15 个工具），
由框架原生识别为 `mcp__compliance__*` 工具 —— 主 Agent 与工作流的 tool 节点都能直接调用。

这样做的好处是工程性的：

| 好处 | 说明 |
|---|---|
| 能力可独立演进 | 加一个工具只改本服务，不动流程定义；工具可单独单测（协议层与业务层分开） |
| Agent 与流程共用同一套能力 | 对话里能查规则/检索法条/解释判定，用的就是流程里那几个工具，不会两套逻辑漂移 |
| 边界干净 | 业务逻辑、规则、数据全在本项目进程内；底座只负责编排、渠道与会话 |

```
<workspace>/
├── compliance_officer/        ← 本项目（业务能力 + 接口 + 工作台 + 规则与数据）
│   └── .lemonclaw/            ← 部署时写入的挂载点（mcp.json / skills / .env）
└── lemonclaw-main/            ← 运行时底座（开源依赖，按第四节安装）
```

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

### 4.1 本机 Python（Windows，无需 Docker 与管理员权限）

```powershell
# ① 取代码 + 运行时底座（底座是开源依赖，放在同一工作目录即可）
git clone https://github.com/look378/compliance-officer.git
git clone https://github.com/nl8590687/lemonclaw.git ..\lemonclaw-main
#    底座放别处也行： $env:LEMONCLAW_HOME = 'D:\path\to\lemonclaw-main'

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

### 4.2 Docker Compose（可选）

```bash
git clone https://github.com/look378/compliance-officer.git
git clone https://github.com/nl8590687/lemonclaw.git      # 运行时底座，放在同级目录
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

> compose 会把底座以**只读**方式挂进容器（`${LEMONCLAW_HOME:-../lemonclaw-main}:/lemonclaw:ro`，
> 容器内 `LEMONCLAW_HOME=/lemonclaw`）：能力服务要 `from dao.db import get_db_path`、网关要编译工作流，
> 两者都必须能 import 到底座；只读挂载也让容器不会就地改动依赖。

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
| 运行时底座 | LemonClaw | Apache-2.0 | Agent 运行时 · 工作流 · 技能 · 渠道 · 记忆 · Cron |
| 大模型 | DeepSeek `deepseek-chat` | — | OpenAI 兼容协议，128K 上下文，temperature 0.2 |
| 数据库 | SQLite（默认） / **PostgreSQL** | 内置 / 16.4 | 一份列定义渲染两套 DDL；`DB_MODE` 切换 |
| PG 驱动 | **pg8000** | 1.31.5 | 纯 Python，免编译（兼容 psycopg3/2） |
| 向量库（预留） | pgvector | pg16 | `regulation_embedding vector(1024)` 已建表，可按需接入 |
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
| **能力走 MCP，不写进流程代码** | 框架对 MCP 是一等公民；岗位能力留在本项目进程内，单独演进、单独单测，Agent 与流程共用同一套工具，不会出现两套逻辑漂移 |
| **零外部服务优先** | 默认 SQLite 单文件 + 自研检索 + 本地 OCR，`trial-demo.ps1` 一条命令就能给人看；生产再切 PostgreSQL / 企业 OCR 中台（都只改配置） |
| **PBKDF2 而非 bcrypt/argon2** | 标准库实现，受限与内网环境免编译；20 万次迭代满足试点强度，少一层依赖 |
| **原生单文件前端** | 无供应链风险、文本可直接 diff 审查、内网离线可部署；这个项目的可审查性比 UI 花哨更重要 |

### 5.3 部署形态

- **本机形态**：`.runtime/venv` + SQLite，无任何外部服务，适合试用与演示
- **交付形态**：`docker compose`（capability / mock / gateway / postgres 四服务，均带健康检查）；
  `DB_MODE=pg` 时本项目自有表（待办/审计/账号/令牌撤销/登录退避）真正落 PostgreSQL
- **边界**：工作流运行态仍由底座 DAO 写 SQLite（这部分不在本次迁移范围，见 `docs/16`）
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

## 八、效果指标

评测集：**25 份合同 / 81 条条款**（预埋风险条款 28、干净条款 54；含 11 例表格）。

| 指标 | 结果 | 说明 |
|---|---|---|
| 风险召回 | **1.00** | 预埋风险条款全部命中，漏报 0 |
| 误报 | **0 处** | 干净条款没有被误判为风险 |
| 等级一致性 | **1.00** | 风险等级与标注期望完全一致 |
| 合同类型识别 | **0.96** | 劳动 / 技术 / 采购等类型判断正确率 |
| 表格抽取 | 成功率 1.00 / **单元格正确率 100%**（11 例） | 表格转 Markdown 后逐单元格比对 |
| 人工介入率 | **0.76**（19/25） | 需人工签核的合同占比 |

判定完全由规则表与法规检索给出，**不经过大模型**，因此这些数字是可重复的、可逐条复核的 ——
原始结果在 `docs/evidence/p7-eval.json`，评测脚本 `scripts/d0/p7_eval.py`。

---

## 九、使用前提与边界

1. **外部系统需要对接**：合同来源与邮件通道已做成连接器（`CONTRACT_SOURCE=dir|http`、`MAIL_GATEWAY=smtp|http`），
   接贵司 OA / 合同库 / 邮箱只需按其接口填字段映射，不改代码（契约见 `docs/13`）。
2. **身份**：演示模式开箱即用；生产用本地账号（`AUTH_MODE=jwt`）或贵司 OIDC IdP（`AUTH_MODE=sso`，见 `docs/15`）。
3. **部署形态**：当前为单实例；本项目自有表（待办/审计/账号）可落 PostgreSQL，
   工作流运行态由底座 DAO 写 SQLite（见 `docs/16`）。
4. **判定是确定性的规则匹配**：不做否定语义理解，"本协议不设最终解释权条款"这类含否定词的正向表述可能被告警 ——
   有意取舍：宁可让人工多复核一次，也不放过真实违规。
5. **法规库需业务核验**：条文来自公开整理，正式使用前建议由合规岗对照监管原文核验。
6. **大模型的回答供参考**：员工对话的输出需人工确认后再对外使用。

---

## 十、文档索引

| 文档 | 内容 |
|---|---|
| `docs/01-方案设计说明.md` | 架构、能力契约、分阶段计划、容错矩阵 |
| `docs/08-架构说明.md` | 系统边界、五要素、底座硬约束清单 |
| `docs/09-演示剧本.md` | 现场演示步骤与追问回答要点 |
| `docs/10-P7评测与交付报告.md` | 评测数据、验收清单 |
| `docs/11-使用说明书.md` | 快速上手、界面操作、接口、运维排障、企业就绪度清单 |
| `docs/13-企业系统对接契约.md` | **给 IT**：OA / 合同库 / 邮件三种接入方式与字段映射 |
| `docs/15-企业SSO对接.md` | **给身份管理员**：OIDC 配置、IdP 登记要点、验收步骤 |
| `docs/16-PostgreSQL部署.md` | **给 DBA/运维**：存储边界、部署与验收 |
| `docs/17-本机真实验证记录.md` | 真实 PostgreSQL / OCR / OIDC 的验证记录 |
| `docs/19-技术栈清单.md` | 完整技术栈、精确版本、端口拓扑、选型理由 |
| `docs/20-试用指引.md` | 四条场景的点击路径与预期结果 |
| `docs/evidence/` | 各阶段原始证据（JSON） |

工程细节（两轮代码审查、依赖验证、测试与回归入口）见 `docs/12`、`docs/18` 与
`docs/11` 的「自检与测试」章节；回归入口：`.\.runtime\venv\Scripts\python.exe -m pytest tests -q`。

---

## 十一、说明

- 本项目为**独立设计与实现**，采用 **MIT 许可**（见 `LICENSE`）。
- 仓库内**不含任何真实密钥**：`.env` 已在 `.gitignore` 中，并提供 `scripts/tools/secret_scan.py`
  作为推送前守卫（只扫 git 索引，占位符自动降噪，豁免项须写入 `.secret-scan-allow` 并注明理由）。
- 仓库内的示例合同、法规与规则**全部为自编或公开整理**，不含任何真实企业合同与个人信息。
- 第三方依赖（运行时底座与各 Python 包）版本见 `requirements.txt`，各自遵循其原始许可。

## License

[MIT](LICENSE)
