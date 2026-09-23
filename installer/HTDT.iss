// AppVersion is normally passed by scripts/build-installer.ps1 as the display
// version "<canonical>+g<sha8>[.dirty]"; this fallback tracks the canonical
// version in backend/src/htdt/__init__.py for direct ISCC invocations.
#ifndef AppVersion
  #define AppVersion "0.2.0.dev0"
#endif

#ifndef SourceDir
  #define SourceDir "..\\dist-native\\HTDT"
#endif

[Setup]
AppId={{8EA4B43A-7CD0-4F1F-83D8-38B37035E7D2}
AppName=Home Theater Digital Twin
AppVersion={#AppVersion}
AppPublisher=HTDT
DefaultDirName={localappdata}\\Programs\\Home Theater Digital Twin
DefaultGroupName=Home Theater Digital Twin
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputBaseFilename=HTDT-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#SourceDir}\\HTDT.ico
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=Home Theater Digital Twin
UninstallDisplayIcon={app}\\HTDT\\HTDT.exe

[Files]
Source: "{#SourceDir}\\*"; DestDir: "{app}\\HTDT"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\\Home Theater Digital Twin"; Filename: "{app}\\HTDT\\HTDT.exe"
Name: "{autodesktop}\\Home Theater Digital Twin"; Filename: "{app}\\HTDT\\HTDT.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

; #612: per-user file associations so Explorer launches route through the
; single-instance guard into the running app's one launch-intent authority.
; .htdt-backup opens as a preview only — restoring stays an explicit choice.
[Registry]
Root: HKCU; Subkey: "Software\Classes\.htdtproject"; ValueType: string; ValueData: "HTDT.Project"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Project"; ValueType: string; ValueData: "HTDT Project"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Project\DefaultIcon"; ValueType: string; ValueData: "{app}\HTDT\HTDT.exe,0"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Project\shell\open\command"; ValueType: string; ValueData: """{app}\HTDT\HTDT.exe"" ""%1"""; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\.htdtcapture"; ValueType: string; ValueData: "HTDT.Capture"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Capture"; ValueType: string; ValueData: "HTDT Capture Package"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Capture\DefaultIcon"; ValueType: string; ValueData: "{app}\HTDT\HTDT.exe,0"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Capture\shell\open\command"; ValueType: string; ValueData: """{app}\HTDT\HTDT.exe"" ""%1"""; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\.htdt-backup"; ValueType: string; ValueData: "HTDT.Backup"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Backup"; ValueType: string; ValueData: "HTDT Backup Archive"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Backup\DefaultIcon"; ValueType: string; ValueData: "{app}\HTDT\HTDT.exe,0"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\HTDT.Backup\shell\open\command"; ValueType: string; ValueData: """{app}\HTDT\HTDT.exe"" ""%1"""; Flags: uninsdeletekey

[Run]
Filename: "{app}\\HTDT\\HTDT.exe"; Description: "Launch Home Theater Digital Twin"; Flags: nowait postinstall skipifsilent
