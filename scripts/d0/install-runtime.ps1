# 一键安装本机运行时依赖（P1/D0 用）
#
# 设计要点（对应方案设计说明 §1 约束与 §8 风险）：
#   1) 运行时 venv 只装依赖、不复制底座源码；底座通过 PYTHONPATH 引用（以依赖方式引入）
#   2) uv 的缓存与临时目录都放在工作区内，避免写入用户目录被拒
#   3) 少数包（jieba）只有源码包，需放开构建；其余强制仅用 whl
#
# 用法： pwsh -File scripts/d0/install-runtime.ps1
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Runtime     = Join-Path $ProjectRoot '.runtime'
$Venv        = Join-Path $Runtime 'venv'
$VenvPy      = Join-Path $Venv 'Scripts\python.exe'
$Framework   = if ($env:LEMONCLAW_HOME) { $env:LEMONCLAW_HOME }
               else { Join-Path (Split-Path -Parent $ProjectRoot) 'lemonclaw-main' }

if (-not (Test-Path $VenvPy)) { throw "运行时 venv 不存在，请先创建：uv venv `"$Venv`" --python 3.13" }

New-Item -ItemType Directory -Force -Path (Join-Path $Runtime 'tmp') | Out-Null
$env:UV_CACHE_DIR     = Join-Path $Runtime 'uvcache'
$env:UV_DEFAULT_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'
$env:TEMP = $env:TMP = $env:TMPDIR = Join-Path $Runtime 'tmp'

$log = Join-Path $Runtime 'd0-venv-install.log'
"=== 安装开始 $(Get-Date -Format s) ===" | Out-File $log -Encoding utf8

# 阶段1：只有源码包的依赖，逐个放开构建（避免 --only-binary 让整个解析失败）
$sdistOnly = @('jieba==0.42.1')
foreach ($pkg in $sdistOnly) {
    "--- pip 安装 $pkg ---" | Tee-Object -FilePath $log -Append
    & $VenvPy -m pip install --no-cache-dir $pkg 2>&1 | Tee-Object -FilePath $log -Append | Select-Object -Last 4
}

# 阶段2：其余依赖，全部走 whl，避免本地构建
"--- uv 安装框架依赖（仅 whl）---" | Tee-Object -FilePath $log -Append
& uv pip install --python $VenvPy --only-binary :all: -r (Join-Path $Framework 'requirements.txt') 2>&1 |
    Tee-Object -FilePath $log -Append | Select-Object -Last 20
if ($LASTEXITCODE -ne 0) { throw "框架依赖安装失败，详见 $log" }

# 阶段3：本项目依赖
"--- uv 安装项目依赖 ---" | Tee-Object -FilePath $log -Append
& uv pip install --python $VenvPy --only-binary :all: -r (Join-Path $ProjectRoot 'requirements.txt') 2>&1 |
    Tee-Object -FilePath $log -Append | Select-Object -Last 10
if ($LASTEXITCODE -ne 0) { throw "项目依赖安装失败，详见 $log" }

# 阶段4：导入自检
"--- 依赖自检 ---" | Tee-Object -FilePath $log -Append
$env:PYTHONPATH = $Framework
& $VenvPy -c @"
import importlib, sys
mods = ['langchain','langchain_openai','langgraph','langgraph.checkpoint.sqlite','pydantic','rich','yaml','jieba','lark_oapi','httpx','ephem','zhdate']
bad = []
for m in mods:
    try: importlib.import_module(m)
    except Exception as e: bad.append(f'{m}: {e}')
print('PY', sys.version.split()[0])
print('MISSING' if bad else 'ALL_OK', '; '.join(bad))
"@ 2>&1 | Tee-Object -FilePath $log -Append

"=== 安装结束 $(Get-Date -Format s)，日志：$log ==="
