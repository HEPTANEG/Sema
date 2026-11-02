$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")

$paths = @()
if ($userPath) {
    $paths = $userPath -split ";"
}

if ($paths -contains $projectRoot) {
    Write-Host "Sema and sema-eval are already on your user PATH."
    exit 0
}

$newPath = if ($userPath) {
    "$userPath;$projectRoot"
} else {
    $projectRoot
}

[Environment]::SetEnvironmentVariable("Path", $newPath, "User")
Write-Host "Added Sema to your user PATH."
Write-Host "Open a new PowerShell window, then run: sema or sema-eval"
