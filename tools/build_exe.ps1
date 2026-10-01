# Build a standalone omni-autofocus.exe into dist\ (requires: pip install -e ".[build]")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }
# --collect-submodules is needed because an editable install hides the modules from PyInstaller.
& $py -m PyInstaller --noconfirm --clean --onefile --console `
    --name omni-autofocus --paths "$root\src" --collect-submodules omni_autofocus `
    --distpath "$root\dist" --workpath "$root\build" --specpath "$root\build" `
    "$root\tools\pyinstaller_entry.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
& "$root\dist\omni-autofocus.exe" --version
& "$root\dist\omni-autofocus.exe" --simulate focus --yes
if ($LASTEXITCODE -ne 0) { throw "smoke test failed" }
