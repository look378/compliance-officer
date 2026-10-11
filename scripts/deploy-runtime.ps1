# 部署岗位能力到本机运行时平台（框架侧只读挂载点：.lemonclaw/）
#
# 原则：不改外部运行时框架源码，只写框架约定的三个配置挂载点：
#   .lemonclaw/skills/<name>-<version>/   岗位技能包
#   .lemonclaw/mcp.json                  岗位能力服务地址
#   .lemonclaw/.env                      LLM / 渠道 / 开关
#
# 用法： pwsh -File scripts/deploy-runtime.ps1 [-EnableMcp]
param(
    [switch]$EnableMcp,
    [string]$LlmKeyFrom = ''
)
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runtime     = Join-Path $ProjectRoot '.runtime'
$Workspace   = Join-Path $Runtime 'workspace'
$Framework   = if ($env:LEMONCLAW_HOME) { $env:LEMONCLAW_HOME }
               else { Join-Path (Split-Path -Parent $ProjectRoot) 'lemonclaw-main' }
$SkillsSrc   = Join-Path $ProjectRoot 'skills'

if (-not (Test-Path (Join-Path $Framework 'config'))) {
    throw @"
未找到运行时框架 LemonClaw：$Framework
请任选其一，然后重跑本脚本：
  1) 把框架放到本项目目录的**同级兄弟目录** lemonclaw-main（默认约定）：
       git clone https://github.com/nl8590687/lemonclaw.git ..\lemonclaw-main
  2) 或显式指定位置：  `$env:LEMONCLAW_HOME = 'D:\path\to\lemonclaw-main'
"@
}

# 【实读结论】框架的配置查找只认两处：进程当前工作目录下的 .lemonclaw/，或框架安装根目录下的 .lemonclaw/
#   （lemonclaw-main/config/config.py:23-46 find_config_file）
# 因此挂载点必须是框架根目录下的 .lemonclaw/ —— 它属于框架的数据目录（已在 .gitignore 中），
# 不是源码，写它不违反「源码零改动」边界。
$LemonDir  = Join-Path $Framework '.lemonclaw'
$SkillsDst = Join-Path $LemonDir 'skills'
# 进程工作目录另用 .runtime/workspace（由调用方设置），其下也会生成 .lemonclaw/lemonclaw.db

New-Item -ItemType Directory -Force -Path $LemonDir, $Workspace, $SkillsDst | Out-Null

# 1) 技能包：源在项目内（版本库），部署到框架约定的技能目录
$deployed = @()
Get-ChildItem $SkillsSrc -Directory | ForEach-Object {
    $dst = Join-Path $SkillsDst $_.Name
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item $_.FullName $dst -Recurse -Force
    $deployed += $_.Name
}
"[deploy] 已部署技能包 $($deployed.Count) 个: $($deployed -join ', ')"

# 1b) 工作区 .lemonclaw -> 框架 .lemonclaw（目录联接，无需管理员权限）
#     【实读结论】框架的技能目录与数据库路径同样是进程相对路径
#     （agent/skill/manager.py:51-55 用 Path.cwd()/'.lemonclaw'/'skills'），
#     若进程工作目录与配置目录不一致，技能会「已部署但列表为空」。
#     用联接把两者绑定，既不复制、也不改动框架源码。
$wsLemon = Join-Path $Workspace '.lemonclaw'
if (Test-Path $wsLemon) {
    $item = Get-Item $wsLemon -Force
    if ($item.LinkType -eq 'Junction') { $item.Delete() } else { Remove-Item $wsLemon -Recurse -Force }
}
New-Item -ItemType Junction -Path $wsLemon -Target $LemonDir | Out-Null
"[deploy] 工作区联接: $wsLemon -> $LemonDir"

# 2) MCP 配置：是否自动连接由参数决定（D0 机械验证阶段可先关闭，避免依赖未启动的服务）
$mcpPath = Join-Path $LemonDir 'mcp.json'
$auto = if ($EnableMcp) { 'true' } else { 'false' }
@"
{
  "compliance": {
    "url": "http://127.0.0.1:8770/mcp",
    "headers": {},
    "auto_connect": $auto
  }
}
"@ | Set-Content -Path $mcpPath -Encoding utf8
"[deploy] mcp.json auto_connect=$auto -> $mcpPath"

# 3) .env：以本项目的 .runtime/.lemonclaw/.env 为源，同步到框架配置目录；密钥从既有项目注入
$envSrc  = Join-Path $Runtime '.lemonclaw\.env'
$envPath = Join-Path $LemonDir '.env'
if (-not (Test-Path $envSrc)) { throw ".env 源不存在：$envSrc" }

# 密钥来源优先级：-LlmKeyFrom 参数 > 源文件内的现值
# 说明：不再去"同工作区其它项目"里找密钥（那是开发机的历史遗留，不适合公开仓库：
#       既写死了别的项目路径，也等于把密钥从别处悄悄搬过来）。
$KeySources = @()
if ($LlmKeyFrom) { $KeySources += $LlmKeyFrom }
$llmKey = $null
foreach ($src in $KeySources) {
    if (Test-Path $src) {
        $line = Select-String -Path $src -Pattern '^(LLM_API_KEY|OPENAI_API_KEY)\s*=' | Select-Object -First 1
        if ($line) { $llmKey = ($line.Line -split '=', 2)[1].Trim().Trim('"'); if ($llmKey) { break } }
    }
}
if (-not $llmKey) {
    $cur = Select-String -Path $envSrc -Pattern '^OPENAI_API_KEY\s*=' | Select-Object -First 1
    if ($cur) { $llmKey = ($cur.Line -split '=', 2)[1].Trim().Trim('"') }
}
if (-not $llmKey) { throw "未取得 LLM 密钥：请用 -LlmKeyFrom <含密钥的.env路径> 指定" }

$content = Get-Content $envSrc -Raw -Encoding utf8
$content = $content -replace '(?m)^OPENAI_API_KEY\s*=.*$', "OPENAI_API_KEY=$llmKey"
if ($EnableMcp) { $content = $content -replace '(?m)^\s*ENABLE_MCP\s*=.*$', 'ENABLE_MCP=true' }

# 【企业对接】连接器开关默认写入（**已存在则不覆盖**，贵司改过的配置不会被部署脚本冲掉）。
# 细节与字段映射见 docs/13-企业系统对接契约.md；不认识的取值会被 prod_config.validate() 拦下。
$connectorDefaults = [ordered]@{
    CONTRACT_SOURCE = 'mock'    # mock | dir | http
    MAIL_GATEWAY    = 'mock'    # mock | http | smtp | none
}
$addedKeys = @()
foreach ($k in $connectorDefaults.Keys) {
    if ($content -notmatch "(?m)^\s*$k\s*=") {
        $content = $content.TrimEnd() + "`n$k=$($connectorDefaults[$k])`n"
        $addedKeys += $k
    }
}
if ($addedKeys.Count -gt 0) {
    "[deploy] 已补入连接器开关默认值（未覆盖既有值）：$($addedKeys -join ', ')"
    "[deploy]   改这些开关即可对接真实 OA/合同库/邮件：docs/13-企业系统对接契约.md"
}

Set-Content -Path $envPath -Value $content -Encoding utf8 -NoNewline
# 【重要】同时把密钥写回项目侧的 .env 源文件：
#   dotenv 在 override=False 时不会用后面的文件覆盖"已存在但为空"的键，
#   若源文件里 OPENAI_API_KEY= 是空值，会挡住框架 .env 里的真实密钥
#   （实测：网关因此拿不到 key，WorkflowManager 初始化失败且被框架静默吞掉）。
Set-Content -Path $envSrc -Value $content -Encoding utf8 -NoNewline
"[deploy] .env 已同步（密钥来源：$(if($LlmKeyFrom){$LlmKeyFrom}else{'既有项目 .env'})，指纹 ...$($llmKey.Substring($llmKey.Length-4))）"
$mcpFlag = (Select-String -Path $envPath -Pattern '^\s*ENABLE_MCP\s*=' | Select-Object -First 1).Line
"[deploy] .env 就绪，$mcpFlag"

# 4) 部署清单留证
$manifest = [ordered]@{
    deployed_at = (Get-Date -Format s)
    project     = 'compliance_officer'
    skills      = $deployed
    mcp_auto    = $EnableMcp
    framework   = $Framework
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $Runtime 'deploy-manifest.json') -Encoding utf8
"[deploy] 完成"
