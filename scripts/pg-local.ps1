#!/usr/bin/env pwsh
# 【已废弃 · 第二次审查修正】本脚本无法工作，保留仅为兼容旧命令并给出明确指引。
#
# 为什么废弃：
#   它把数据目录默认放在**项目内** `.runtime\pg\data`，而本项目路径含中文（项目三）。
#   PostgreSQL 在 Windows 上不接受含非 ASCII 字符的数据目录：
#     - initdb 报  invalid byte sequence for encoding "UTF8": 0xcf 0xee
#     - 受限环境另报 could not create restricted token
#   这是 PG 的硬约束，不是参数问题 —— 因此"数据目录落在项目内"这条路走不通。
#   另外它还把 `psql` 查询写了两遍（第 97-100 行），且从不检查 createdb 的返回码。
#
# 现在请用（数据目录 C:\co-pg，纯 ASCII）：
#   pwsh -File scripts/pg-local-ascii.ps1 -Action init|start|stop|status
#
# 本脚本会把调用**转发**给上面那个脚本（`-PgDataPath` 不再支持）。
param(
    [ValidateSet('init', 'start', 'stop', 'status')]
    [string]$Action = 'status',
    [int]$Port = 55432,
    [string]$PgDataPath = ''
)
$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

if ($PgDataPath) {
    Write-Host "[pg][已废弃] -PgDataPath 不再支持：数据目录必须落在纯 ASCII 路径下。"
    Write-Host "            请改用 pwsh -File scripts/pg-local-ascii.ps1 -Action $Action -Port $Port"
    exit 2
}

Write-Host "[pg][已废弃] scripts/pg-local.ps1 已停用，转发到 scripts/pg-local-ascii.ps1 ..."
Write-Host "[pg][已废弃] 原因：默认数据目录在项目内，而项目路径含中文，PG 无法在其上建库。"

$ascii = Join-Path $PSScriptRoot 'pg-local-ascii.ps1'
if (-not (Test-Path $ascii)) { throw "未找到 $ascii" }
& $ascii -Action $Action -Port $Port
# 用子进程调用，退出码才可靠（被调用脚本内部的 $LASTEXITCODE 可能被原生命令污染）
if ($null -eq $LASTEXITCODE) { exit 0 }
exit $LASTEXITCODE
