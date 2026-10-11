# 合规官「小合」 · Compliance Officer

> 一个岗位化的智能合规数字员工：接收合同 → 解析条款 → 检索法条 → 标注风险 → 人工签核 → 出具审查报告 → 沉淀规则。
>
> 定位：**低频高价值文档审查**（合规审查日均 < 100 份、审批角色 ≤ 10 人）。

---

## 项目名与目录

| 项 | 值 |
|---|---|
| 项目名（中文） | 合规官「小合」 |
| 项目名（英文 / 仓库名） | `compliance_officer` |
| 岗位角色 | 合规专员数字员工（初审）+ 合规主管（高风险复核、规则提案审批） |
| 目录 | `项目三/compliance_officer/` |

> 命名说明：与工作区既有仓库风格保持一致（项目一 `belle_ai` / `flowsell_ai`，项目二 `saas-support-cs`，均为英文 slug）。
> 备选名：`compliance_employee`（与任务书交付物清单同名）、`hegui_ai`。如需改名，仅需重命名目录与全文替换，成本可忽略。

---

## 架构边界（本项目的第一原则）

本项目**不改动任何第三方运行时框架的源码**。所有岗位能力以**独立进程 + 配置接入**的方式挂载：

```
compliance_officer/                 # 本项目（全部自有代码）
├── mcp_server/                      # 岗位能力以 MCP 工具形式对外暴露（自有进程）
├── skills/                          # 岗位 Skill 包（说明书 + 确定性脚本）
├── workflows/                       # 审查主流程 / 规则提案流程的声明式 spec
├── rules/risk_rules.yaml            # 风险规则表（唯一真相源，版本化）
├── data/regulations/                # 法规与内控条款数据集
├── gateway/                         # 认证、待办队列、报告与指标接口
├── scripts/                         # 部署与一键拉起脚本
├── tests/
└── docs/

<外部运行时框架>                       # 仅通过配置文件引用，源码零改动
├── .lemonclaw/skills/<name>-<version>/   ← 由本项目 scripts 部署写入
├── .lemonclaw/mcp.json                   ← 指向本项目 mcp_server
└── .lemonclaw/.env                       ← LLM / 渠道 / 开关
```

**为什么这样切**：岗位化改造的差异化来自「岗位知识 + 证据链 + 评测」，不来自改框架。零改动同时保住了跟随上游更新的能力，并让"未重写框架"成为可验证的事实（对框架目录做 diff/哈希即可留证）。

---

## 快速开始（从零开始，约 10 分钟）

> **本仓库不包含第三方运行时框架的源码**（`lemonclaw-main` 是 Apache-2.0 的开源项目，
> 按许可证要求以"外部依赖"方式引用，而非复制进本仓库）。请按下面前两步把它就位。

```powershell
# 0) 目录约定：框架与本项目**并排**放在同一个工作目录下
#    <workspace>\
#      ├── lemonclaw-main\        ← 第三方运行时框架（clone 得到）
#      └── compliance_officer\    ← 本仓库

# 1) 取回运行时框架（与本项目同级）
git clone https://github.com/nl8590687/lemonclaw.git ..\lemonclaw-main
#    放到别处也行，用环境变量指路即可： $env:LEMONCLAW_HOME = 'D:\path\to\lemonclaw-main'

# 2) 建虚拟环境 + 装依赖（本项目依赖 + 框架依赖）
uv venv .runtime\venv --python 3.13        # 没有 uv 就改用 python -m venv .runtime\venv
.\.runtime\venv\Scripts\python.exe -m pip install -r requirements.txt
pwsh -File scripts\d0\install-runtime.ps1  # 装框架依赖

# 3) 配置大模型密钥（写入框架侧 .env；也可直接用 .env.enterprise.example 作模板）
pwsh -File scripts\deploy-runtime.ps1 -EnableMcp -LlmKeyFrom <你的.env>   # 或手工填 .lemonclaw\.env
#    最少需要：OPENAI_BASE_URL / OPENAI_API_KEY / MODEL_NAME（默认指向 DeepSeek）

# 4) 一键准备试用环境：起服务 + 跑三组可用性自检（跑完服务保持运行）
pwsh -File scripts\trial-demo.ps1 -Reset
#    然后打开 http://127.0.0.1:8790，右上角「认证设置」令牌填 local-dev-token，角色选 specialist

# 停止服务
pwsh -File scripts\stop-demo.ps1
```

