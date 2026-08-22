<#
.SYNOPSIS
    One-click installer for this Octo Updater fork.

.DESCRIPTION
    Bootstraps a real Python (the Microsoft Store stub does not count),
    installs PyInstaller and certifi, then runs octo_setup.py, which patches
    Octo Updater, finds your OctoWoW folder, backs it up, points the updater at
    it and builds OctoUpdater.exe.

    No administrator rights required - everything installs per-user.
    Safe to re-run: every step is idempotent.

    Octo Updater itself is by rebasedkon:
    https://github.com/rebasedkon/octo-updater

.PARAMETER GameFolder
    Your OctoWoW folder. Omit it and the installer detects one, or asks.

.PARAMETER MigrateFrom
    Another OctoWoW install to copy every addon and all settings from.

.EXAMPLE
    .\install.ps1

.EXAMPLE
    .\install.ps1 -GameFolder "D:\Octowow"

.EXAMPLE
    .\install.ps1 -GameFolder "E:\Games\OctoWoW" -MigrateFrom "D:\Octowow"
#>
[CmdletBinding()]
param(
    [string]$Repo,
    [string]$GameFolder,
    [string]$MigrateFrom,
    [string]$SourceRepo = "eqomdx/EquadisOctoUpdater",
    [switch]$NoDownload,
    [switch]$NoBuild,
    [switch]$NoConfig,
    [switch]$KeepAddonRecords,
    [switch]$Yes,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Write-Head($text) {
    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor DarkCyan
    Write-Host " $text" -ForegroundColor Cyan
    Write-Host ("=" * 70) -ForegroundColor DarkCyan
}
function Write-Step($text) { Write-Host "  -> $text" -ForegroundColor Gray }
function Write-Ok($text)   { Write-Host "  OK $text" -ForegroundColor Green }

# ---------------------------------------------------------------------------
# Find a real Python.
#
# The entries under WindowsApps are the Microsoft Store stub: they sit on PATH
# and look like an interpreter, but running one just prints "Python was not
# found; run without arguments to install from the Microsoft Store". Skip them.
# ---------------------------------------------------------------------------
function Find-RealPython {
    $candidates = @()

    $roots = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        "C:\Program Files\Python313", "C:\Program Files\Python312",
        "C:\Program Files\Python311", "C:\Program Files\Python310",
        "C:\Python313", "C:\Python312", "C:\Python311", "C:\Python310"
    )
    foreach ($r in $roots) {
        if (Test-Path $r) {
            $candidates += Get-ChildItem -Path $r -Filter python.exe -Recurse `
                -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
        }
    }
    foreach ($c in (Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        $candidates += $c.Source
    }

    $seen = @{}
    foreach ($p in $candidates) {
        if (-not $p) { continue }
        if ($p -like "*\WindowsApps\*") { continue }
        $key = $p.ToLower()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        if (-not (Test-Path $p)) { continue }
        try {
            $v = & $p -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $v) { continue }
        $parts = $v.Trim().Split(".")
        if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
            return [pscustomobject]@{ Path = $p; Version = $v.Trim() }
        }
    }
    return $null
}

function Install-Python {
    Write-Step "No suitable Python found. Installing Python 3.12 (per-user)..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw ("winget is not available on this machine, so Python cannot be " +
               "installed automatically.`n" +
               "Install Python 3.10 or newer from https://www.python.org/downloads/ " +
               "(tick 'Add python.exe to PATH') and run this installer again.")
    }
    & winget install --id Python.Python.3.12 --source winget --scope user `
        --silent --accept-package-agreements --accept-source-agreements `
        --disable-interactivity
    # winget exit codes vary between versions - verify by looking again.
    $py = Find-RealPython
    if (-not $py) {
        throw ("Python is still not detected after the install.`n" +
               "Install it manually from https://www.python.org/downloads/ " +
               "and run this installer again.")
    }
    return $py
}

# ---------------------------------------------------------------------------

Write-Head "Octo Updater - fork installer"
Write-Host "  Original project by rebasedkon:" -ForegroundColor DarkGray
Write-Host "    https://github.com/rebasedkon/octo-updater" -ForegroundColor DarkGray

# Where this script lives. Empty when the script was piped straight into
# PowerShell (irm ... | iex) - then everything is fetched into a temp folder.
# Captured here at script scope on purpose: inside a function $MyInvocation
# describes the function call, not the script file.
$script:selfPath = $MyInvocation.MyCommand.Path
$here = $null
if ($script:selfPath) {
    $here = Split-Path -Parent $script:selfPath
}
$piped = -not $here
if ($piped) {
    $here = Join-Path ([IO.Path]::GetTempPath()) "EquadisOctoUpdater"
    New-Item -ItemType Directory -Force $here | Out-Null
}

# Where a downloaded project should land: the folder above install/, or this
# folder when the installer is sitting loose.
if ((Split-Path -Leaf $here) -eq "install") {
    $projectDir = Split-Path -Parent $here
} else {
    $projectDir = $here
}

function Find-Project($start) {
    $d = $start
    for ($i = 0; $i -lt 3; $i++) {
        if (Test-Path (Join-Path $d "octo_updater.py")) { return $d }
        $up = Split-Path -Parent $d
        if (-not $up -or $up -eq $d) { break }
        $d = $up
    }
    return $null
}

