; Установщик MiniDock (Inno Setup 6). Собирается из build\build.ps1, версия передаётся через /DMyAppVersion.
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef DistDir
  #define DistDir "dist\MiniDock"
#endif
#define MyAppName "MiniDock"
#define MyAppExe "MiniDock.exe"

[Setup]
AppId={{6F1C2A7E-3B9D-4E52-9A0C-5D7E8B1F4A21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=MiniDock
VersionInfoVersion={#MyAppVersion}
; ставится в папку пользователя — права администратора не нужны
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\release
OutputBaseFilename=MiniDock-Setup-{#MyAppVersion}
SetupIconFile=MiniDock.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no
; в папке программы — только MiniDock.exe, lib\ и uninstall\
UninstallFilesDir={app}\uninstall

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
russian.QuitShortcut=Выйти из MiniDock
english.QuitShortcut=Quit MiniDock
russian.AutostartTask=Запускать MiniDock вместе с Windows
english.AutostartTask=Start MiniDock with Windows
russian.LaunchNow=Запустить MiniDock
english.LaunchNow=Launch MiniDock
russian.Note=MiniDock заменит панель задач Windows своим доком и добавит верхнюю панель.%nВернуть всё как было: правый клик по доку → «Выход» или ярлык «Выйти из MiniDock».
english.Note=MiniDock replaces the Windows taskbar with its dock and adds a top menu bar.%nTo restore everything: right-click the dock → Quit, or use the "Quit MiniDock" shortcut.

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; остатки старой раскладки (до 1.3.0): служебная папка и деинсталлятор лежали прямо в папке программы
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\unins000.exe"
Type: files; Name: "{app}\unins000.dat"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autoprograms}\{cm:QuitShortcut}"; Filename: "{app}\{#MyAppExe}"; Parameters: "--quit"; IconFilename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
; то же значение, что ставит переключатель «Запускать вместе с Windows» в настройках MiniDock
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExe}"""; Tasks: autostart; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "{#MyAppName}"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent
; автообновление: установщик запущен тихо с /UPDATE — после установки сразу запускаем новую версию
Filename: "{app}\{#MyAppExe}"; Flags: nowait runasoriginaluser; Check: IsUpdate

[Code]
function IsUpdate: Boolean;
begin
  Result := Pos('/UPDATE', UpperCase(GetCmdTail)) > 0;
end;

procedure StopMiniDock(const Dir: String);
var
  ResultCode: Integer;
begin
  { мягкий выход: MiniDock сам возвращает панель задач и рабочую область }
  if FileExists(Dir + '\{#MyAppExe}') then
  begin
    Exec(Dir + '\{#MyAppExe}', '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(3000);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopMiniDock(ExpandConstant('{app}'));  { обновление поверх запущенного }
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopMiniDock(ExpandConstant('{app}'));
end;

procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption := WizardForm.WelcomeLabel2.Caption + #13#10#13#10 + ExpandConstant('{cm:Note}');
end;