分步说明与"进去点哪儿、应该看到什么"：**`docs/20-试用指引.md`**。

---

## 当前状态

| 阶段 | 状态 |
|---|---|
| 材料研读（运行时框架核心代码 + 任务书全文） | ✅ 已完成 |
| 项目立项 / 命名 / 边界冻结 | ✅ 已确认 |
| 方案设计说明 | ✅ 已定稿（`docs/01-方案设计说明.md`） |
| **P1 D0 门禁验证** | ✅ **已完成**：8 项通过 + 1 项已定档降级（`docs/02-D0门禁报告.md`） |
| **P2 岗位能力服务** | ✅ **已完成**：15 个 MCP 工具 / 54 条法规 / 50 条单测 / Mock 故障注入（`docs/03-P2岗位能力服务报告.md`） |
| **P3 主流程编排** | ✅ **已完成**：4 条流程路径全通（低风险自动办结 / 中风险双人审 / 高风险改单循环 / 驳回转提案）（`docs/04-P3主流程报告.md`） |
| **P4 人工闭环服务端化** | ✅ **已完成**：编排器 + 待办闭环 + 原子认领（线程/跨进程并发各只生效一次）+ 启动补偿（`docs/05-P4人工闭环报告.md`） |
| **P5 规则提案闭环** | ✅ **已完成**：驳回→提案（三层去重/冲突裁决）→主管审批→版本化写入→再次命中（`docs/07-P5规则提案报告.md`） |
| **P6 主动工作** | ✅ **已完成**：例行巡检（幂等/降级/批次健壮）+ 自排跟进任务（`docs/06-P6主动工作报告.md`） |
| **P7 评测与交付** | ✅ **已完成**：25 份合同评测（召回 1.00 / 误报 0）/ 轻量网关 / docker-compose / 架构说明 / 演示剧本（`docs/10-P7评测与交付报告.md`） |
| **P8 全量代码审查** | ✅ **已完成**：60 项真实缺陷全部修复并回归（`docs/12-代码审查报告.md`） |
| **真实组件落地** | ✅ **已完成**：真 PostgreSQL 16.4（18/18）、真 OIDC SSO（20/20）、真 OCR 引擎（RapidOCR）（`docs/17-本机真实验证记录.md`） |
| **第二次（增量）审查** | ✅ **已完成**：1 项 P0 + 26 项 P1 修复并复验；重点在"验证本身是否可信"（`docs/18-第二次审查报告.md`） |
| **交付准备** | ✅ 技术栈清单 `docs/19`、试用指引 `docs/20`、单测 166 条、静态审查 0 问题 |

**项目状态：P1–P7 全部完成，10 项验收清单全部兑现；其后完成两轮代码审查与真实组件验证。**

**质量数据（25 份合同 / 81 条条款）**：风险召回 **1.00**（0 漏报）、误报 **0 处**、等级一致性 **1.00**、
类型识别 **0.96**、表格抽取单元格正确率 **100%**（11 例含表格）、人工介入率 **0.76**。

**已跑通的主链路（真实 LLM + 真实工作流）**：
自建 MCP 岗位能力服务（15 工具）→ 框架原生 `mcp__compliance__*` 工具（主 Agent 可用工具 36 → 51）→
合规审查主流程（分诊 / 专员初审 / 主管复核 / 改单重标注 / 驳回转规则提案）→
跨进程断点恢复 → 并发恢复互斥（线程与跨进程各只生效一次）→ 规则标注质量基线 →
规则提案（三层去重 + 冲突裁决 + 版本化写入）→ 统一待办与证据链 →
Mock 外部系统故障注入 → 主动巡检与自排跟进 → 轻量网关（认证/RBAC/待办/SSE）。

**能力清单（MCP 工具）**：`parse_contract` `extract_tables` `search_regulations` `annotate_clause`
`annotate_document` `generate_report` `propose_rule` `list_rule_proposals` `approve_rule_proposal`
`reject_rule_proposal` `record_decision` `list_pending` `list_audit` `metrics` `service_info`

