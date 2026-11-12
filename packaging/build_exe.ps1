# Fabrique SolanaTradingDesk.exe puis l'installateur SolanaTradingDesk-Setup-<version>.exe
# Utilisé automatiquement par GitHub Actions ; peut aussi être lancé à la main sous Windows :
#   powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$version = (python -c "from core.version import __version__; print(__version__)").Trim()
Write-Host "Version : $version"

python -m PyInstaller --noconfirm --clean --windowed `
    --name SolanaTradingDesk `
    --icon assets\icon.ico `
    --add-data "config\defaults.toml;config" `
    --add-data "assets;assets" `
    --add-data "web;web" `
    --collect-submodules keyring `
    --collect-data tzdata `
    --exclude-module tests `
    --exclude-module pytest `
    --exclude-module numpy `
    --exclude-module PIL `
    --exclude-module tkinter `
    --exclude-module PySide6 `
    SolanaTradingDesk.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller a échoué" }

# Vérification rapide : l'exécutable démarre et affiche la bonne version
$exe = "dist\SolanaTradingDesk\SolanaTradingDesk.exe"
$p = Start-Process -FilePath $exe -ArgumentList "--version" -Wait -PassThru -NoNewWindow
if ($p.ExitCode -ne 0) { throw "L'exécutable ne démarre pas" }

$iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    choco install innosetup -y --no-progress
}
& $iscc "/DMyAppVersion=$version" "packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup a échoué" }

$installer = "dist\installer\SolanaTradingDesk-Setup-$version.exe"
$hash = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()
"$hash  SolanaTradingDesk-Setup-$version.exe" | Out-File -Encoding ascii "dist\installer\SHA256SUMS.txt"
Write-Host "Installateur prêt : $installer"
Write-Host "SHA-256 : $hash"
