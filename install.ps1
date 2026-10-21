param(
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'CloudVault'),
    [string]$PythonExecutable,
    [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Program (exit $LASTEXITCODE)" }
}
$sourceDir = $PSScriptRoot
if (-not $PythonExecutable) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $PythonExecutable = (& py -3 -c 'import sys; print(sys.executable)')
        if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer from python.org.' }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $PythonExecutable = (Get-Command python).Source
    } else { throw 'Install Python 3.11 or newer from python.org, including Tcl/Tk.' }
}
Invoke-Checked $PythonExecutable @('-c','import sys,tkinter; assert sys.version_info >= (3,11), "Python 3.11+ is required"')
if (-not (Get-Command gh -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath (Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe'))) {
    throw 'Install GitHub CLI from https://cli.github.com and run this installer again.'
}
$appDir = Join-Path $InstallDirectory 'app'
$runtimeDir = Join-Path $InstallDirectory 'runtime'
New-Item -ItemType Directory -Path $appDir -Force | Out-Null
$files = @('staging.py','capacity.py','repositories.py','history_remote.py','task_cleanup.py','management_ui.py','app.pyw','engine.py','cache_cleanup.py','background.py','selection.py','recovery.py','recovery_ui.py','configuration.py','onboarding.py','restore_incremental.py','backup.ico','backup.png','requirements.txt','README.md','SECURITY.md','LICENSE')
foreach ($name in $files) {
    $from = Join-Path $sourceDir $name
    $to = Join-Path $appDir $name
    if ([IO.Path]::GetFullPath($from) -ne [IO.Path]::GetFullPath($to)) { Copy-Item -LiteralPath $from -Destination $to -Force }
}
Invoke-Checked $PythonExecutable @('-m','venv',$runtimeDir)
$runtimePython = Join-Path $runtimeDir 'Scripts\python.exe'
Invoke-Checked $runtimePython @('-m','pip','install','--disable-pip-version-check','-r',(Join-Path $appDir 'requirements.txt'))
Invoke-Checked $runtimePython @('-c','import requests,cryptography,tkinter; print("Dependencies ready.")')
if (-not $NoShortcut) {
    $shellObject = New-Object -ComObject WScript.Shell
    $desktopDir = [Environment]::GetFolderPath('Desktop')
    $shortcut = $shellObject.CreateShortcut((Join-Path $desktopDir 'Cloud Vault.lnk'))
    $shortcut.TargetPath = Join-Path $runtimeDir 'Scripts\pythonw.exe'
    $shortcut.Arguments = '"' + (Join-Path $appDir 'app.pyw') + '"'
    $shortcut.WorkingDirectory = $appDir
    $shortcut.IconLocation = Join-Path $appDir 'backup.ico'
    $shortcut.Description = 'Encrypted incremental backups to your private GitHub repository'
    $shortcut.Save()
}
Write-Host "Installed in $appDir. Open Cloud Vault from your desktop to configure your private repository."
