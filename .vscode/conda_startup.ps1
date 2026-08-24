# VS Code / Cursor terminal and debugger startup script.
# Reads CONDA_ENV_NAME from .env and runs: conda activate <name>
#
# Used by terminal.integrated.profiles and automationProfile in settings.json.

$ErrorActionPreference = "Continue"

if ($PSScriptRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
} else {
    $ProjectRoot = (Get-Location).Path
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)$") {
            $value = $Matches[1].Trim()
            if (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            ) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $null
}

function Find-CondaRoot {
    $condaCmd = Get-Command conda -ErrorAction SilentlyContinue
    if ($condaCmd) {
        $source = $condaCmd.Source
        $dir = Split-Path -Parent $source
        $leaf = Split-Path -Leaf $dir
        if ($leaf -eq "condabin" -or $leaf -eq "Scripts") {
            return (Split-Path -Parent $dir)
        }
        return $dir
    }

    $candidates = @(
        "$env:USERPROFILE\anaconda3",
        "$env:USERPROFILE\miniconda3",
        "$env:USERPROFILE\miniforge3",
        "$env:USERPROFILE\mambaforge",
        "$env:LOCALAPPDATA\anaconda3",
        "$env:LOCALAPPDATA\miniconda3",
        "$env:LOCALAPPDATA\miniforge3",
        "$env:LOCALAPPDATA\mambaforge",
        "C:\ProgramData\anaconda3",
        "C:\ProgramData\miniconda3",
        "C:\ProgramData\miniforge3",
        "C:\anaconda3",
        "C:\miniconda3"
    )
    foreach ($root in $candidates) {
        if (
            (Test-Path -LiteralPath (Join-Path $root "Scripts\conda.exe")) -or
            (Test-Path -LiteralPath (Join-Path $root "condabin\conda.bat"))
        ) {
            return $root
        }
    }
    return $null
}

$envFile = Join-Path $ProjectRoot ".env"
$envName = Get-DotEnvValue -Path $envFile -Key "CONDA_ENV_NAME"
if (-not $envName) {
    $envName = "data_gov_agent"
}

$condaRoot = Find-CondaRoot
if (-not $condaRoot) {
    Write-Warning "conda was not found on PATH or in common install locations. Skip conda activate."
    Write-Warning "Install Anaconda/Miniconda, or start Cursor from an Anaconda Prompt."
    return
}

$hook = Join-Path $condaRoot "shell\condabin\conda-hook.ps1"
if (Test-Path -LiteralPath $hook) {
    . $hook
} else {
    $condaExe = Join-Path $condaRoot "Scripts\conda.exe"
    if (-not (Test-Path -LiteralPath $condaExe)) {
        $condaExe = Join-Path $condaRoot "condabin\conda.bat"
    }
    if (-not (Test-Path -LiteralPath $condaExe)) {
        Write-Warning "Found conda root '$condaRoot' but could not load the PowerShell hook."
        return
    }
    (& $condaExe "shell.powershell" "hook") | Out-String | Invoke-Expression
}

conda activate $envName
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    Write-Warning "conda activate '$envName' failed. Check CONDA_ENV_NAME in .env and that the env exists (conda env list)."
} else {
    Write-Host "Activated conda env '$envName'."
}
