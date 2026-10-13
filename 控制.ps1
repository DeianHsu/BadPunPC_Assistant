param(
    [ValidateSet('状态', '开始', '停止', '题面', '关闭服务')][string]$操作 = '状态',
    [ValidateSet('当前', '十题', '连续')][string]$模式 = '连续'
)
$ErrorActionPreference = 'Stop'
$badpunPort = 18741
$badpunConfigPath = Join-Path $PSScriptRoot 'config.local.json'
if (Test-Path -LiteralPath $badpunConfigPath) {
    $badpunConfig = Get-Content -Raw -LiteralPath $badpunConfigPath | ConvertFrom-Json
    $badpunPort = $badpunConfig.port
}
$badpunUrl = "http://127.0.0.1:$badpunPort"
switch ($操作) {
    '开始' { $badpunRunMode = @{ '当前' = 'current'; '十题' = 'ten'; '连续' = 'continuous' }[$模式]; $badpunResult = Invoke-RestMethod -Uri "$badpunUrl/start" -Method Post -ContentType 'application/json' -Body (@{mode = $badpunRunMode} | ConvertTo-Json) }
    '停止' { $badpunResult = Invoke-RestMethod -Uri "$badpunUrl/stop" -Method Post -ContentType 'application/json' -Body '{}' }
    '关闭服务' { $badpunResult = Invoke-RestMethod -Uri "$badpunUrl/shutdown" -Method Post -ContentType 'application/json' -Body '{}' }
    '题面' { $badpunResult = Invoke-RestMethod -Uri "$badpunUrl/puzzle" }
    '状态' { $badpunResult = Invoke-RestMethod -Uri "$badpunUrl/status" }
}
$badpunResult | ConvertTo-Json -Depth 8
