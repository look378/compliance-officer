#!/usr/bin/env pwsh
# 停止本机演示环境（释放 8770 / 8780 / 8790 端口）
#
# 只停止**本项目**的进程：优先用 start-demo.ps1 记录的 PID 文件，
# 其次按端口 + 命令行归属判断。不会误杀别家程序，并如实报告失败。
#
# 用法：pwsh -File scripts/stop-demo.ps1
$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PidDir      = Join-Path $ProjectRoot '.runtime\pids'
$nameByPort  = @{ 8770 = 'capability'; 8780 = 'mock'; 8790 = 'gateway' }

# 【审查修正 P2】原实现按端口 OwningProcess 无差别 Stop-Process：
#   - 不校验进程归属（端口被别家程序占用时会误杀）
#   - 失败被 -ErrorAction SilentlyContinue 吞掉，却仍打印"已停止"
#   - 杀完不复查端口是否释放
# 现在：PID 文件优先 → 端口 + 命令行归属兜底 → 杀后复查端口 → 失败如实报告并 exit 1。
function Get-PortPid([int]$port) {
    $line = netstat -ano | Select-String ":$port\s+.*LISTENING" | Select-Object -First 1
    if (-not $line) { return $null }
    return [int](($line.ToString() -split '\s+')[-1])
}

function Test-Ours([int]$procId) {
    # 归属判断（按可靠性排序）：
    #   1) 可执行文件路径（Get-Process.Path，不依赖 WMI）
    #   2) 命令行（Get-CimInstance）—— 受限环境下可能"拒绝访问"，此时返回 $null 表示"无法判定"
    # 无法判定时由调用方按"不误杀"原则处理（宁可不杀，也不杀别家程序）。
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($proc) {
        try { if ($proc.Path -and $proc.Path -like "*$ProjectRoot*") { return $true } } catch { }
        if ($proc.ProcessName -notlike 'python*') { return $false }   # 明显不是本项目服务
    }
    try {
        $ci = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction Stop
        if ($ci -and $ci.CommandLine) { return [bool]($ci.CommandLine -like "*$ProjectRoot*") }
    } catch { }
    return $null        # 无法判定
}

$failures = 0
foreach ($port in 8770, 8780, 8790) {
    $name = $nameByPort[$port]
    $targets = @()
    $pidFile = Join-Path $PidDir "$name.pid"
    if (Test-Path $pidFile) {
        $recorded = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($recorded) {
            $ours = Test-Ours ([int]$recorded)
            # PID 文件是本脚本自己写的：为 $null（WMI 不可用）时按"记录可信 + 名字像 python"处理
            if ($ours -eq $true -or ($null -eq $ours -and (Get-Process -Id ([int]$recorded) -ErrorAction SilentlyContinue))) {
                $targets += [int]$recorded
            }
        }
    }
    if (-not $targets) {
        $byPort = Get-PortPid $port
        if ($byPort) {
            $ours = Test-Ours $byPort
            if ($ours -eq $true) {
                $targets += $byPort
            } elseif ($null -eq $ours) {
                "[stop] 端口 $port（PID $byPort）：无法确认进程归属（WMI 不可用），为避免误杀已跳过；" +
                "如需强制清理请手动 Stop-Process -Id $byPort"
                $failures++
                continue
            } else {
                "[stop] 端口 $port 被**非本项目**进程占用（PID $byPort）：跳过，未做任何处理"
                continue
            }
        }
    }
    if (-not $targets) { "[stop] $name（端口 $port）：未运行"; continue }

    foreach ($procId in ($targets | Select-Object -Unique)) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            "[stop] $name (PID $procId) 停止失败：$($_.Exception.Message)"
            $failures++
        }
    }
    Start-Sleep -Milliseconds 600
    $still = Get-PortPid $port
    if ($still) {
        "[stop] $name：进程已发送停止，但端口 $port 仍被 PID $still 占用（请检查权限或被托管的子进程）"
        $failures++
    } else {
        "[stop] $name（端口 $port）：已停止 $(($targets | Select-Object -Unique) -join ',')"
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
}

if ($failures -gt 0) { "[stop] 完成，但有 $failures 项未成功"; exit 1 }
"[stop] 完成。"
exit 0
