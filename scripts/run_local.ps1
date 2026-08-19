param(
    [switch]$SkipLlm,
    [string]$LogLevel = "INFO"
)

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

conda activate data_gov_agent
if ($LASTEXITCODE -ne 0) {
    Write-Error "Could not activate data_gov_agent conda environment."
    exit 1
}

$args_list = @()
if ($SkipLlm) { $args_list += "--skip-llm" }
$args_list += "--log-level"
$args_list += $LogLevel

python main.py @args_list
