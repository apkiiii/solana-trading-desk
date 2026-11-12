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
CloseApplications=yes
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
