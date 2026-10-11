#!/usr/bin/env pwsh
# 在纯 ASCII 路径下建立一个**真实 PostgreSQL 实例**（本机验证用，非生产）
#
# 为什么必须放 C:\co-pg：
#   PostgreSQL 在 Windows 上调用 initdb 时不做 UTF-8 与 ANSI 之间的路径转换，
#   项目路径里的中文会变成非法字节序列（实测报「invalid byte sequence for encoding "UTF8": 0xcf 0xee」）。
#   这是 PG 的硬约束，不是配置问题 —— 因此数据目录必须是纯 ASCII 路径。
#
# 用法：pwsh -File scripts/pg-local-ascii.ps1 -Action init|start|stop|status
#
# 【第二次审查修正】本脚本原先有 3 个问题，已全部修：
#   1. 服务端日志写在项目内 `.runtime\logs\pg-server.log` —— 而 **pg_ctl 是 ANSI 程序**，
#      路径里的中文会让它打不开日志文件（实测 `could not open log file ... Permission denied`
#      且路径显示为乱码），`pg_ctl start` 在"写日志"这一步就整体失败，
#      表现为"库起不来"。现在日志固定写到 ASCII 的 `$Root\pg-server.log`。
#   2. 判活只用 `pg_ctl status`：受限/受管环境里 pg_ctl 要向 postmaster 发信号，
#      被拒时返回非 0（实测 `could not send stop signal ... Operation not permitted`），
#      于是"明明在跑"被判成"没在跑"，脚本又去 start，撞端口占用。
#      现在判活统一用「**能不能连上**」（psql SELECT 1），并把两种视图分开报告。
#   3. `psql` / `createdb` 的返回码从不检查：连不上时 `$exists` 也是空，
#      会被误当成"库不存在"去 createdb，报出与真实原因无关的错误。
#      现在逐个检查 `$LASTEXITCODE` 并给出可执行的错误信息。
param(
    [ValidateSet('init', 'start', 'stop', 'status')]
    [string]$Action = 'status',
    [int]$Port = 55432,
    [string]$Root = 'C:\co-pg'
)
$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PgHomeSrc   = Join-Path $ProjectRoot '.runtime\pgsql-ascii\pgsql'
$PgHome      = Join-Path $Root 'pgsql'
$PgData      = Join-Path $Root 'data'
$LogDir      = Join-Path $ProjectRoot '.runtime\logs'
# 只有 pg_ctl 的 -l 参数必须是 ASCII（原生程序自己打开该路径）；
# initdb 的日志是 PowerShell 重定向写入的，留在项目内没问题。
$ServerLog   = Join-Path $Root 'pg-server.log'
$SuperUser   = 'compliance'
$DbName      = 'compliance'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Get-PgBin([string]$name) { Join-Path $PgHome "bin\$name.exe" }

function Test-PgCtlStatus {
    if (-not (Test-Path (Join-Path $PgData 'PG_VERSION'))) { return $false }
    & (Get-PgBin 'pg_ctl') -D $PgData status *> $null
    return ($LASTEXITCODE -eq 0)
}

function Test-PgReady {
    # 真实判活：直接连一次。受限环境下 pg_ctl status 会因"发不了信号"而误报"没在跑"。
    if (-not (Test-Path (Get-PgBin 'psql'))) { return $false }
    $out = & (Get-PgBin 'psql') -h 127.0.0.1 -p $Port -U $SuperUser -d postgres -tAc 'SELECT 1' 2>$null
    return ($LASTEXITCODE -eq 0 -and ("$out").Trim() -eq '1')
}

