param([string]$PythonPath, [switch]$SkipConnect)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') { throw 'Version 0.2 supports Windows only.' }
$pluginSource = $PSScriptRoot
if (-not $PythonPath) {
    $pythonCandidates = @()
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython) { $pythonCandidates += $bundledPython }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notlike '*WindowsApps*') { $pythonCandidates += $pythonCommand.Source }
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        $resolvedPython = & $pythonLauncher.Source -3 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -eq 0) { $pythonCandidates += $resolvedPython }
    }
    foreach ($candidatePython in $pythonCandidates) {
        & $candidatePython -c 'import sys; raise SystemExit(not ((3,11) <= sys.version_info[:2] < (3,15)))' 2>$null
        if ($LASTEXITCODE -eq 0) { $PythonPath = $candidatePython; break }
    }
}
if (-not $PythonPath) { throw 'Install Python 3.11-3.14 from python.org, then run Install.cmd again.' }
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'TelegramReader\runtime'
$runtimePython = Join-Path $runtimeRoot 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) {
    Write-Host 'Creating an isolated Python environment...'
    & $PythonPath -m venv $runtimeRoot
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python environment.' }
}
Write-Host 'Installing Telegram Reader dependencies...'
& $runtimePython -m pip install --disable-pip-version-check -r (Join-Path $pluginSource 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $runtimePython -m pip install --disable-pip-version-check --no-deps $pluginSource
if ($LASTEXITCODE -ne 0) { throw 'Telegram Reader installation failed.' }
& $runtimePython (Join-Path $pluginSource 'scripts\register_plugin.py') --python $runtimePython
if ($LASTEXITCODE -ne 0) { throw 'Codex plugin registration failed.' }
Write-Host 'Installed. Open a new Codex task to use Telegram Reader.'
if (-not $SkipConnect) { & (Join-Path $env:USERPROFILE 'plugins\telegram-reader\Connect-local.ps1') }
