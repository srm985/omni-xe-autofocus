# Build dist\omni-autofocus.exe (command line), dist\OmniAutofocus.exe (app) and, if Inno Setup 6 is
# installed, dist\OmniAutofocus-Setup.exe. Requires: pip install -e ".[build]"
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }
$version = & $py -c "import omni_autofocus; print(omni_autofocus.__version__)"
$icon = "$root\src\omni_autofocus\assets\icon.ico"
# --collect-submodules is needed because an editable install hides the modules from PyInstaller.
$common = @("--noconfirm", "--clean", "--onefile", "--paths", "$root\src", "--collect-submodules", "omni_autofocus",
    "--icon", $icon, "--distpath", "$root\dist", "--workpath", "$root\build", "--specpath", "$root\build")

& $py -m PyInstaller @common --console --name omni-autofocus "$root\tools\pyinstaller_entry.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (command line) failed" }
& $py -m PyInstaller @common --windowed --name OmniAutofocus `
    --add-data "$root\src\omni_autofocus\assets;omni_autofocus\assets" "$root\tools\pyinstaller_app_entry.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (app) failed" }

& "$root\dist\omni-autofocus.exe" --version
& "$root\dist\omni-autofocus.exe" --simulate focus --yes
if ($LASTEXITCODE -ne 0) { throw "smoke test failed" }

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc /Qp "/DAppVersion=$version" "$root\installer\omni-autofocus.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
} else {
    Write-Warning "Inno Setup 6 not found: skipped the installer (winget install JRSoftware.InnoSetup)"
}