function Start-Pg {
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    & (Get-PgBin 'pg_ctl') -D $PgData -l $ServerLog `
        -o "-p $Port -c listen_addresses=127.0.0.1" start *> $null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 750
        if (Test-PgReady) { return $true }
    }
    return $false
}

function Show-ServerLog([int]$Tail = 25) {
    if (Test-Path $ServerLog) { Get-Content $ServerLog -Tail $Tail }
    else { Write-Host "[pg] 日志文件不存在：$ServerLog" }
}

switch ($Action) {
    'init' {
        if (-not (Test-Path (Join-Path $PgHomeSrc 'bin\initdb.exe'))) {
            throw "缺少 PostgreSQL 程序文件：$PgHomeSrc（先解压 .runtime\pgsql-download\pgsql.zip）"
        }
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
        if (-not (Test-Path (Join-Path $PgHome 'bin\initdb.exe'))) {
            Write-Host "[pg] 复制程序文件 -> $PgHome"
            Copy-Item -Recurse -Force $PgHomeSrc $PgHome
        }
        if (-not (Test-Path (Join-Path $PgData 'PG_VERSION'))) {
            Write-Host "[pg] initdb（locale=C，trust 认证，仅回环）"
            & (Get-PgBin 'initdb') -D $PgData -U $SuperUser -A trust -E UTF8 --locale=C *> (Join-Path $LogDir 'pg-initdb.log')
            if ($LASTEXITCODE -ne 0) {
                Get-Content (Join-Path $LogDir 'pg-initdb.log') -Tail 20
                throw "initdb 失败（exit=$LASTEXITCODE）"
            }
        } else { Write-Host '[pg] 数据目录已存在，跳过 initdb' }

        if (-not (Test-PgReady)) {
            Write-Host "[pg] 启动 postgres（端口 $Port，日志 $ServerLog）"
            if (-not (Start-Pg)) { Show-ServerLog; throw 'postgres 启动失败' }
        }

        $exists = & (Get-PgBin 'psql') -h 127.0.0.1 -p $Port -U $SuperUser -d postgres -tAc `
            "SELECT 1 FROM pg_database WHERE datname='$DbName'" 2>$null
        # 【第二次审查修正】检查返回码：连不上/权限不足时 $exists 也是空，
        # 会被误判成"库不存在"，接着 createdb 报出与真实原因无关的错。
        if ($LASTEXITCODE -ne 0) { throw "查询数据库列表失败（exit=$LASTEXITCODE，端口 $Port 是否可连？）" }
        if (-not ("$exists").Trim()) {
            Write-Host "[pg] 建库 $DbName"
            & (Get-PgBin 'createdb') -h 127.0.0.1 -p $Port -U $SuperUser $DbName
            if ($LASTEXITCODE -ne 0) { throw "createdb 失败（exit=$LASTEXITCODE）" }
        }
        Write-Host "[pg] 就绪"
        Write-Host "DATABASE_URL=postgresql://$SuperUser@127.0.0.1:$Port/$DbName"
    }
    'start' {
        if (Test-PgReady) { Write-Host '[pg] 已在运行（可连接）'; break }
        if (-not (Start-Pg)) { Show-ServerLog -Tail 20; exit 1 }
        Write-Host '[pg] 已启动'
    }
    'stop' {
        if (-not (Test-PgCtlStatus)) { Write-Host '[pg] 未在运行（pg_ctl 视图）'; break }
        & (Get-PgBin 'pg_ctl') -D $PgData -m fast stop *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[pg] 停止失败（exit=$LASTEXITCODE）——受限环境下可能不允许向 postmaster 发信号"
            exit 1
        }
        Write-Host '[pg] 已停止'
    }
    'status' {
        Write-Host "[pg] PgHome=$PgHome"
        Write-Host "[pg] PgData=$PgData"
        Write-Host "[pg] ServerLog=$ServerLog"
        Write-Host ("[pg] 可连接（判活依据）={0}" -f (Test-PgReady))
        Write-Host ("[pg] pg_ctl status 视图={0}" -f (Test-PgCtlStatus))
        if (Test-Path (Join-Path $PgData 'PG_VERSION')) {
            Write-Host "[pg] 版本文件=$((Get-Content (Join-Path $PgData 'PG_VERSION') -Raw).Trim())"
        }
    }
}

# 【第二次审查修正】显式 exit 0：`$LASTEXITCODE` 会被上面最后一次原生调用
# （如 `pg_ctl status`，在本机受限环境下返回非 0）污染，转发出一个假失败码。
exit 0
