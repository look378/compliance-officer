#!/usr/bin/env pwsh
# JWT 模式端到端回归（审查修正 P0/P1 的验证）
#
# 覆盖：
#   1) AUTH_MODE=jwt 下「演示令牌 + X-Role: admin」必须 401
#      （原实现落到演示分支直接采纳 X-Role，任何能访问端口的人都能自称管理员）
#   2) 账号登录拿到的 Bearer 才能访问，且角色来自令牌而非请求头
#   3) 本人改密必须提供旧密码；管理员代改必须带管理员身份
#   4) 改密后旧令牌立即失效（账号级撤销水位生效）
#
# 注意：全程不改动 admin 账号，改密只在一次性测试账号上做。
# 用法：pwsh -NoProfile -File scripts/_jwt-regression.ps1

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Py   = Join-Path $ProjectRoot '.runtime\venv\Scripts\python.exe'
$LogDir = Join-Path $ProjectRoot '.runtime\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'jwt-regression.log'

function Get-PortPid([int]$Port) {
    $line = netstat -ano | Select-String ":$Port\s+.*LISTENING" | Select-Object -First 1
    if (-not $line) { return $null }
    return [int](($line.ToString() -split '\s+')[-1])
}

function Wait-Health([int]$Port, [int]$Tries = 90) {
    for ($i = 0; $i -lt $Tries; $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-WebRequest "http://127.0.0.1:$Port/health" -TimeoutSec 2 -UseBasicParsing | Out-Null; return $true } catch { }
    }
    return $false
}