**当前 OCR 档位**：**`ocr_lite` —— 已接真实 OCR 引擎**。扫描版 PDF（整页位图、无文本层）能被真实识别出
中文条款并命中风险（引擎 RapidOCR / onnxruntime，纯 CPU、模型随包分发、无需联网下载）；
未安装 OCR 引擎时自动降级为"扫描件标记低置信度转人工"，不会静默当成原生文本。
引擎优先级 `PaddleOCR → RapidOCR → Tesseract → HTTP OCR 服务 → 无`，由 `OCR_ENGINE=auto` 自动挑选，
内网已有 OCR 中台时可直接 `OCR_ENGINE=http` 指向它。

**数据库形态**：默认 SQLite 单文件开箱即用；`DB_MODE=pg` 时本项目自有表（待办/审计/账号/令牌撤销/登录退避）
真正落 PostgreSQL（已在本机真实 PostgreSQL 16.4 上端到端验证 18/18）。框架侧工作流运行态仍由框架 DAO 写 SQLite
（不改框架源码的必然结果，边界见 `docs/16-PostgreSQL部署.md`）。

### 常用命令

```powershell
# 一键准备试用环境（起服务 + 三组可用性自检；-Reset 先清掉自检遗留数据）
pwsh -File scripts\trial-demo.ps1 -Reset
pwsh -File scripts\stop-demo.ps1

# 部署运行时平台（技能包 + MCP 配置 + .env）
# 框架位置：优先 $env:LEMONCLAW_HOME，默认取本项目同级目录 lemonclaw-main
pwsh -File scripts/deploy-runtime.ps1 -EnableMcp

# 启动岗位能力服务（MCP Streamable HTTP，端口 8770）
.\.runtime\venv\Scripts\python.exe mcp_server\compliance_mcp_server.py --port 8770

# 启动 Mock 外部系统（OA + 邮箱 + 故障注入，端口 8780）
.\.runtime\venv\Scripts\python.exe mock\mock_office.py --port 8780

# 一键执行全部门禁（会真实调用 DeepSeek）
pwsh -File scripts/d0/run-all-gates.ps1

# P2 自检与单元测试
.\.runtime\venv\Scripts\python.exe scripts\d0\p2_static_check.py
.\.runtime\venv\Scripts\python.exe scripts\d0\p2_tools_e2e.py
.\.runtime\venv\Scripts\python.exe scripts\d0\p2_mock_faults.py
.\.runtime\venv\Scripts\python.exe -m pytest

# 证明"未改动运行时框架源码"（SHA256 清单比对）
pwsh -File scripts/check-framework-integrity.ps1 -Verify

# 推送前密钥扫描（只扫"会被推上去的内容"：git 索引；0 命中才适合 push）
.\.runtime\venv\Scripts\python.exe scripts\tools\secret_scan.py

# 运行态观察与清理
.\.runtime\venv\Scripts\python.exe scripts\tools\status_overview.py
.\.runtime\venv\Scripts\python.exe scripts\tools\cleanup_runs.py --status paused --dry-run
.\.runtime\venv\Scripts\python.exe scripts\tools\cleanup_runs.py --trial-reset   # 试用/演示前重置

# P3/P4 端到端
.\.runtime\venv\Scripts\python.exe scripts\d0\p3_review_flow.py
.\.runtime\venv\Scripts\python.exe scripts\d0\p4_human_loop.py

# P5/P6/P7 端到端
.\.runtime\venv\Scripts\python.exe scripts\d0\p5_rule_proposal.py
.\.runtime\venv\Scripts\python.exe scripts\d0\p6_proactive.py
.\.runtime\venv\Scripts\python.exe scripts\d0\p7_eval.py
.\.runtime\venv\Scripts\python.exe scripts\d0\p7_gateway.py

# 交付物校验（docker-compose 语法与结构）
.\.runtime\venv\Scripts\python.exe scripts\tools\check_compose.py
```

文档索引：

