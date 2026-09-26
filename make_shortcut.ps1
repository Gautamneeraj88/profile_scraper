# Creates the Desktop shortcut.  Called by SETUP.bat.
# Kept as its own file because quoting a COM call inside a .bat is a
# reliable way to get it wrong.

param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][string]$WorkDir,
    [string]$Name = "LinkedIn Enricher"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Target)) {
    Write-Output "Shortcut target does not exist: $Target"
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "$Name.lnk"

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $Target
$sc.WorkingDirectory = $WorkDir
$sc.Description = "Fill in missing candidate details from LinkedIn"
$sc.Save()

Write-Output "Shortcut created: $link"
Write-Output "  -> $Target"
exit 0
