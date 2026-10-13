param([switch]$Inspect)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$badpunPython = Get-Command python -ErrorAction SilentlyContinue
if ($badpunPython) { $badpunPythonPath = $badpunPython.Source }
else { $badpunPythonPath = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' }
if (-not (Test-Path -LiteralPath $badpunPythonPath)) { throw '未找到 Python。请安装 Python 3.12 或在脚本中指定路径。' }
if (-not (Test-Path -LiteralPath 'config.local.json')) { Copy-Item -LiteralPath 'config.example.json' -Destination 'config.local.json' }
if ($Inspect) { & $badpunPythonPath badpun.py inspect }
else { & $badpunPythonPath badpun.py serve }
exit $LASTEXITCODE