# 停掉旧的 8790 / 8770（可能是更早以 demo 模式启动的残留进程）
foreach ($p in 8790, 8770) {
    $pid0 = Get-PortPid $p
    if ($pid0) {
        Write-Host "[jwt-reg] 停止端口 $p 上的进程 PID=$pid0"
        Stop-Process -Id $pid0 -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 800
    }
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH       = $ProjectRoot

# 能力服务必须在线：网关启动钩子会编译工作流图（图内 MCP 工具在编译期解析）
Write-Host '[jwt-reg] 启动岗位能力服务（8770）'
Start-Process -FilePath $Py -ArgumentList @('mcp_server\compliance_mcp_server.py', '--port', '8770') `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir 'jwt-capability.log') `
    -RedirectStandardError  (Join-Path $LogDir 'jwt-capability.log.err')
if (-not (Wait-Health 8770)) { Write-Host '[jwt-reg] 能力服务未就绪'; exit 1 }

$env:AUTH_MODE     = 'jwt'
$env:JWT_SECRET    = (& $Py -c "import secrets;print(secrets.token_urlsafe(48))")
$env:TOKEN_TTL_MIN = '480'
Write-Host "[jwt-reg] 以 AUTH_MODE=jwt 启动网关（secret 长度 $($env:JWT_SECRET.Length)）"
Start-Process -FilePath $Py -ArgumentList @('gateway\app.py', '--port', '8790') `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
    -RedirectStandardOutput $Log -RedirectStandardError "$Log.err"
if (-not (Wait-Health 8790)) {
    Write-Host '[jwt-reg] 网关未就绪；stdout:'; Get-Content $Log -Tail 20 -ErrorAction SilentlyContinue
    Write-Host '[jwt-reg] stderr:'; Get-Content "$Log.err" -Tail 30 -ErrorAction SilentlyContinue
    exit 1
}

$base = 'http://127.0.0.1:8790'
$results = [ordered]@{}

function Try-Call([string]$Method, [string]$Path, [hashtable]$Headers, [string]$Body) {
    $args = @{ Uri = "$base$Path"; Method = $Method; UseBasicParsing = $true; TimeoutSec = 30 }
    if ($Headers) { $args.Headers = $Headers }
    if ($Body) { $args.ContentType = 'application/json'; $args.Body = $Body }
    try {
        $r = Invoke-WebRequest @args
        return @{ code = [int]$r.StatusCode; body = $r.Content }
    } catch {
        $code = 0
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode.value__ }
        return @{ code = $code; body = '' }
    }
}

# 0) 预备：管理员登录（bootstrap 创建的默认账号）
$adminLogin = Try-Call 'POST' '/api/v1/auth/login' $null '{"username":"admin","password":"admin12345"}'
$adminToken = ''
if ($adminLogin.code -eq 200) { $adminToken = ($adminLogin.body | ConvertFrom-Json).access_token }
$results['admin_login'] = if ($adminToken) { 'PASS' } else { "FAIL（HTTP $($adminLogin.code)）" }

# 1) 演示令牌 + 自封管理员 → 必须 401
$demo = Try-Call 'GET' '/api/v1/auth/me' @{ 'X-Auth-Token' = 'local-dev-token'; 'X-Role' = 'admin' } $null
$results['demo_token_rejected'] = if ($demo.code -eq 401) { "PASS（HTTP $($demo.code)）" } else { "FAIL（HTTP $($demo.code)：jwt 模式仍接受演示令牌）" }

# 2) Bearer 访问且角色来自令牌
if ($adminToken) {
    $me = Try-Call 'GET' '/api/v1/auth/me' @{ Authorization = "Bearer $adminToken" } $null
    $role = ''
    if ($me.code -eq 200) { $role = ($me.body | ConvertFrom-Json).role }
    $results['bearer_access_role_from_token'] = if ($me.code -eq 200 -and $role -eq 'admin') { "PASS（role=$role）" } else { "FAIL（HTTP $($me.code), role=$role）" }

    # 3) 一次性测试账号：管理员代改必须带管理员身份
    $uname = "jwttest$((Get-Random -Maximum 99999))"
    $created = Try-Call 'POST' '/api/v1/users' @{ Authorization = "Bearer $adminToken" } `
        (@{ username = $uname; password = 'Init@12345'; display_name = 'JWT 回归'; role = 'specialist' } | ConvertTo-Json)
    $results['admin_creates_test_user'] = if ($created.code -eq 200) { 'PASS' } else { "FAIL（HTTP $($created.code)）" }

    $login2 = Try-Call 'POST' '/api/v1/auth/login' $null (@{ username = $uname; password = 'Init@12345' } | ConvertTo-Json)
    $userToken = ''
    if ($login2.code -eq 200) { $userToken = ($login2.body | ConvertFrom-Json).access_token }
    $results['test_user_login'] = if ($userToken) { 'PASS' } else { "FAIL（HTTP $($login2.code)）" }

    if ($userToken) {
        # 本人改密不带旧密码 → 必须 400
        $noOld = Try-Call 'POST' "/api/v1/users/$uname/password" @{ Authorization = "Bearer $userToken" } `
            '{"new_password":"NewPass@6789"}'
        $results['self_change_requires_old_password'] = if ($noOld.code -eq 400) { "PASS（HTTP $($noOld.code)）" } else { "FAIL（HTTP $($noOld.code)）" }

        # 非管理员改他人（admin）密码 → 必须 403（放在改密之前：改密会使本令牌失效）
        $cross = Try-Call 'POST' '/api/v1/users/admin/password' @{ Authorization = "Bearer $userToken" } `
            '{"new_password":"Hacked@123"}'
        $results['cross_user_change_denied'] = if ($cross.code -eq 403) { "PASS（HTTP $($cross.code)）" } else { "FAIL（HTTP $($cross.code)）" }

        # 本人改密带旧密码 → 200，且旧令牌立即失效
        $changed = Try-Call 'POST' "/api/v1/users/$uname/password" @{ Authorization = "Bearer $userToken" } `
            (@{ old_password = 'Init@12345'; new_password = 'NewPass@6789' } | ConvertTo-Json)
        $results['self_change_with_old_password'] = if ($changed.code -eq 200) { 'PASS' } else { "FAIL（HTTP $($changed.code)）" }
        $after = Try-Call 'GET' '/api/v1/auth/me' @{ Authorization = "Bearer $userToken" } $null
        $results['old_token_revoked_after_change'] = if ($after.code -eq 401) { "PASS（HTTP $($after.code)）" } else { "FAIL（HTTP $($after.code)：旧令牌仍可用）" }
    }

    # 4) 停用账号即时生效
    $off = Try-Call 'POST' "/api/v1/users/$uname/status" @{ Authorization = "Bearer $adminToken" } '{"status":"disabled"}'
    $results['disable_user'] = if ($off.code -eq 200) { 'PASS' } else { "FAIL（HTTP $($off.code)）" }
}

# 5) 诊断与维护端点（新增，需管理员）
$diag = Try-Call 'GET' '/api/v1/diagnostics' @{ Authorization = "Bearer $adminToken" } $null
$results['diagnostics_requires_admin_works'] = if ($diag.code -eq 200) { 'PASS' } else { "FAIL（HTTP $($diag.code)）" }
$maint = Try-Call 'POST' '/api/v1/maintenance' @{ Authorization = "Bearer $adminToken" } '{"actions":["stale_claims","revocations"]}'
$results['maintenance_works'] = if ($maint.code -eq 200) { 'PASS' } else { "FAIL（HTTP $($maint.code)）" }
$anonDiag = Try-Call 'GET' '/api/v1/diagnostics' $null $null
$results['diagnostics_anonymous_denied'] = if ($anonDiag.code -eq 401) { "PASS（HTTP $($anonDiag.code)）" } else { "FAIL（HTTP $($anonDiag.code)）" }

Write-Host "`n===== JWT 模式回归结果 ====="
foreach ($k in $results.Keys) { Write-Host ("  {0,-40} {1}" -f $k, $results[$k]) }

$fails = @($results.Values | Where-Object { $_ -like 'FAIL*' })
$verdict = if ($fails.Count -eq 0) { 'PASS' } else { 'FAIL' }
Write-Host "`n总判定: $verdict（失败 $($fails.Count) 项）"

$outDir = Join-Path $ProjectRoot 'docs\evidence'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
@{ check = 'auth-posture-jwt'; results = $results; verdict = $verdict
   passed = @($results.Values | Where-Object { $_ -notlike 'FAIL*' }).Count
   total = $results.Count
   at = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') } |
    ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $outDir 'auth-posture-jwt.json')
Write-Host "证据: $(Join-Path $outDir 'auth-posture-jwt.json')"

# 收尾：停掉本次启动的网关（能力服务保留，供后续自检复用）
$gw = Get-PortPid 8790
if ($gw) { Stop-Process -Id $gw -Force -ErrorAction SilentlyContinue }
exit $(if ($verdict -eq 'PASS') { 0 } else { 1 })
