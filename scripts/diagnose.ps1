<#
.SYNOPSIS
    Diagnose one or more build logs on Windows (PowerShell 5.1 or 7+).
.EXAMPLE
    .\scripts\diagnose.ps1 build.log
    $env:PIPELINEDOCTOR_FORMAT = "json"; .\scripts\diagnose.ps1 build.log
#>
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$LogFiles
)

$ErrorActionPreference = "Stop"
$format = if ($env:PIPELINEDOCTOR_FORMAT) { $env:PIPELINEDOCTOR_FORMAT } else { "text" }
$python = if ($env:PIPELINEDOCTOR_PYTHON) { $env:PIPELINEDOCTOR_PYTHON } else { "python" }

foreach ($log in $LogFiles) {
    if (-not (Test-Path -LiteralPath $log)) {
        Write-Error "Log file not found: $log. Fix hint: check the path or use a full path like C:\builds\build.log"
        exit 3
    }
}

if (Get-Command pipelinedoctor -ErrorAction SilentlyContinue) {
    & pipelinedoctor analyze @LogFiles --format $format
} else {
    # Run from source without installing.
    $env:PYTHONPATH = Join-Path $PSScriptRoot ".."
    & $python -m pipelinedoctor analyze @LogFiles --format $format
}
exit $LASTEXITCODE
