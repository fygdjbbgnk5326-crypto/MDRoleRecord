$ErrorActionPreference = 'Stop'

$runtimeRoot = Join-Path $PSScriptRoot 'python_runtime'
$generator = Join-Path $runtimeRoot 'action_cache_generator.py'
if (-not (Test-Path -LiteralPath $generator)) {
    throw "Generator script not found: $generator"
}

$pythonPath = (& py -3.14 -c 'import sys; print(sys.executable)' | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python 3.14 is required. Install python_runtime/requirements.txt first.'
}
& $pythonPath -c 'import UnityPy'
if ($LASTEXITCODE -ne 0) {
    throw 'UnityPy is missing from Python 3.14. Install python_runtime/requirements.txt.'
}

$traceDir = Join-Path $env:LOCALAPPDATA 'MuseDashChart\role_traces'
[System.IO.Directory]::CreateDirectory($traceDir) | Out-Null
$settingsFile = Join-Path $traceDir 'generator_settings.json'
$cacheRoot = Join-Path $PSScriptRoot 'action_cache'
[System.IO.Directory]::CreateDirectory($cacheRoot) | Out-Null
$settings = @{
    enabled = $true
    python_executable = $pythonPath
    generator_script = $generator
    config_path = ''
    cache_root = $cacheRoot
}
$json = $settings | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText($settingsFile, $json, [System.Text.UTF8Encoding]::new($false))
Write-Output "Configured chart generator: $settingsFile"
