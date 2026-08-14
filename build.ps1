# Build a standalone dictation.exe on Windows.
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    Write-Host "Creating venv..."
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
& .\.venv\Scripts\python.exe -m pip install pyinstaller -q

Write-Host "Building dictation.exe..."
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean dictation.spec

Write-Host "Done. Binary: dist\dictation.exe"