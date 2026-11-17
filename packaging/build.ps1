[CmdletBinding()]
param(
    [string]$TestPythonPath = "",
    [string]$PackagingVenvPath = "build\venv-packaging",
    [string]$IsccPath = "",
    [switch]$CompileInstaller,
    [switch]$SkipTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PyProjectPath = Join-Path $RepoRoot "pyproject.toml"
$SpecPath = Join-Path $PSScriptRoot "ortodocs.spec"
$VersionInfoPath = Join-Path $PSScriptRoot "version_info.txt"
$ManifestPath = Join-Path $PSScriptRoot "ortodocs.manifest"
$IssPath = Join-Path $PSScriptRoot "installer\OrtoDocs.iss"
$RequirementsPath = Join-Path $PSScriptRoot "requirements-packaging.txt"

function Resolve-PathRelativeToRepo {
    param([string]$InputPath)
    if ([System.IO.Path]::IsPathRooted($InputPath)) {
        return $InputPath
    }
    return (Join-Path $RepoRoot $InputPath)
}

function Resolve-TestPython {
    param([string]$ExplicitPath)
    if ($ExplicitPath) {
        $resolved = Resolve-PathRelativeToRepo $ExplicitPath
        if (-not (Test-Path $resolved)) {
            throw "Python-ul pentru teste nu există: $resolved"
        }
        return $resolved
    }

    $activeVenv = $env:VIRTUAL_ENV
    if ($activeVenv) {
        $activePython = Join-Path $activeVenv "Scripts\python.exe"
        if (Test-Path $activePython) {
            return $activePython
        }
    }

    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        return $cmd.Source
    }

    throw "Nu s-a găsit interpretorul Python pentru teste. Specificați -TestPythonPath."
}

function Get-CanonicalVersion {
    param([string]$PythonExe)
    $code = "import tomllib, pathlib; print(tomllib.loads(pathlib.Path(r'$PyProjectPath').read_text(encoding='utf-8'))['project']['version'])"
    $version = ([string](& $PythonExe -c $code)).Trim()
    if ($LASTEXITCODE -ne 0 -or -not ($version -match '^\d+\.\d+\.\d+$')) {
        throw "Nu s-a putut citi versiunea semantică (X.Y.Z) din pyproject.toml: '$version'"
    }
    return $version
}

