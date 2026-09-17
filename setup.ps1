# setup.ps1
#
# RetroBat Display Companion setup
#
# - Downloads the pinned official mpv Windows build
# - Verifies its SHA-256 checksum
# - Checks for Python 3
# - Checks/enables pip
# - Installs packages from requirements.txt if present

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$MpvVersion = "0.41.0"
$MpvArchive = "mpv-v$MpvVersion-x86_64-pc-windows-msvc.zip"
$MpvUrl = "https://github.com/mpv-player/mpv/releases/download/v$MpvVersion/$MpvArchive"
$MpvSha256 = "4e197f729f5071c6772f35fffd96e0f36e3e8a044bd9479b136bb09b7c6a80ff"

$MpvDir = Join-Path $ScriptDir "mpv"
$MpvExe = Join-Path $MpvDir "mpv.exe"
$RequirementsFile = Join-Path $ScriptDir "requirements.txt"

Write-Host ""
Write-Host "RetroBat Display Companion setup"
Write-Host "================================"
Write-Host ""

function Install-Mpv {
    $installRequired = $true

    if (Test-Path $MpvExe) {
        try {
            $versionOutput = & $MpvExe --version 2>$null | Select-Object -First 1

            if ($versionOutput -match [regex]::Escape($MpvVersion)) {
                Write-Host "[OK] mpv $MpvVersion is already installed."
                $installRequired = $false
            }
            else {
                Write-Host "[INFO] Existing mpv version does not match $MpvVersion."
                Write-Host "       It will be replaced."
            }
        }
        catch {
            Write-Host "[INFO] Existing mpv could not be checked and will be replaced."
        }
    }

    if (-not $installRequired) {
        return
    }

    $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) `
        ("retrobat-display-companion-" + [guid]::NewGuid().ToString("N"))

    $zipFile = Join-Path $tempDir $MpvArchive

    try {
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null

        Write-Host "[INFO] Downloading mpv $MpvVersion..."
        Invoke-WebRequest `
            -Uri $MpvUrl `
            -OutFile $zipFile `
            -UseBasicParsing

        Write-Host "[INFO] Verifying download..."

        $actualHash = (Get-FileHash `
            -Path $zipFile `
            -Algorithm SHA256).Hash.ToLowerInvariant()

        if ($actualHash -ne $MpvSha256.ToLowerInvariant()) {
            throw @"
mpv download checksum does not match.

Expected: $MpvSha256
Actual:   $actualHash

The archive will not be installed.
"@
        }

        if (Test-Path $MpvDir) {
            Remove-Item $MpvDir -Recurse -Force
        }

        New-Item -ItemType Directory -Path $MpvDir -Force | Out-Null

        Write-Host "[INFO] Extracting mpv..."
        Expand-Archive `
            -Path $zipFile `
            -DestinationPath $MpvDir `
            -Force

        # Cope with an archive containing an enclosing directory.
        if (-not (Test-Path $MpvExe)) {
            $foundMpv = Get-ChildItem `
                -Path $MpvDir `
                -Filter "mpv.exe" `
                -File `
                -Recurse |
                Select-Object -First 1

            if ($null -eq $foundMpv) {
                throw "mpv.exe was not found in the downloaded archive."
            }

            $extractedDir = $foundMpv.Directory.FullName

            Get-ChildItem -Path $extractedDir -Force |
                Move-Item -Destination $MpvDir -Force

            if ($extractedDir -ne $MpvDir -and (Test-Path $extractedDir)) {
                Remove-Item $extractedDir -Recurse -Force
            }
        }

        if (-not (Test-Path $MpvExe)) {
            throw "mpv.exe was not installed successfully."
        }

        Write-Host "[OK] mpv $MpvVersion installed."
    }
    finally {
        if (Test-Path $tempDir) {
            Remove-Item $tempDir -Recurse -Force
        }
    }
}

function Test-Python {
    Write-Host "[INFO] Checking Python 3..."

    try {
        $pythonVersion = & py -3 --version 2>&1

        if ($LASTEXITCODE -ne 0) {
            throw "Python launcher returned exit code $LASTEXITCODE."
        }

        Write-Host "[OK] $pythonVersion"
    }
    catch {
        throw @"
Python 3 was not found.

Install Python 3 for Windows and ensure the 'py' launcher is available,
then run setup.ps1 again.
"@
    }
}

function Test-Pip {
    Write-Host "[INFO] Checking pip..."

    & py -3 -m pip --version *> $null

    if ($LASTEXITCODE -ne 0) {
        Write-Host "[INFO] pip was not found. Attempting to enable it..."

        & py -3 -m ensurepip --upgrade

        if ($LASTEXITCODE -ne 0) {
            throw "pip could not be installed."
        }
    }

    $pipVersion = & py -3 -m pip --version
    Write-Host "[OK] $pipVersion"
}

function Install-PythonRequirements {
    if (-not (Test-Path $RequirementsFile)) {
        Write-Host "[OK] No requirements.txt present; no Python packages to install."
        return
    }

    Write-Host "[INFO] Installing Python dependencies from requirements.txt..."

    & py -3 -m pip install -r $RequirementsFile

    if ($LASTEXITCODE -ne 0) {
        throw "Python package installation failed."
    }

    Write-Host "[OK] Python dependencies installed."
}

try {
    Install-Mpv

    Write-Host ""

    Test-Python
    Test-Pip
    Install-PythonRequirements

    Write-Host ""
    Write-Host "Setup complete."
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "Setup failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    exit 1
}