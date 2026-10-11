#!/usr/bin/env pwsh
# 试用/演示 一键准备（起服务 → 跑三组可用性自检 → 留在运行状态并打印试用指引）
#
# 用法：
#   pwsh -File scripts/trial-demo.ps1                 # 直接准备（复用已在跑的服务）
#   pwsh -File scripts/trial-demo.ps1 -Reset          # 先把自检遗留数据清干净（推荐首次使用）
#   pwsh -File scripts/trial-demo.ps1 -SkipChecks     # 只起服务，不跑自检（最快）
#
# 跑完**服务保持运行**，你可以直接打开浏览器 http://127.0.0.1:8790 开始点。
# 停止：pwsh -File scripts/stop-demo.ps1
param(
    [switch]$Reset,
    [switch]$SkipChecks
)
$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Py          = Join-Path $ProjectRoot '.runtime\venv\Scripts\python.exe'
$env:PYTHONPATH = $ProjectRoot

if (-not (Test-Path $Py)) { throw "运行时 venv 不存在：$Py（先跑 scripts/d0/install-runtime.ps1）" }

$failed = 0
$results = [ordered]@{}

function Run-Step([string]$name, [scriptblock]$body) {
    Write-Host "`n================ $name ================" -ForegroundColor Cyan
    & $body
    $rc = $LASTEXITCODE
    $results[$name] = if ($rc -eq 0) { 'PASS' } else { "FAIL(exit=$rc)" }
    if ($rc -ne 0) { $failed++ }
}

# ---------- 0) 可选：清掉自检遗留（待办/在办 run/测试账号）----------
if ($Reset) {
    Run-Step '0 试用前重置（清理自检遗留数据）' {
        & $Py (Join-Path $ProjectRoot 'scripts\tools\cleanup_runs.py') --trial-reset
    }
}

# ---------- 1) 起服务（能力服务 + Mock 外部系统 + 网关）----------
Run-Step '1 启动演示环境' {
    & pwsh -NoProfile -File (Join-Path $ProjectRoot 'scripts\start-demo.ps1')
}

if ($failed -gt 0) {
    Write-Host "`n[trial] 服务没起来，后面的自检没有意义，先看 .runtime\logs\*.err.log" -ForegroundColor Red
    exit 1
}

if (-not $SkipChecks) {
    # ---------- 2) 业务流自检（提交 → 待办 → 双签 → RBAC → 指标 → SSE）----------
    Run-Step '2 业务流自检（P7 网关 12 项）' {
        & $Py (Join-Path $ProjectRoot 'scripts\d0\p7_gateway.py')
    }
    # ---------- 3) 工作台可用性自检（页面 / 接口 / 上传 / 权限）----------
    Run-Step '3 工作台可用性自检（30 项）' {
        & $Py (Join-Path $ProjectRoot 'scripts\d0\p7_webui.py')
    }
    # ---------- 4) 身份 / 账号 / 配置 / 日志 / 数字员工对话（真实 LLM）----------
    Run-Step '4 生产化自检（含数字员工对话，真实 LLM）' {
        & $Py (Join-Path $ProjectRoot 'scripts\d0\p7_production.py')
    }
}

Write-Host "`n================ 试用准备汇总 ================" -ForegroundColor Cyan
$results.GetEnumerator() | ForEach-Object { "  {0,-42} {1}" -f $_.Key, $_.Value }

Write-Host ""
if ($failed -gt 0) {
    Write-Host "[trial] 有 $failed 步没通过：服务仍在运行，可照常打开页面，但请把上面的失败项发我。" -ForegroundColor Yellow
} else {
    Write-Host "[trial] 全部通过 —— 可以直接开始试用。" -ForegroundColor Green
}

Write-Host @"

================ 现在这样试 ================
1) 打开浏览器： http://127.0.0.1:8790
2) 右上角「认证设置」→ 令牌填 local-dev-token，角色选 specialist → 保存
   （演示模式：换角色只需改这里，不需要账号密码）
3) 按 4 个场景点：
   ① 合同审查台 → 点「低风险示例」→ 提交 → 应显示 completed / low（不惊动任何人）
   ② 合同审查台 → 点「中风险示例」→ 提交 → 应停在「合规专员」待办
      → 待办审批（角色 specialist）→ 认领 → 确认
      → 把角色切成 supervisor → 待办审批 → 通过 → 归档
   ③ 规则提案 → 用专员角色提一条 → 切 supervisor → 批准 → 回「规则库」看版本号 +1
   ④ 员工对话 → 问「最终解释权为什么有风险，依据哪条法条」→ 应答带法条/规则编号
   另有：运行概览（每次审查的完整证据链、SSE 实时事件）、审计台（谁在什么时候做了什么）、
         指标（待办/决策分布/规则指纹）、诊断（数据库/连接器/OCR/规则表健康）

细节步骤与"应该看到什么"见： docs\20-试用指引.md
停止服务： pwsh -File scripts\stop-demo.ps1
试用后想回到干净状态： .\.runtime\venv\Scripts\python.exe scripts\tools\cleanup_runs.py --trial-reset
"@
exit $failed