function Assert-VersionConsistency {
    param([string]$Version)
    $parts = $Version.Split(".")
    $tupleText = "($($parts[0]), $($parts[1]), $($parts[2]), 0)"
    $versionInfoText = Get-Content -Path $VersionInfoPath -Raw -Encoding UTF8
    if ($versionInfoText -notmatch [regex]::Escape("filevers=$tupleText")) {
        throw "packaging\version_info.txt nu corespunde cu versiunea $Version (lipsea filevers=$tupleText)."
    }
    if ($versionInfoText -notmatch [regex]::Escape("StringStruct('ProductVersion', '$Version')")) {
        throw "packaging\version_info.txt nu corespunde cu ProductVersion '$Version'."
    }

    $manifestText = Get-Content -Path $ManifestPath -Raw -Encoding UTF8
    if ($manifestText -notmatch [regex]::Escape("version=`"$Version.0`"")) {
        throw "packaging\ortodocs.manifest nu corespunde cu versiunea $Version.0."
    }

    $issText = Get-Content -Path $IssPath -Raw -Encoding UTF8
    if ($issText -notmatch [regex]::Escape("#define AppVersion `"$Version`"")) {
        throw "packaging\installer\OrtoDocs.iss nu are implicit AppVersion `"$Version`"."
    }
}

function Assert-PackagingEnvironment {
    param([string]$PkgPythonExe)
    if (-not (Test-Path $PkgPythonExe)) {
        throw "Mediul separat de packaging lipsește: $PkgPythonExe. Creați un venv separat de build și instalați din $RequirementsPath."
    }

    $pyVersion = ([string](& $PkgPythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $pyVersion.StartsWith("3.14.")) {
        throw "Mediul de packaging necesită Python 3.14.x (detectat: '$pyVersion')."
    }

    $expectedPins = @{}
    foreach ($line in (Get-Content -Path $RequirementsPath -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed -match '^([A-Za-z0-9_.-]+)==([^\s]+)$') {
            $expectedPins[$Matches[1]] = $Matches[2]
        }
    }

    $checkScript = @'
import importlib.metadata, json, os, sys
reqs = json.loads(os.environ["ORTODOCS_PKG_PINS"])
errors = []
for pkg, expected in reqs.items():
    try:
        actual = importlib.metadata.version(pkg)
        if actual != expected:
            errors.append(f"{pkg}: așteptat {expected}, instalat {actual}")
    except importlib.metadata.PackageNotFoundError:
        errors.append(f"{pkg}: neinstalat")
if errors:
    print(chr(10).join(errors))
    sys.exit(1)
'@
    $previousPins = $env:ORTODOCS_PKG_PINS
    $previousEap = $ErrorActionPreference
    try {
        $env:ORTODOCS_PKG_PINS = $expectedPins | ConvertTo-Json -Compress
        $ErrorActionPreference = "Continue"
        $checkOutput = ($checkScript | & $PkgPythonExe - 2>&1) -join "`n"
        $checkExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousEap
        $env:ORTODOCS_PKG_PINS = $previousPins
    }
    if ($checkExit -ne 0) {
        throw "Mediul de packaging nu respectă packaging\requirements-packaging.txt:`n$checkOutput"
    }
    Write-Host "Mediu packaging verificat: Python $pyVersion ($PkgPythonExe)"
}

function Invoke-TestSuite {
    param([string]$TestPythonExe)
    $isolatedLocalAppData = Join-Path $RepoRoot "build\isolated-localappdata"
    New-Item -ItemType Directory -Force -Path $isolatedLocalAppData | Out-Null

    $previousLocalAppData = $env:LOCALAPPDATA
    $previousPythonPath = $env:PYTHONPATH
    $previousEap = $ErrorActionPreference
    try {
        $env:LOCALAPPDATA = $isolatedLocalAppData
        $env:PYTHONPATH = Join-Path $RepoRoot "src"
        $ErrorActionPreference = "Continue"
        Write-Host "Rulare suită completă de teste cu LOCALAPPDATA izolat..."
        Push-Location $RepoRoot
        try {
            & $TestPythonExe -m unittest discover -s tests -v
            if ($LASTEXITCODE -ne 0) {
                throw "Suita de teste a eșuat (exit code $LASTEXITCODE). Build-ul a fost oprit."
            }
        }
        finally {
            Pop-Location
        }
    }
    finally {
        $ErrorActionPreference = $previousEap
        $env:LOCALAPPDATA = $previousLocalAppData
        $env:PYTHONPATH = $previousPythonPath
    }
}

function Assert-BuiltBundle {
    param([string]$DistBundleDir)

    $requiredFiles = @(
        "OrtoDocs.exe",
        "_internal\python314.dll",
        "_internal\VCRUNTIME140.dll",
        "_internal\VCRUNTIME140_1.dll",
        "_internal\ucrtbase.dll",
        "_internal\sqlite3.dll",
        "_internal\_sqlite3.pyd",
        "_internal\_tkinter.pyd",
        "_internal\ortodocs.ico",
        "_internal\pyproject.toml",
        "_internal\docx\templates\default.docx",
        "_internal\docx\templates\default-styles.xml",
        "_internal\licenses\THIRD_PARTY_LICENSES.txt",
        "_internal\licenses\tk_date_entry-EUPL-1.2-LICENSE.txt",
        "_internal\licenses\python-docx-MIT-LICENSE.txt",
        "_internal\licenses\lxml-BSD-LICENSE.txt",
        "_internal\licenses\lxml-THIRD_PARTY-LICENSES.txt",
        "_internal\licenses\typing_extensions-PSF-LICENSE.txt"
    )

    foreach ($rel in $requiredFiles) {
        $full = Join-Path $DistBundleDir $rel
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
            throw "Verificarea build-ului a eșuat — lipsește fișierul obligatoriu: $full"
        }
    }

    $internalDir = Join-Path $DistBundleDir "_internal"
    $hasEmbeddedTclTk9 = (Test-Path (Join-Path $internalDir "tcl90.dll")) -and (Test-Path (Join-Path $internalDir "tcl9tk90.dll"))
    $tclDirs = @(Get-ChildItem -Path $internalDir -Directory | Where-Object { $_.Name -match '^(_tcl_data|tcl9)' })
    $tkDirs = @(Get-ChildItem -Path $internalDir -Directory | Where-Object { $_.Name -match '^(_tk_data|tk9)' })
    if (-not $hasEmbeddedTclTk9 -and ($tclDirs.Count -eq 0 -or $tkDirs.Count -eq 0)) {
        throw "Verificarea build-ului a eșuat — nici DLL-urile Tcl/Tk 9 (tcl90.dll/tcl9tk90.dll), nici directoarele de date Tcl/Tk nu au fost găsite în $internalDir."
    }

    $forbiddenMatches = @(Get-ChildItem -Path $DistBundleDir -Recurse -File | Where-Object {
        $_.Extension -in '.db', '.sqlite', '.sqlite3' -or
        $_.Name -match '\.db-(wal|shm|journal)$' -or
        $_.FullName -match '\\(tests|backup)\\' -or
        $_.Name -eq 'protocol_test.docx'
    })
    if ($forbiddenMatches.Count -gt 0) {
        $paths = ($forbiddenMatches | Select-Object -ExpandProperty FullName) -join ", "
        throw "Verificarea build-ului a eșuat — au fost detectate fișiere interzise în dist: $paths"
    }

    Write-Host "Verificarea bundle-ului dist\OrtoDocs a reușit."
}