- `docs/00-项目立项说明.md` — 立项、命名、边界、决策
- `docs/01-方案设计说明.md` — 架构、能力契约、分阶段计划、容错矩阵、25 条证据索引
- `docs/02-D0门禁报告.md` — 门禁结论、环境事实、对后续阶段的具体输入
- `docs/03-P2岗位能力服务报告.md` — P2 交付、自检发现并修正的 12 个真实问题、对 P3–P7 的输入
- `docs/04-P3主流程报告.md` — P3 主流程与图拓扑纪律
- `docs/05-P4人工闭环报告.md` — 编排器、待办闭环、并发互斥、启动补偿
- `docs/06-P6主动工作报告.md` — 例行巡检与自排跟进
- `docs/07-P5规则提案报告.md` — 规则提案红线与版本化
- `docs/08-架构说明.md` — 系统边界、五要素、8 条框架硬约束
- `docs/09-演示剧本.md` — 现场演示步骤与追问回答要点
- `docs/10-P7评测与交付报告.md` — 评测数据、网关、交付物、验收清单
- `docs/11-使用说明书.md` — **使用说明书**：快速上手、界面操作、接口、运维排障、企业就绪度清单
- `docs/12-代码审查报告.md` — 第一轮全量审查：60 项缺陷与修复、回归结论
- `docs/13-企业系统对接契约.md` — **给 IT 的对接契约**：OA / 合同库 / 邮件三种接入方式与字段映射
- `docs/15-企业SSO对接.md` — **给身份管理员**：OIDC 配置、IdP 登记要点、验收步骤
- `docs/16-PostgreSQL部署.md` — **给 DBA/运维**：迁移边界、部署与验收
- `docs/17-本机真实验证记录.md` — 真 PostgreSQL / 真 OCR / 真 OIDC 的复现步骤与结论
- `docs/18-第二次审查报告.md` — 增量审查：P0/P1 明细、修复验证、"有意不改"清单
- `docs/19-技术栈清单.md` — **完整技术栈**：分层清单、精确依赖版本、端口拓扑、选型理由
- `docs/20-试用指引.md` — **试用指引**：四条场景的点击路径与"应该看到什么"
- `docs/evidence/` — 全部门禁与自检的原始证据（JSON，28 个）

---

## 已知边界（本仓库**未**验证的部分）

诚实说明，避免误导（逐条理由见 `docs/18-第二次审查报告.md` §6）：

1. **真实企业 OA / 合同库 / 邮件网关**：连接器已就绪（`CONTRACT_SOURCE=dir|http`、`MAIL_GATEWAY=http|smtp`），
   但**没有对接过任何真实企业系统的现场字段名与鉴权**；
2. **贵司 IdP 现场联调**：SSO 已用标准 OIDC 实现的本地 IdP 跑通（真 RSA / 真 PKCE / 20 项断言），
   但未接过 Keycloak / Azure AD / Okta 实例；
3. **多实例部署**：当前是单实例（SSO 一次性 state 在进程内存；框架侧运行态共享未做）；
4. **Docker 容器未真跑**：`docker-compose.yml` 只做了配置级校验（本机无 Docker daemon）。

以上都属于"需要目标环境才能做"的事，不是已知缺陷。

---

## 术语与纪律

- 对外口径：本项目为**独立项目**；仅在技术实现章节说明所依赖的开源运行时框架，并保留其许可证与声明。
- 所有对外系统（OA、邮箱、合同库）以 mock 形式提供，代码内标注 `TODO[生产对接]`。
- 中文注释、中文文档。

## 第三方依赖与许可

本项目**不包含**任何第三方框架源码；以下依赖以"外部包 / 外部进程"方式引用：

| 依赖 | 许可 | 说明 |
|---|---|---|
| [LemonClaw](https://github.com/nl8590687/lemonclaw) (`lemonclaw-main`) | **Apache License 2.0** | 运行时底座（Agent / 工作流 / 技能 / 渠道）。按本仓库纪律**源码零改动**，并以 SHA256 基线（110 个文件）证明；需自行 clone（见「快速开始」） |
| Python 第三方包 | 各自许可 | 见 `requirements.txt`（FastAPI / LangChain / LangGraph / pdfplumber / reportlab / PyJWT 等） |
| PostgreSQL / pgvector / Docker 基础镜像 | PostgreSQL License / Apache-2.0 等 | 仅交付形态使用，见 `docker-compose.yml` |

本项目自身代码采用 **MIT 许可**（见 `LICENSE`）。
