# Builds release\interview-prep-v<version>.zip (folder app: exe + _internal). Run from the project folder.
# data.db / uploads are NOT bundled: the app creates them next to the exe on first run.
param([string]$Dist = "dist", [string]$Work = "build", [string]$Python = ".\.venv\Scripts\python.exe")
$ver = (Select-String -Path version.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
& $Python -m PyInstaller --noconfirm --onedir --windowed --name interview-prep `
  --distpath $Dist --workpath $Work `
  --add-data "templates;templates" --add-data "static;static" `
  --collect-all webview --hidden-import clr main.py 2>&1 | Out-Null
if (-not (Test-Path "$Dist\interview-prep\interview-prep.exe")) { throw "build failed" }
New-Item -ItemType Directory -Force release | Out-Null
$zip = "release\interview-prep-v$ver.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$Dist\interview-prep" -DestinationPath $zip
Write-Host "built: $zip"