function Find-Setup($project) {
    foreach ($c in @((Join-Path $here "octo_setup.py"),
                     (Join-Path (Join-Path $project "install") "octo_setup.py"),
                     (Join-Path $project "octo_setup.py"))) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function Get-Project($dest) {
    # Fetch the project from GitHub and unpack it into $dest. Only ever pulls
    # from $SourceRepo over HTTPS, and never overwrites the script that is
    # currently running.
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("octoupd_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force $tmp | Out-Null
    $zip = Join-Path $tmp "project.zip"

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ok = $false
    foreach ($branch in @("main", "master")) {
        $url = "https://github.com/$SourceRepo/archive/refs/heads/$branch.zip"
        Write-Step "downloading $url"
        try {
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing `
                -MaximumRedirection 5 -ErrorAction Stop
        } catch {
            Write-Host "     branch '$branch' not available" -ForegroundColor DarkGray
            continue
        }
        # An empty repository or a missing branch can still answer with an HTML
        # page rather than an error, so check for the zip signature (PK\003\004)
        # instead of trusting the status code.
        $sig = $null
        try {
            $fs = [IO.File]::OpenRead($zip)
            $buf = New-Object byte[] 4
            [void]$fs.Read($buf, 0, 4)
            $fs.Close()
            $sig = $buf
        } catch { }
        if (-not $sig -or $sig[0] -ne 0x50 -or $sig[1] -ne 0x4B -or
            $sig[2] -ne 0x03 -or $sig[3] -ne 0x04) {
            Write-Host "     branch '$branch' did not return a zip archive" `
                -ForegroundColor DarkGray
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            continue
        }
        $ok = $true
        break
    }
    if (-not $ok) {
        throw ("Could not download the project from " +
               "https://github.com/$SourceRepo`n`n" +
               "The repository may be empty, private, or renamed - or you may " +
               "be offline.`nPush the project to that repository first, or " +
               "download it by hand and`nrun the installer from inside it.")
    }

    try {
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    } catch {
        throw ("The download from https://github.com/$SourceRepo could not be " +
               "unpacked:`n  " + $_.Exception.Message)
    }
    $root = Get-ChildItem $tmp -Directory |
            Where-Object { Test-Path (Join-Path $_.FullName "octo_updater.py") } |
            Select-Object -First 1
    if (-not $root) {
        throw ("The downloaded archive does not contain octo_updater.py. " +
               "Check that`nhttps://github.com/$SourceRepo has the project " +
               "files at its top level.")
    }

    New-Item -ItemType Directory -Force $dest | Out-Null
    $running = $script:selfPath
    foreach ($item in Get-ChildItem $root.FullName -Force) {
        $to = Join-Path $dest $item.Name
        if ($item.PSIsContainer) {
            Copy-Item $item.FullName $to -Recurse -Force
        } elseif ($running -and ((Split-Path -Leaf $running) -eq $item.Name) -and
                  ((Split-Path -Parent $running) -eq $dest)) {
            continue   # never overwrite the script that is executing
        } else {
            Copy-Item $item.FullName $to -Force
        }
    }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "project downloaded to $dest"
}

if (-not $Repo) { $Repo = Find-Project $here }
$setup = if ($Repo) { Find-Setup $Repo } else { $null }

if (-not $Repo -or -not $setup) {
    Write-Head "Getting Octo Updater"
    if ($piped) {
        Write-Host "  Fetching the project (nothing was downloaded yet)." -ForegroundColor Gray
    } else {
        Write-Host "  octo_updater.py is not here, so the project needs downloading." -ForegroundColor Gray
        Write-Host "  Source: https://github.com/$SourceRepo" -ForegroundColor Gray
    }
    if (-not $NoDownload) {
        Get-Project $projectDir
        $Repo  = Find-Project $projectDir
        if (-not $Repo) { $Repo = $projectDir }
        $setup = Find-Setup $Repo
    }
}

if (-not $Repo -or -not (Test-Path (Join-Path $Repo "octo_updater.py"))) {
    throw ("octo_updater.py was not found and could not be downloaded.`n`n" +
           "Get the project from https://github.com/$SourceRepo, then run`n" +
           "install\INSTALL.cmd from inside the folder you extracted.`n`n" +
           "Or point the installer at it: .\install.ps1 -Repo `"C:\path\to\project`"")
}
if (-not $setup) {
    throw "octo_setup.py was not found next to install.ps1 or in $Repo\install."
}
$repo = $Repo

Write-Host ""
Write-Host "  Octo Updater folder : $repo"

Write-Head "Step 1 of 3 - Python"
$py = Find-RealPython
if (-not $py) { $py = Install-Python }
Write-Ok "Python $($py.Version)"
Write-Host "     $($py.Path)" -ForegroundColor DarkGray

Write-Head "Step 2 of 3 - build tools"
Write-Step "installing PyInstaller and certifi"
& $py.Path -m pip install --upgrade pip --quiet --no-warn-script-location
& $py.Path -m pip install pyinstaller certifi --quiet --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    throw "pip could not install PyInstaller/certifi. Check your connection."
}
$pyiVer = & $py.Path -c "import PyInstaller; print(PyInstaller.__version__)"
Write-Ok "PyInstaller $pyiVer"

Write-Head "Step 3 of 3 - patch, configure and build"
$a = @($setup, "--repo", $repo)
if ($GameFolder)       { $a += @("--game-folder", $GameFolder) }
if ($MigrateFrom)      { $a += @("--migrate-from", $MigrateFrom) }
if ($NoBuild)          { $a += "--no-build" }
if ($NoConfig)         { $a += "--no-config" }
if ($KeepAddonRecords) { $a += "--keep-addon-records" }
if ($Yes)              { $a += "--yes" }
if ($Force)            { $a += "--force" }

& $py.Path $a
if ($LASTEXITCODE -ne 0) { throw "Setup failed (exit $LASTEXITCODE)." }

$exe = Join-Path $repo "OctoUpdater.exe"
Write-Head "All done"
if (Test-Path $exe) {
    Write-Host "  Run: $exe" -ForegroundColor Green
} else {
    Write-Host "  Setup finished (the exe build was skipped)." -ForegroundColor Yellow
}
Write-Host ""
