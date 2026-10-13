$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$badpunPython = Get-Command python -ErrorAction SilentlyContinue
if ($badpunPython) { $badpunPythonPath = $badpunPython.Source }
else { $badpunPythonPath = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' }
if (-not (Test-Path -LiteralPath $badpunPythonPath)) { throw '未找到 Python。请安装 Python 3.12 或在脚本中指定路径。' }
& $badpunPythonPath -m pip install --target .runtime -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败' }
npm install --cache .npm-cache --ignore-scripts
if ($LASTEXITCODE -ne 0) { throw '桥接依赖安装失败' }
