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
; The app holds this mutex while running; Setup asks to close it before replacing files.
AppMutex=OmniAutofocusApp
CloseApplications=yes

[Tasks]
Name: "startup"; Description: "Start Omni Autofocus when Windows starts (recommended)"
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
