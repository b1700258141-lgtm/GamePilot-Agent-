param(
    [switch]$Build
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousKey = [Environment]::GetEnvironmentVariable('GAMEPILOT_AGENT_API_KEY', 'Process')
$previousWslEnv = $env:WSLENV

if ($previousKey) {
    $modelKey = $previousKey
} elseif ($env:ANTHROPIC_AUTH_TOKEN -and
        $env:ANTHROPIC_BASE_URL -eq 'https://api.deepseek.com/anthropic') {
    $modelKey = $env:ANTHROPIC_AUTH_TOKEN
} else {
    throw '未找到 DeepSeek 密钥：请在本机环境配置 GAMEPILOT_AGENT_API_KEY，或配置指向 DeepSeek Anthropic 入口的 ANTHROPIC_AUTH_TOKEN。'
}

Push-Location $projectRoot
try {
    # docker.cmd 经 WSL 调用 Docker；仅为此次启动转发已有密钥，不写入文件。
    $env:GAMEPILOT_AGENT_API_KEY = $modelKey
    $forwarded = @($previousWslEnv -split ':' | Where-Object {
        $_ -and $_ -notlike 'GAMEPILOT_AGENT_API_KEY*'
    })
    $env:WSLENV = (@($forwarded) + 'GAMEPILOT_AGENT_API_KEY/u') -join ':'

    $composeArgs = @('compose', '-f', 'compose.local.yaml', 'up', '-d')
    if ($Build) {
        $composeArgs += '--build'
    } else {
        $composeArgs += '--no-build'
    }
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw '本地 Docker 服务启动失败。'
    }
    Write-Host '本地服务已启动；刷新页面后可选择 deepseek-flash。'
} finally {
    if ($null -eq $previousKey) {
        Remove-Item Env:GAMEPILOT_AGENT_API_KEY -ErrorAction SilentlyContinue
    } else {
        $env:GAMEPILOT_AGENT_API_KEY = $previousKey
    }
    $env:WSLENV = $previousWslEnv
    Pop-Location
}
