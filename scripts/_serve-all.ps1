# 供计划任务调用的服务启动包装（脱离调用方会话运行）
# 说明：计划任务直接指向 python.exe 时容易因环境/工作目录问题失败，
#       统一由本脚本负责：切目录、设环境、依次拉起三个服务并等它们就绪。
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runtime     = Join-Path $ProjectRoot '.runtime'
$Py          = Join-Path $Runtime 'venv\Scripts\python.exe'
$LogDir      = Join-Path $Runtime 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH       = $ProjectRoot

$services = @(
    @{ name = 'capability'; port = 8770; script = 'mcp_server\compliance_mcp_server.py'; args = @('--port','8770') },
    @{ name = 'mock';       port = 8780; script = 'mock\mock_office.py';               args = @('--port','8780','--timeout-sleep','3') },
    @{ name = 'gateway';    port = 8790; script = 'gateway\app.py';                    args = @('--port','8790') }
)

foreach ($s in $services) {
    $alive = $false
    try { Invoke-WebRequest "http://127.0.0.1:$($s.port)/health" -TimeoutSec 2 -UseBasicParsing | Out-Null; $alive = $true } catch { $alive = $false }
    if ($alive) { continue }
    Start-Process -FilePath $Py -ArgumentList (@($s.script) + $s.args) `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "$($s.name).out.log") `
        -RedirectStandardError  (Join-Path $LogDir "$($s.name).err.log")
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 400
        try { Invoke-WebRequest "http://127.0.0.1:$($s.port)/health" -TimeoutSec 2 -UseBasicParsing | Out-Null; break } catch { }
    }
}
