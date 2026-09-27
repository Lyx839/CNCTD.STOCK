; Inno Setup 6 – Installer für CNCTD.STOCK
; Wird von build.bat automatisch aufgerufen (oder: ISCC.exe installer\cnctd_stock.iss)

#define AppName      "CNCTD.STOCK"
#define AppVersion   "1.3.0"
#define AppPublisher "CNCTD.VERSE"
#define AppExe       "CNCTD.STOCK.exe"

[Setup]
AppId={{7C1B4E2A-5D3F-4A8B-9E61-C7D2F0A4B913}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Installation ohne Adminrechte möglich (dann unter %LocalAppData%\Programs)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=CNCTD.STOCK_Setup
SetupIconFile=..\assets\cnctd_stock.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
