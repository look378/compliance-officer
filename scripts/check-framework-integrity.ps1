#!/usr/bin/env pwsh
# 框架源码零改动证据脚本
#
# 做法：对框架**全部源码**（排除 .git 与运行时数据目录 .lemonclaw）做 SHA256 清单，
#       生成基线快照；后续任何时刻重跑并比对，即可证明"未改动框架源码"。
#
# 用法：
#   pwsh -File scripts/check-framework-integrity.ps1                 # 生成/刷新基线
#   pwsh -File scripts/check-framework-integrity.ps1 -Verify         # 与基线比对
param(
    [switch]$Verify
)
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
# 框架位置：优先 $env:LEMONCLAW_HOME，默认取本项目目录的**同级兄弟目录** lemonclaw-main
# （基线文件里存的是**相对路径 + SHA256**，因此换机器/换盘符都不影响校验）
$Framework   = if ($env:LEMONCLAW_HOME) { $env:LEMONCLAW_HOME }
               else { Join-Path (Split-Path -Parent $ProjectRoot) 'lemonclaw-main' }
$EvidenceDir = Join-Path $ProjectRoot 'docs\evidence'
$Baseline    = Join-Path $EvidenceDir 'framework-source-baseline.json'
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

# 排除项说明：
#   .git            版本库元数据
#   .lemonclaw      框架自身的配置/数据目录（占位文件），本项目的部署挂载点，非源码
$exclude = @('\\\.git\\', '\\\.lemonclaw\\', '\\\.venv\\', '\\__pycache__\\', '\\\.pytest_cache\\')

function Get-FileList {
    Get-ChildItem $Framework -Recurse -File -Force | Where-Object {
        $p = $_.FullName
        -not ($exclude | Where-Object { $p -match $_ })
    }
}

$entries = @{}
foreach ($f in Get-FileList) {
    $rel = $f.FullName.Substring($Framework.Length).TrimStart('\')
    $entries[$rel] = (Get-FileHash $f.FullName -Algorithm SHA256).Hash
}

$snapshot = [ordered]@{
    framework      = $Framework
    generated_at   = (Get-Date -Format s)
    file_count     = $entries.Count
    total_bytes    = (Get-FileList | Measure-Object -Property Length -Sum).Sum
    excluded       = $exclude
    files          = $entries
}

if (-not $Verify) {
    $snapshot | ConvertTo-Json -Depth 4 | Set-Content -Path $Baseline -Encoding utf8
    "[integrity] 已生成基线：$($entries.Count) 个文件 -> $Baseline"
    exit 0
}

if (-not (Test-Path $Baseline)) { throw "基线不存在，请先不带 -Verify 运行一次：$Baseline" }
$base = Get-Content $Baseline -Raw -Encoding utf8 | ConvertFrom-Json
$baseFiles = @{}
$base.files.PSObject.Properties | ForEach-Object { $baseFiles[$_.Name] = $_.Value }

$added, $removed, $changed = @(), @(), @()
foreach ($k in $entries.Keys) {
    if (-not $baseFiles.ContainsKey($k)) { $added += $k }
    elseif ($baseFiles[$k] -ne $entries[$k]) { $changed += $k }
}
foreach ($k in $baseFiles.Keys) { if (-not $entries.ContainsKey($k)) { $removed += $k } }

$result = [ordered]@{
    verified_at    = (Get-Date -Format s)
    baseline_at    = $base.generated_at
    baseline_count = $baseFiles.Count
    current_count  = $entries.Count
    added          = $added
    removed        = $removed
    changed        = $changed
    unchanged      = ($added.Count -eq 0 -and $removed.Count -eq 0 -and $changed.Count -eq 0)
}
$out = Join-Path $EvidenceDir 'framework-integrity-verify.json'
$result | ConvertTo-Json -Depth 4 | Set-Content -Path $out -Encoding utf8

if ($result.unchanged) {
    "[integrity] 通过：框架源码与基线完全一致（$($entries.Count) 个文件）"
    "[integrity] 证据：$out"
    exit 0
} else {
    "[integrity] 发现差异！added=$($added.Count) removed=$($removed.Count) changed=$($changed.Count)"
    $changed | Select-Object -First 10 | ForEach-Object { "   changed: $_" }
    "[integrity] 证据：$out"
    exit 1
}
