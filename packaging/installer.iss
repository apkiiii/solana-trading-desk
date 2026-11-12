; Installateur Windows de MY SOLANA TRADING DESK (Inno Setup 6)
; Installation pour l'utilisateur courant, sans droits administrateur.
; Les données (base, logs, clés API) restent dans %LOCALAPPDATA%\SolanaTradingDesk
; et sont conservées lors des mises à jour et de la désinstallation.

#define MyAppName "MY SOLANA TRADING DESK"
#define MyAppExe "SolanaTradingDesk.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{8F3C2A71-5D4B-4E8A-9C1F-6A2B7D9E4F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=apkiiii
AppPublisherURL=https://github.com/apkiiii/solana-trading-desk
DefaultDirName={localappdata}\Programs\SolanaTradingDesk
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=SolanaTradingDesk-Setup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Supprime les fichiers de l'ancienne version avant de copier la nouvelle
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\SolanaTradingDesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
; Relance l'application après l'installation (y compris après une mise à jour silencieuse)
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall

[Code]
{ Avant de copier les fichiers : ferme proprement l'ancienne version (elle peut tourner en arrière-plan,
  fenêtre fermée, quand le bot est actif), puis la termine si elle ne répond pas. Sinon Windows refuse
  de remplacer SolanaTradingDesk.exe (« DeleteFile a échoué ; code 5. Accès refusé »). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8766/api/quit -TimeoutSec 3 | Out-Null } catch {}"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(3000);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
  Result := '';
end;
