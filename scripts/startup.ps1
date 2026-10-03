param([Parameter(Mandatory)][string]$Python, [Parameter(Mandatory)][string]$PluginRoot, [switch]$Remove)
$ErrorActionPreference = 'Stop'
$startupDirectory = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDirectory 'Codex 工作回顾.lnk'
if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
    return
}
$interpreter = (Resolve-Path -LiteralPath $Python).Path
$windowlessInterpreter = Join-Path (Split-Path -Parent $interpreter) 'pythonw.exe'
if (Test-Path -LiteralPath $windowlessInterpreter) { $interpreter = $windowlessInterpreter }
$pluginDirectory = (Resolve-Path -LiteralPath $PluginRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $pluginDirectory 'recap/__main__.py'))) { throw 'Missing plugin runtime' }
$shellObject = New-Object -ComObject WScript.Shell
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $interpreter
$shortcut.Arguments = '-c "from recap.runtime import ensure_service; ensure_service()"'
$shortcut.WorkingDirectory = $pluginDirectory
$shortcut.WindowStyle = 7
$shortcut.Description = '登录后独立启动工作回顾后台，沿用已选显示方式，无需网页或端口'
$shortcut.Save()
Write-Output $shortcutPath
