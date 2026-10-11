# D0 门禁一键执行（P1）
#
# 顺序执行 G1..G7，逐项打印判定；不因单项失败而中断，最后汇总退出码。
# 用法： pwsh -File scripts/d0/run-all-gates.ps1 [-SkipLlm]
param(
    [switch]$SkipLlm
)
$ErrorActionPreference = 'Continue'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Runtime     = Join-Path $ProjectRoot '.runtime'
$Py          = Join-Path $Runtime 'venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path $Py)) { throw "运行时 venv 不存在：$Py（先跑 scripts/d0/install-runtime.ps1）" }

$results = [ordered]@{}

function Run-Gate([string]$name, [scriptblock]$body) {
    Write-Host "`n================ $name ================" -ForegroundColor Cyan
    & $body
    $results[$name] = if ($LASTEXITCODE -eq 0) { 'PASS' } else { "FAIL(exit=$LASTEXITCODE)" }
}

Run-Gate 'G1 主 Agent 对话（真实 LLM）' {
    if ($SkipLlm) { Write-Host '[skip] -SkipLlm'; $global:LASTEXITCODE = 0; return }
    & $Py (Join-Path $ProjectRoot 'scripts\d0\g1_agent_smoke.py')
}
Run-Gate 'G2 工作流程图形态' { & $Py (Join-Path $ProjectRoot 'scripts\d0\g2_graph_shape.py') }
Run-Gate 'G3 断点续接（三进程）' {
    & $Py (Join-Path $ProjectRoot 'scripts\d0\g3_restart.py') --phase start | Out-Null
    $ev = Get-Content (Join-Path $ProjectRoot 'docs\evidence\d0-g3-restart.json') -Raw -Encoding utf8 | ConvertFrom-Json
    & $Py (Join-Path $ProjectRoot 'scripts\d0\g3_restart.py') --phase resume --run-id $ev.phase_start.run_id
    & $Py (Join-Path $ProjectRoot 'scripts\d0\g3_restart.py') --phase verify --run-id $ev.phase_start.run_id
}
Run-Gate 'G4 岗位能力 MCP 通道' { & $Py (Join-Path $ProjectRoot 'scripts\d0\g4_mcp.py') --mode check }
Run-Gate 'G4b 技能确定性脚本通道' {
    # 说明：技能依赖安装由框架用管道捕获子进程输出，在受限环境下需要更宽权限
    & $Py (Join-Path $ProjectRoot 'scripts\d0\g4b_skill_script.py')
}
Run-Gate 'G5 解析与 OCR 档位' { & $Py (Join-Path $ProjectRoot 'scripts\d0\g5_ocr_tier.py') }
Run-Gate 'G6 并发恢复互斥' { & $Py (Join-Path $ProjectRoot 'scripts\d0\g6_concurrency.py') }
Run-Gate 'G7 标注质量基线' { & $Py (Join-Path $ProjectRoot 'scripts\d0\g7_eval_baseline.py') }
# 【审查修正后新增】P4+ 决策-节点绑定回归：验证"跨节点审批被拒"与 update_state 语义
# （对应审查发现的两项 P0：伪造主管签核、单人一步办结跳过复核）
Run-Gate 'P4b 决策与节点绑定' { & $Py (Join-Path $ProjectRoot 'scripts\d0\p4b_decision_binding.py') }
Run-Gate 'INTEGRITY 底座完整性' {
    & pwsh -NoProfile -File (Join-Path $ProjectRoot 'scripts\check-framework-integrity.ps1') -Verify
}

Write-Host "`n================ D0 门禁汇总 ================" -ForegroundColor Cyan
$results.GetEnumerator() | ForEach-Object { "  {0,-32} {1}" -f $_.Key, $_.Value }
$failed = ($results.Values | Where-Object { $_ -ne 'PASS' }).Count
$summary = [ordered]@{
    run_at  = (Get-Date -Format s)
    results = $results
    failed  = $failed
} 
$summary | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $ProjectRoot 'docs\evidence\d0-summary.json') -Encoding utf8
Write-Host "`n汇总证据：docs/evidence/d0-summary.json" -ForegroundColor Cyan
exit $failed
