#ifndef AppVersion
  #define AppVersion "0.1.0.dev0"
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

[Run]
Filename: "{app}\\HTDT\\HTDT.exe"; Description: "Launch Home Theater Digital Twin"; Flags: nowait postinstall skipifsilent
