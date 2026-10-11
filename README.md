# 合规官「小合」 · Compliance Officer

> 岗位化的 AI 合规数字员工：接收合同 → 解析条款 → 检索法条 → 标注风险 → 人工签核 → 出具报告 → 沉淀规则
> 面向低频高价值的文档审查场景

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.143-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C)](https://langchain-ai.github.io/langgraph/)
[![MCP](https://img.shields.io/badge/MCP-2025--03--26-6E4AFF)](https://modelcontextprotocol.io/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 一、简介

合同合规审查量不大，但每一份都不能错。一条"最终解释权归甲方"、一条"违约金 50%"、一条"月工时不超过 280 小时"，
漏掉任何一条都可能是真实事故。而纯人工审查的问题不是不认真，是**口径会漂** —— 同一条款今天判中风险、明天判低风险；
被驳回的理由也留不下来，下个月同类条款还得再讨论一次。

小合把这件事做成一个岗位：

1. **判定有出处**。风险由版本化规则表 + 法规检索算出，每条判定都能回答"命中哪条规则、依据哪条法条"。
2. **决策权在人**。低风险自动办结；中高风险必须经专员初审 + 主管复核两道签核，
   且决策与当前等待的节点强绑定（专员批不了主管节点，一个角色不能一步办结）。
3. **规则自己长出来**。人工驳回的理由生成规则提案 → 去重与冲突裁决 → 主管批准 → 版本化生效，下次同类条款自动命中。

---

## 二、功能一览

### 2.1 文档解析（含表格与扫描件）

支持 `.txt` / `.docx` / `.pdf`（上传或指定路径），输出条款切分、表格抽取（Markdown）与置信度。

| 形态 | 处理方式 |
|---|---|
| 原生文本 PDF / Word | 直接抽文本层 |
| 扫描件（整页位图、无文本层） | 自动转 OCR（RapidOCR，纯 CPU、模型随包分发） |
| 无 OCR 引擎可用时 | 标记低置信度 + 扫描件告警，转人工，不静默当文本 |

### 2.2 法规检索

中文 2-gram 检索，带文档频率过滤与"至少 2 个有区分度 token 重合"的门槛；
法规库 54 条，覆盖劳动用工、数据合规、金融销售、政府采购、建设工程等 15 个领域，可按领域过滤。
检索为空时显式声明，不给"看起来有依据"的结果。

> 法规条文来自公开整理，正式使用前建议由合规岗对照监管原文核验。

### 2.3 双层风险判定

| 层 | 依据 | 产出 |
|---|---|---|
| 规则层 | 版本化规则表（模式 + 等级 + 法条引用），9 条 | 风险等级（high / mid / low）+ 命中规则编号 |
| 引用层 | 法规检索 | 每条风险的法条出处；无引用的不判风险 |

整篇给出总体风险等级、风险条款数与规则表指纹，判定结果可复现、可比对。

> 规则是确定性匹配，不做否定语义理解 —— "本协议不设最终解释权条款"这类含否定词的正向表述可能被告警。
> 这是有意的：宁可让人多复核一次，也不放过真实违规。

### 2.4 人工签核闭环

```
提交 → 分诊 → 专员初审（确认 / 修改 / 驳回）→ 主管复核（通过 / 驳回）→ 归档
                  ↑ 修改：带意见回炉重新标注
```

- 按角色待办；原子认领（多人同时点，只有一次生效）
- 跨节点决策被拒（409 并说明当前等待的节点与角色），防伪造签核与单人一步办结
- 进程重启后自动扫描暂停中的流程，为缺失角色补建待办
- 修改由服务层重新标注并留痕

### 2.5 规则提案闭环

| 提案情形 | 系统行为 |
|---|---|
| 与现有规则模式完全相同 | 转「修订」，指向目标规则 |
| 与现有规则存在包含关系 | 提示相似 |
| 同模式但等级判断冲突 | 转「冲突，需主管裁决」，禁止直接写入 |
| 确实是新的 | 走「新增」；批准前不参与任何判定 |

批准后规则版本号 +1，记录批准人、时间与变更说明。

### 2.6 主动工作

- **例行巡检**：轮询合同来源（共享盘目录 / REST 接口），按内容哈希去重，自动发起审查并投递通知
- **故障降级**：来源不可达、脏数据、超时都有明确降级语义，不中断批次
- **自排跟进**：发现材料缺失时自动创建定时跟进任务，到期提醒

### 2.7 报告与证据链

- 报告双形态：Markdown + PDF
- 每个流程有完整事件序列（提交 → 待办 → 决策 → 归档），记录谁、何时、什么角色、什么决策
- 审计链只追加不修改；日志按 trace_id 串联

### 2.8 数字员工对话

除固定流程外，可直接提问：查规则、检索法条、解释某条判定的依据、总结某份合同的审查过程。
回答带规则编号与法条出处，并显示调用了哪些岗位工具。

> 回答供参考，对外使用前需人工确认。

### 2.9 工作台与权限

合同审查台、待办审批、运行概览、规则库、规则提案、审计台、指标、账号管理、配置、诊断、员工对话、实时日志流。
三档角色（专员 / 主管 / 管理员）× 11 项权限，切角色即可看到菜单与按钮差异。

---

## 三、架构

### 3.1 分层设计

```
工作台        浏览器（原生 HTML/JS 单文件）                    127.0.0.1:8790
   ↓
接口层        FastAPI + Uvicorn（认证 RBAC · 待办 · 规则 · 指标 · 审计 · 日志流 · 对话）
   ↓
服务层        编排器 · 账号与 SSO · 连接器 · 主动工作 · 可观测 · 员工对话
   ↓                                    ↘
能力层        岗位能力服务（15 个 MCP 工具）                  独立进程 :8770
   ↓            解析 / 表格 / 检索 / 标注 / 报告 / 规则提案 / 待办审计 / 指标
编排层        工作流（显式状态机：节点 · 条件路由 · 断点续接）
   ↓
底座          运行时框架（Agent · 技能 · 渠道 · 记忆 · 定时任务）
   ↓
数据层        SQLite（默认） ／ PostgreSQL 16 + pgvector
```

### 3.2 主流程

```
entry_probe ──→ specialist_review ──→ etl_gate ──→ supervisor_verify ──→ finalize
   （分诊）        （专员初审）          （汇合）        （主管复核）        （归档出报告）
                      ↑   │
                      └───┘  修改：带意见回炉重新标注
```

- 低风险：分诊后不生成待办，直接归档出报告
- 中风险：专员初审 → 主管复核；驳回转规则提案
- 高风险：初审可要求重新标注，回炉后重走流程

### 3.3 目录结构

```
compliance_officer/
├── mcp_server/          岗位能力服务（MCP，15 个工具）
│   ├── compliance_mcp_server.py   协议与传输层
│   ├── tools_registry.py          工具实现与注册
│   ├── contract_parser.py         条款切分 / 表格抽取
│   ├── ocr.py                     可插拔 OCR
│   ├── regulations_store.py       法规库与检索
│   ├── rules_store.py             规则表（版本化 · 去重 · 冲突裁决）
│   ├── queue_store.py             待办与审计
│   ├── runtime_store.py           运行态
│   └── db.py / _dbpath.py         双后端存储层（SQLite / PostgreSQL）
├── service/             业务服务层
│   ├── review_orchestrator.py     编排器
│   ├── auth.py / sso.py           账号 · 权限矩阵 · JWT · OIDC SSO
│   ├── connectors.py             合同来源与邮件连接器
│   ├── proactive_worker.py       例行巡检与自排跟进
│   ├── agent_runtime.py          员工对话
│   ├── observability.py          结构化日志与 trace
│   └── prod_config.py            配置校验
├── gateway/             接口层
│   ├── app.py                    FastAPI 路由
│   └── web/index.html            工作台界面
├── mock/                外部系统替身（OA + 邮箱 + 故障注入、OIDC IdP）
├── skills/              岗位技能包
├── workflows/           流程定义
├── rules/risk_rules.yaml 风险规则表
├── data/                法规库 / 评测集 / 规则提案
├── deploy/              PostgreSQL 初始化 SQL
├── scripts/             部署与运维工具
├── tests/               单元测试
└── docs/                产品文档
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

# ② 虚拟环境与依赖
uv venv .runtime\venv --python 3.13          # 没有 uv 就 python -m venv .runtime\venv
.\.runtime\venv\Scripts\python.exe -m pip install -r requirements.txt
pwsh -File scripts\d0\install-runtime.ps1

# ③ 配置大模型密钥（默认指向 DeepSeek，OpenAI 兼容协议）
pwsh -File scripts\deploy-runtime.ps1 -EnableMcp -LlmKeyFrom <你的.env>

# ④ 启动（能力服务 8770 / Mock 外部系统 8780 / 网关 8790）
pwsh -File scripts\trial-demo.ps1 -Reset

# ⑤ 停止
pwsh -File scripts\stop-demo.ps1
```

### 4.2 Docker Compose（可选）

```bash
git clone https://github.com/look378/compliance-officer.git
git clone https://github.com/nl8590687/lemonclaw.git      # 运行时底座，放在同级目录
cd compliance-officer

cp .env.enterprise.example .env      # 至少填 JWT_SECRET
export JWT_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")

docker compose up -d --build
```

| 入口 | 地址 |
|---|---|
| 工作台 | http://127.0.0.1:8790 |
| 岗位能力服务 | http://127.0.0.1:8770/health |
| Mock 外部系统 | http://127.0.0.1:8780/health |

> compose 会把底座以只读方式挂进容器（`${LEMONCLAW_HOME:-../lemonclaw-main}:/lemonclaw:ro`，
> 容器内 `LEMONCLAW_HOME=/lemonclaw`）。

### 4.3 打开工作台

浏览器访问 **http://127.0.0.1:8790** → 右上角「认证设置」→ 令牌填 `local-dev-token`，角色选 `specialist` → 保存。

演示模式用固定令牌模拟已登录角色，方便随时切换身份查看权限差异；生产用账号密码或企业单点登录（见第五节）。

---

## 五、配置说明

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `AUTH_MODE` | `demo` | `demo` 演示令牌 / `jwt` 账号密码 / `sso` 企业单点登录 |
| `JWT_SECRET` | — | `jwt`/`sso` 模式必填（长度 ≥16） |
| `DB_MODE` | `sqlite` | `sqlite` 单文件 / `pg` PostgreSQL（需 `DATABASE_URL`） |
| `DATABASE_URL` | — | 例：`postgresql://compliance:***@127.0.0.1:5432/compliance` |
| `CONTRACT_SOURCE` | `mock` | `mock` / `dir`（共享盘）/ `http`（OA 接口） |
| `MAIL_GATEWAY` | `mock` | `mock` / `none` / `smtp` / `http` |
| `OCR_ENGINE` | `auto` | `auto` / `paddle` / `rapidocr` / `tesseract` / `http` / `none` |
| `OCR_MAX_PAGES` / `OCR_DPI` | `30` / `200` | 单份文档 OCR 页数上限与渲染分辨率 |
| `OPENAI_BASE_URL` / `MODEL_NAME` | DeepSeek / `deepseek-chat` | 兼容任何 OpenAI 协议端点 |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_ROLE_MAP` | — | 单点登录配置，组到角色的映射支持点号路径 |
| `LOG_MODE` / `LOG_MAX_BYTES` | `console` / 8 MB | 结构化日志与轮转 |
| `HTTP_TRUST_ENV` | `false` | 在反向代理之后设 `true` 才采信 `X-Forwarded-For` |

完整样例与每一项取值含义见 **`.env.enterprise.example`**。取值写错会被配置校验拦下并拒绝启动。

**对接说明**

| 要对接的东西 | 怎么做 |
|---|---|
| 合同来源（OA / 合同库 / 共享盘） | `CONTRACT_SOURCE=dir\|http` + 字段映射，不改代码 → `docs/13` |
| 邮件通知 | `MAIL_GATEWAY=smtp\|http` → `docs/13` |
| 企业身份 | `AUTH_MODE=sso`，填贵司 OIDC 参数 → `docs/15` |
| 数据库 | `DB_MODE=pg`，本项目自有表落 PostgreSQL → `docs/16` |

---

## 六、技术栈

### 6.1 核心技术选型

| 层次 | 选型 | 版本 |
|---|---|---|
| 语言 | Python | 3.13.9 |
| 接口层 | FastAPI + Uvicorn + Starlette | 0.143 / 0.54 / 1.7 |
| 岗位能力通道 | MCP Streamable HTTP（JSON-RPC 2.0） | 2025-03-26 |
| 编排 | LangGraph | 1.2 |
| 运行时底座 | LemonClaw（Apache-2.0） | — |
| 大模型 | DeepSeek `deepseek-chat`（OpenAI 兼容） | — |
| 数据库 | SQLite（默认） / PostgreSQL | 内置 / 16.4 |
| PostgreSQL 驱动 | pg8000（兼容 psycopg3/2） | 1.31.5 |
| 向量库 | pgvector（已建表，按需接入） | pg16 |
| 检索 | 中文 2-gram（自研） | — |
| 文档解析 | pdfplumber / pypdf / python-docx | 0.11.10 / 6.20 / 1.2 |
| OCR | RapidOCR（onnxruntime） | 1.2.3 |
| PDF 渲染 | pypdfium2 | 5.14.0 |
| 报告 | reportlab | 5.0.1 |
| 认证 | PyJWT + PBKDF2-HMAC-SHA256（20 万次迭代） | 2.15.1 |
| 企业单点登录 | OIDC Authorization Code + PKCE | — |
| 界面 | 原生 HTML/CSS/JS 单文件（无前端框架、无 CDN） | — |
| 测试 | pytest + httpx | 9.1.1 / 0.28.1 |

### 6.2 部署形态

- **本机形态**：`.runtime/venv` + SQLite，无任何外部服务
- **交付形态**：`docker compose`，四服务（能力服务 / Mock 外部系统 / 网关 / PostgreSQL），均带健康检查
- **外部系统**：合同来源与邮件通道连接器化，切换只改环境变量与字段映射

---

## 七、文档索引

| 文档 | 内容 |
|---|---|
| `docs/11-使用说明书.md` | 快速上手、界面操作、接口调用、运维排障、企业就绪度清单 |
| `docs/13-企业系统对接契约.md` | **给 IT**：OA / 合同库 / 邮件三种接入方式与字段映射 |
| `docs/15-企业SSO对接.md` | **给身份管理员**：OIDC 配置、IdP 登记要点、验收步骤 |
| `docs/16-PostgreSQL部署.md` | **给 DBA/运维**：存储边界、部署与验收 |
| `docs/19-技术栈清单.md` | 完整技术栈、精确版本、端口拓扑 |
| `docs/20-试用指引.md` | 四条场景的点击路径与预期结果 |
| `docs/10-P7评测与交付报告.md` | 验收清单与评测数据 |
| `docs/08-架构说明.md` | 系统边界与设计说明 |

---

## License

[MIT](LICENSE)