function Find-Iscc {
    param([string]$ExplicitPath)
    if ($ExplicitPath) {
        if (-not (Test-Path $ExplicitPath)) {
            throw "ISCC.exe nu a fost găsit la calea specificată: $ExplicitPath"
        }
        return $ExplicitPath
    }
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        return $cmd.Source
    }
    $defaultCandidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $defaultCandidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    throw "ISCC.exe (Inno Setup 7/6) nu a fost găsit. Specificați -IsccPath."
}

$testPython = Resolve-TestPython -ExplicitPath $TestPythonPath
$testPyVersion = ([string](& $testPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")).Trim()
if ($LASTEXITCODE -ne 0 -or -not $testPyVersion.StartsWith("3.14.")) {
    throw "Interpretorul de test necesită Python 3.14.x (detectat: '$testPyVersion' la $testPython). Activați venv-ul Python 3.14 sau specificați -TestPythonPath."
}
$appVersion = Get-CanonicalVersion -PythonExe $testPython
Assert-VersionConsistency -Version $appVersion
Write-Host "Versiune canonică detectată: $appVersion"

$pkgVenvAbs = Resolve-PathRelativeToRepo $PackagingVenvPath
$pkgPython = Join-Path $pkgVenvAbs "Scripts\python.exe"
Assert-PackagingEnvironment -PkgPythonExe $pkgPython

if (-not $SkipTests) {
    Invoke-TestSuite -TestPythonExe $testPython
}

$pyinstallerWorkDir = Join-Path $RepoRoot "build\pyinstaller"
$distDir = Join-Path $RepoRoot "dist"
$distBundleDir = Join-Path $distDir "OrtoDocs"

if (Test-Path $pyinstallerWorkDir) {
    Remove-Item -Path $pyinstallerWorkDir -Recurse -Force
}
if (Test-Path $distBundleDir) {
    Remove-Item -Path $distBundleDir -Recurse -Force
}

Write-Host "Construire dist\OrtoDocs cu PyInstaller..."
$previousBuildEap = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    & $pkgPython -m PyInstaller --noconfirm --clean --workpath $pyinstallerWorkDir --distpath $distDir $SpecPath
    $pyiExit = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousBuildEap
}
if ($pyiExit -ne 0) {
    throw "PyInstaller a eșuat cu exit code $pyiExit."
}

Assert-BuiltBundle -DistBundleDir $distBundleDir

if ($CompileInstaller) {
    $iscc = Find-Iscc -ExplicitPath $IsccPath
    Write-Host "Folosire ISCC: $iscc"
    Write-Host "Compilare installer Inno Setup ($appVersion)..."
    & $iscc "/DAppVersion=$appVersion" $IssPath
    if ($LASTEXITCODE -ne 0) {
        throw "ISCC.exe a eșuat cu exit code $LASTEXITCODE."
    }
    $installerOut = Join-Path $distDir "installer\OrtoDocs_Setup_$appVersion.exe"
    if (-not (Test-Path -LiteralPath $installerOut -PathType Leaf)) {
        throw "Compilarea installerului s-a terminat fără fișierul așteptat: $installerOut"
    }
    Write-Host "Installer generat cu succes: $installerOut"
}
else {
    Write-Host "Compilarea installerului a fost omisă (rulați cu -CompileInstaller pentru a genera .exe-ul de setup)."
}
