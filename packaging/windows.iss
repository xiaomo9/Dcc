#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
[Setup]
AppId={{51C3D86C-1DCE-47B2-ADE0-349ED63DBA21}
AppName=DCC
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\DCC
PrivilegesRequired=lowest
DefaultGroupName=DCC
OutputDir=..\dist
OutputBaseFilename=DCC-Windows-x64-Setup
SetupIconFile=..\build\dcc.ico
UninstallDisplayIcon={app}\DCC.exe
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
CloseApplications=yes
[Files]
Source: "..\dist\DCC\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\DCC"; Filename: "{app}\DCC.exe"
Name: "{autodesktop}\DCC"; Filename: "{app}\DCC.exe"
[Run]
Filename: "{app}\DCC.exe"; Description: "Open DCC"; Flags: nowait postinstall skipifsilent
