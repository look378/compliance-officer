#!/usr/bin/env pwsh
# 一键启动本机演示环境（岗位能力服务 + Mock 外部系统 + 轻量网关）
#
# 说明：三个服务以独立进程启动，日志写入 .runtime/logs/，PID 记录在 .runtime/pids/。
#       启动后脚本会做健康自检；任一服务不健康时以非 0 退出码结束（CI/自动化可据此判断）。
#
# 用法：
#   pwsh -File scripts/start-demo.ps1            # 启动（已在跑的会跳过）
#   pwsh -File scripts/start-demo.ps1 -Force     # 先清理本项目的旧进程再启动
param(
    [switch]$Force
)
$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runtime     = Join-Path $ProjectRoot '.runtime'
$Py          = Join-Path $Runtime 'venv\Scripts\python.exe'
$LogDir      = Join-Path $Runtime 'logs'
$PidDir      = Join-Path $Runtime 'pids'

if (-not (Test-Path $Py)) { throw "运行时 venv 不存在：$Py（先跑 scripts/d0/install-runtime.ps1）" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONPATH = $ProjectRoot

$services = @(
    @{ name = 'capability'; port = 8770; script = 'mcp_server\compliance_mcp_server.py'; args = @('--port','8770') },
    @{ name = 'mock';       port = 8780; script = 'mock\mock_office.py';               args = @('--port','8780','--timeout-sleep','3') },
    @{ name = 'gateway';    port = 8790; script = 'gateway\app.py';                    args = @('--port','8790') }
)

function Test-Health([int]$port) {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$port/health" -TimeoutSec 3 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch { return $false }
}

function Test-Ours([int]$procId) {
    # 归属判断：可执行文件在项目 venv 内，或命令行提到项目路径。
    # 只按 CommandLine 判断会漏掉"用 venv python + 相对脚本路径"启动的进程。
    try {
        $ci = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction Stop
        if ($ci -and $ci.ExecutablePath -and $ci.ExecutablePath -like "*$ProjectRoot*") { return $true }
        return [bool]($ci -and $ci.CommandLine -and $ci.CommandLine -like "*$ProjectRoot*")
    } catch { return $false }
}

# 【审查修正 P2】端口占用判定改用 netstat -ano 解析：
# 原实现依赖 Get-NetTCPConnection（NetTCPIP 模块），模块缺失时命令发现阶段就报
# CommandNotFound，-ErrorAction SilentlyContinue 抑制不住，$procId 为 $null，
# 随后的"是否已在运行"判断全部失真。
function Get-PortPid([int]$port) {
    $line = netstat -ano | Select-String ":$port\s+.*LISTENING" | Select-Object -First 1
    if (-not $line) { return $null }
    $candidate = [int](($line.ToString() -split '\s+')[-1])
    if (Test-Ours $candidate) { return $candidate }
    return -$candidate        # 负数表示"端口被别人占用"
}

$failed = 0
foreach ($s in $services) {
    $found = Get-PortPid $s.port
    if ($found -and $found -gt 0) {
        # 端口上已有本项目进程：以 /health 为准判断"是否真的可用"
        if (Test-Health $s.port) {
            "[start] $($s.name) 已在运行（端口 $($s.port)，PID $found，健康检查通过）"
        } else {
            "[start] 警告：端口 $($s.port) 上有本项目进程 PID=$found 但 /health 不健康，建议 -Force 重启"
            $failed++
        }
        continue
    }
    if ($found -and $found -lt 0) {
        # 端口被别人占用，但若健康检查通过（例如已有 mock 实例在跑）仍算可用
        if (Test-Health $s.port) {
            "[start] $($s.name) 端口 $($s.port) 由外部进程 PID $([math]::Abs($found)) 提供服务，健康检查通过"
        } else {
            "[start] 端口 $($s.port) 被**非本项目**进程占用（PID $([math]::Abs($found))）：请先释放该端口"
            $failed++
        }
        continue
    }
    if ($Force) { "（-Force 无旧进程可清理：端口 $($s.port) 空闲）" }

    $out = Join-Path $LogDir "$($s.name).out.log"
    $err = Join-Path $LogDir "$($s.name).err.log"
    # 参数逐个加引号，避免脚本路径含空格时被拆断
    $argList = @("`"$($s.script)`"") + ($s.args | ForEach-Object { "`"$_`"" })
    $proc = Start-Process -FilePath $Py `
        -ArgumentList $argList `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err `
        -PassThru
    New-Item -ItemType Directory -Force -Path $PidDir | Out-Null
    Set-Content -Path (Join-Path $PidDir "$($s.name).pid") -Value $proc.Id -Encoding ascii

    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 400
        if (Test-Health $s.port) { $ok = $true; break }
    }
    "[start] $($s.name) 端口 $($s.port)：$(if ($ok) { '健康' } else { "启动失败，见 $err" })"
    if (-not $ok) { $failed++ }
}

"`n=== 健康检查 ==="
foreach ($s in $services) {
    $h = if (Test-Health $s.port) { 'OK' } else { 'DOWN' }
    "  {0,-12} http://127.0.0.1:{1}  {2}" -f $s.name, $s.port, $h
}

if ($failed -gt 0) {
    "`n[start] $failed 个服务异常，请查看 .runtime/logs/*.err.log"
    exit 1
}

"`n=== 打开工作台 ==="
"  浏览器访问： http://127.0.0.1:8790"
"  演示模式登录：点击右上角「认证设置」，令牌填 local-dev-token，角色选 specialist/supervisor/admin"

"`n=== 现在可以直接测（复制以下命令）==="
@'
# 1) 看系统全貌
.\.runtime\venv\Scripts\python.exe scripts\tools\status_overview.py

# 2) 提交一份干净合同（低风险 -> 自动办结，不惊动任何人）
$h = @{'X-Auth-Token'='local-dev-token';'X-Role'='specialist'}
$low = '{"text":"技术服务合同\n第一条 甲方委托乙方提供运维服务，期限一年。\n第二条 甲方在验收合格后 30 日内支付服务费。\n第三条 双方按约定交付，验收标准以附件一为准。"}'
Invoke-RestMethod -Uri http://127.0.0.1:8790/api/v1/contracts -Method Post -ContentType application/json -Headers $h -Body $low | ConvertTo-Json -Depth 4

# 3) 提交一份中风险合同（停在专员待办）
$mid = '{"text":"技术服务合同\n第一条 甲方委托乙方提供运维服务。\n第二条 乙方违反保密义务的，违约金为合同总额的 50%。\n第三条 甲方在验收合格后 30 日内支付服务费。"}'
$r = Invoke-RestMethod -Uri http://127.0.0.1:8790/api/v1/contracts -Method Post -ContentType application/json -Headers $h -Body $mid
$r | ConvertTo-Json -Depth 4

# 4) 专员确认 -> 主管签署（决策必须与当前等待节点匹配，跨节点会被 409 拒绝）
$rid = $r.run_id
Invoke-RestMethod -Uri "http://127.0.0.1:8790/api/v1/runs/$rid/decide" -Method Post -ContentType application/json -Headers $h -Body '{"decision":"confirm","actor":"specialist-li","note":"确认"}' | ConvertTo-Json -Depth 3
Invoke-RestMethod -Uri "http://127.0.0.1:8790/api/v1/runs/$rid/decide" -Method Post -ContentType application/json -Headers @{'X-Auth-Token'='local-dev-token';'X-Role'='supervisor'} -Body '{"decision":"approve","actor":"supervisor-zhang","note":"同意归档"}' | ConvertTo-Json -Depth 3

# 5) 看这个 run 的完整证据链
.\.runtime\venv\Scripts\python.exe scripts\tools\status_overview.py --run $rid
'@
"`n提示：网关接口需要请求头 X-Auth-Token=local-dev-token 与 X-Role=specialist|supervisor|admin。"
"提示：AUTH_MODE=jwt 时改用 Authorization: Bearer <token>（见 docs/11-使用说明书.md）。"
"提示：停止服务：pwsh -File scripts/stop-demo.ps1"
exit 0
