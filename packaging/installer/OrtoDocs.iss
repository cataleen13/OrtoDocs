; Script Inno Setup pentru OrtoDocs (instalare per-user, fără drepturi de administrator)
; Păstrează intact profilul de date din %LOCALAPPDATA%\OrtoDocs la upgrade și dezinstalare.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "OrtoDocs"
#define AppPublisher "OrtoDocs"
#define AppExeName "OrtoDocs.exe"
#define AppIdGuid "{{7D5F2A3E-9C4B-4E1A-8F6D-3B2E1A9C4D5E}"

[Setup]
AppId={#AppIdGuid}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
AllowNoIcons=yes
LicenseFile=..\THIRD_PARTY_LICENSES.txt
OutputDir=..\..\dist\installer
OutputBaseFilename=OrtoDocs_Setup_{#AppVersion}
SetupIconFile=..\ortodocs.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
CloseApplicationsFilter={#AppExeName}
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Copiază exclusiv aplicația din dist\OrtoDocs. Nu atinge {localappdata}\OrtoDocs.
Source: "..\..\dist\OrtoDocs\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent unchecked
