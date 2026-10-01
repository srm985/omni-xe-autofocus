; Per-user installer for Omni Autofocus (no administrator rights needed).
; Built by tools\build_exe.ps1 after PyInstaller has produced the two .exe files in dist\.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7C1F5E0A-3B9D-4C55-9E61-0D2A8B7F4E13}
AppName=Omni Autofocus
AppVersion={#AppVersion}
AppVerName=Omni Autofocus {#AppVersion}
AppPublisher=Sean (github.com/srm985)
DefaultDirName={autopf}\Omni Autofocus
DefaultGroupName=Omni Autofocus
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableWelcomePage=yes
OutputDir=..\dist
OutputBaseFilename=OmniAutofocus-Setup-{#AppVersion}
SetupIconFile=..\src\omni_autofocus\assets\icon.ico
UninstallDisplayIcon={app}\OmniAutofocus.exe
UninstallDisplayName=Omni Autofocus
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; A running app is asked to close first (see the Code section); this is the fallback for anything else.
CloseApplications=yes

[Tasks]
; Offered on a fresh install only: an upgrade keeps whatever the user chose in the app's menu.
Name: "startup"; Description: "Start Omni Autofocus when Windows starts (recommended)"; Check: not IsUpgrade
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\OmniAutofocus.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\omni-autofocus.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"

[Icons]
Name: "{group}\Omni Autofocus"; Filename: "{app}\OmniAutofocus.exe"
Name: "{group}\Fine-tune focus (test burns)"; Filename: "{app}\omni-autofocus.exe"; Parameters: "focus-ladder"
Name: "{group}\Check the laser's USB driver"; Filename: "{app}\omni-autofocus.exe"; Parameters: "driver"
Name: "{autodesktop}\Omni Autofocus"; Filename: "{app}\OmniAutofocus.exe"; Tasks: desktopicon

[Registry]
; Remove the start-with-Windows entry on uninstall, also when it was switched on from the app's menu.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "OmniAutofocus"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OmniAutofocus"; ValueData: """{app}\OmniAutofocus.exe"""; Tasks: startup

[Run]
Filename: "{app}\OmniAutofocus.exe"; Description: "Open Omni Autofocus now"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: files; Name: "{localappdata}\omni-autofocus\app-state.json"
Type: dirifempty; Name: "{localappdata}\omni-autofocus"

[Code]
const
  AppMutexName = 'OmniAutofocusApp';  { held by the running app, see app.py MUTEX_NAME }
  LaserMutexName = 'OmniAutofocusLaser';  { held while any Omni Autofocus program uses the laser }

function IsUpgrade(): Boolean;
begin
  Result := RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7C1F5E0A-3B9D-4C55-9E61-0D2A8B7F4E13}_is1');
end;

{ True while any OmniAutofocus.exe runs (a one-file .exe is two processes: launcher and app). }
function AppProcessRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'), '/C tasklist /NH /FI "IMAGENAME eq OmniAutofocus.exe" | find /I "OmniAutofocus.exe"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := ResultCode = 0;
end;

{ Ask a running app to close (like clicking its X: it refuses while Z is moving) and wait for it. }
function CloseRunningApp(): Boolean;
var
  ResultCode, I: Integer;
begin
  Result := True;
  if CheckForMutexes(LaserMutexName) and not CheckForMutexes(AppMutexName) then
  begin
    { the command-line tool is using the laser, e.g. fine-tuning: closing it would leave Z offset }
    Result := False;
    SuppressibleMsgBox('Omni Autofocus is using the laser (fine-tuning?). Finish there, then try again.',
      mbError, MB_OK, IDOK);
    Exit;
  end;
  if not CheckForMutexes(AppMutexName) and not AppProcessRunning() then
    Exit;
  Log('Omni Autofocus is running: asking it to close');
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM OmniAutofocus.exe', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
  for I := 1 to 50 do
  begin
    if not CheckForMutexes(AppMutexName) and not AppProcessRunning() then
    begin
      Log('Omni Autofocus has closed');
      Exit;
    end;
    Sleep(200);
  end;
  Result := False;
  Log('Omni Autofocus did not close');
  SuppressibleMsgBox('Omni Autofocus is still running. Close it (wait for Z to stop moving), then try again.',
    mbError, MB_OK, IDOK);
end;

function InitializeSetup(): Boolean;
begin
  Result := CloseRunningApp();
end;

function InitializeUninstall(): Boolean;
begin
  Result := CloseRunningApp();
end;
