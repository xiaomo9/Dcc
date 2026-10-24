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
UninstallDisplayIcon={app}\DCC-panel.exe
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
CloseApplications=yes
ChangesEnvironment=yes
UsePreviousTasks=no
[Tasks]
Name: "addtopath"; Description: "将 dcc 添加到当前用户 Path（在终端中使用 dcc 命令）"; Flags: unchecked
[Files]
Source: "..\dist\DCC\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\DCC"; Filename: "{app}\DCC-panel.exe"
Name: "{autodesktop}\DCC"; Filename: "{app}\DCC-panel.exe"
[Run]
Filename: "{app}\DCC-panel.exe"; Description: "Open DCC"; Flags: nowait postinstall skipifsilent

[InstallDelete]
Type: files; Name: "{app}\dcc-cli.exe"

[Code]
var
  PathConfigured: Boolean;

function SamePath(Left, Right: String): Boolean;
begin
  Result := CompareText(RemoveBackslashUnlessRoot(RemoveQuotes(Trim(Left))),
    RemoveBackslashUnlessRoot(RemoveQuotes(Trim(Right)))) = 0;
end;

function FindPathEntry(Value, Directory: String; var Offset, Count: Integer): Boolean;
var
  Tail, Entry: String;
  Separator: Integer;
begin
  Offset := 1;
  Result := False;
  while Offset <= Length(Value) do begin
    Tail := Copy(Value, Offset, Length(Value));
    Separator := Pos(';', Tail);
    if Separator = 0 then Count := Length(Tail) else Count := Separator - 1;
    Entry := Copy(Tail, 1, Count);
    if SamePath(Entry, Directory) then begin
      Result := True;
      Exit;
    end;
    Offset := Offset + Count + 1;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Value, Directory: String;
  Offset, Count: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  if not WizardIsTaskSelected('addtopath') then Exit;
  Directory := ExpandConstant('{app}');
  RegQueryStringValue(HKCU, 'Environment', 'Path', Value);
  if FindPathEntry(Value, Directory, Offset, Count) then begin
    PathConfigured := True;
    Exit;
  end;
  if (Value <> '') and (Copy(Value, Length(Value), 1) <> ';') then Value := Value + ';';
  if not RegWriteStringValue(HKCU, 'Environment', 'Path', Value + Directory) then begin
    Log('Unable to add DCC to the user Path. Configure it manually if needed.');
    Exit;
  end;
  PathConfigured := True;
  RegWriteStringValue(HKCU, 'Software\DCC', 'PathAdded', Directory);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> wpFinished then Exit;
  if PathConfigured then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      '终端命令已配置。请重新打开 PowerShell，再使用 dcc 2 等命令。'
  else
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      '未自动配置终端命令。如需使用 dcc，请手动设置：' + #13#10 +
      '打开“编辑账户的环境变量”，在用户 Path 中新建以下目录：' + #13#10 +
      ExpandConstant('{app}') + #13#10 +
      '保存后重新打开 PowerShell，即可运行 dcc 2。';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Value, Directory: String;
  Offset, Count: Integer;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  if not RegQueryStringValue(HKCU, 'Software\DCC', 'PathAdded', Directory) then Exit;
  if not SamePath(Directory, ExpandConstant('{app}')) then Exit;
  RegQueryStringValue(HKCU, 'Environment', 'Path', Value);
  if FindPathEntry(Value, Directory, Offset, Count) then begin
    if Offset + Count <= Length(Value) then Count := Count + 1
    else if Offset > 1 then begin
      Offset := Offset - 1;
      Count := Count + 1;
    end;
    Delete(Value, Offset, Count);
    if not RegWriteStringValue(HKCU, 'Environment', 'Path', Value) then Exit;
  end;
  RegDeleteValue(HKCU, 'Software\DCC', 'PathAdded');
  RegDeleteKeyIfEmpty(HKCU, 'Software\DCC');
end;
