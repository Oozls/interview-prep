# Builds dist\생기부면접대비.exe (single file, no console). Run from the project folder.
$ErrorActionPreference = "Stop"
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name "interview-prep" `
  --add-data "templates;templates" --add-data "static;static" `
  --collect-all webview --hidden-import clr `
  main.py
Write-Host "built: dist\interview-prep.exe"
