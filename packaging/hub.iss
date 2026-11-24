; Inno Setup 6 script for the hub, compiled by packaging/build.py (which passes /DAppVersion=x.y.z /DApiVersion=n).
; Installs the PyInstaller folder to Program Files, makes %ProgramData%\AlpacaHub\drivers (where driver installers
; put their drivers; writable for users so the hub can delete a driver's old versions), records the hub and its API
; version in the registry for driver installers, adds Start-menu entries, an optional start at logon and an optional
; Windows Firewall rule, and stops a running copy cleanly before upgrading or uninstalling.
; Uninstalling the hub leaves the drivers installed: each has its own uninstaller.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef ApiVersion
  #define ApiVersion "1"
#endif
#define AppName "Alpaca Hub"
#define AppExe "AlpacaHub.exe"
#define CliExe "alpaca-hub.exe"
#define FwRule "Alpaca Hub"

[Setup]
AppId={{6C328AAE-2213-494C-9AAB-836BCBAA9BC2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Open source
AppComments=One ASCOM Alpaca service for every astronomy device on this PC; drivers are installed separately
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=AlpacaHub-{#AppVersion}-Setup
SetupIconFile=..\build\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} setup

[Tasks]
Name: "desktop"; Description: "Desktop shortcuts to start and stop it"; Flags: unchecked
Name: "autostart"; Description: "Start automatically when Windows starts (when someone logs on)"; Flags: unchecked
Name: "firewall"; Description: "Let phones and other computers use the devices (only on networks set to Private)"

[Messages]
; the text above the task list; Inno Setup moves the list down to fit it. Plain words: who can reach the hub when.
SelectTasksLabel2=Select the additional tasks you would like Setup to perform while installing [name], then click Next.%n%nAbout the firewall option: Windows calls each network either Private (your home network, one you trust) or Public (a cafe, a hotel, anywhere else). With this option, phones and other computers on a Private network can use the devices through the hub. On a Public network nobody else can, only programs on this PC. To share the devices, set this PC's network to Private in Windows Settings (Network, then your connection). If Windows Firewall asks about the hub, leave Public networks unticked.

[Dirs]
Name: "{commonappdata}\AlpacaHub"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\AlpacaHub\drivers"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
Source: "..\build\pyi\dist\AlpacaHub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\USER-GUIDE.html"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; what driver installers check: the hub is installed, and which hub API it has
Root: HKLM; Subkey: "Software\AlpacaHub"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\AlpacaHub"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"
Root: HKLM; Subkey: "Software\AlpacaHub"; ValueType: string; ValueName: "Api"; ValueData: "{#ApiVersion}"

[INI]
Filename: "{group}\Status page.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://localhost:11111/"

[InstallDelete]
; Inno Setup doesn't remove a shortcut whose task is unticked on a reinstall: do it, so "autostart off" really is off
Type: files; Name: "{autostartup}\{#AppName}.lnk"; Tasks: not autostart
Type: files; Name: "{autodesktop}\{#AppName}.lnk"; Tasks: not desktop
Type: files; Name: "{autodesktop}\Stop {#AppName}.lnk"; Tasks: not desktop

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--open"; Comment: "Starts the hub and opens its status page (or just opens the page if it already runs)"
Name: "{group}\Stop {#AppName}"; Filename: "{app}\{#CliExe}"; Parameters: "--stop --wait"; Comment: "Stops the hub cleanly"
Name: "{group}\Allow network access (firewall)"; Filename: "{app}\{#CliExe}"; Parameters: "--firewall"; Comment: "Lets phones and other computers on private networks reach the hub again, e.g. after Cancel at Windows Firewall's question (asks for administrator rights)"
Name: "{group}\Settings and log folder"; Filename: "{app}\{#AppExe}"; Parameters: "--open-folder"
Name: "{group}\User guide"; Filename: "{app}\USER-GUIDE.html"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autostartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: autostart
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--open"; Comment: "Starts the hub and opens its status page (or just opens the page if it already runs)"; Tasks: desktop
Name: "{autodesktop}\Stop {#AppName}"; Filename: "{app}\{#CliExe}"; Parameters: "--stop --wait"; Comment: "Stops the hub cleanly"; Tasks: desktop

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FwRule}"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=all dir=in program=""{app}\{#AppExe}"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=all dir=in program=""{app}\{#CliExe}"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FwRule}"" dir=in action=allow program=""{app}\{#AppExe}"" profile=private,domain enable=yes"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FwRule}"" dir=in action=allow program=""{app}\{#CliExe}"" profile=private,domain enable=yes"; Flags: runhidden; Tasks: firewall
; Not ticked by default: the first start on a PC that ran the older services takes over their settings once, and a
; WandererBox may then get a dew command (USER-GUIDE.html, "Before the first start").
Filename: "{app}\{#AppExe}"; Parameters: "--open"; Description: "Start {#AppName} now"; Flags: postinstall nowait skipifsilent runasoriginaluser unchecked
Filename: "{app}\USER-GUIDE.html"; Description: "Open the user guide"; Flags: postinstall shellexec skipifsilent runasoriginaluser
; An update started from the hub's Overview (hub/updates.py) passes /RESTARTHUB: the hub this installer stopped starts
; again, and the Overview reloads when it answers.
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: RestartHub

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FwRule}"""; Flags: runhidden; RunOnceId: "DelFirewall"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=all dir=in program=""{app}\{#AppExe}"""; Flags: runhidden; RunOnceId: "DelFirewallApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=all dir=in program=""{app}\{#CliExe}"""; Flags: runhidden; RunOnceId: "DelFirewallCli"

[UninstallDelete]
Type: files; Name: "{group}\Status page.url"
Type: files; Name: "{commonappdata}\AlpacaHub\running.json"

[Code]
{ Stop a running copy before files are replaced or removed. --stop asks it to shut down cleanly, so each driver stops
  its device (a box set to switch its dew heater off on exit does so); taskkill is only the fallback, and also ends
  driver processes, which run on the same exe. }
procedure StopHub(const Dir: String);
var
  Rc: Integer;
begin
  if FileExists(Dir + '\{#CliExe}') then
    Exec(Dir + '\{#CliExe}', '--stop --wait', '', SW_HIDE, ewWaitUntilTerminated, Rc);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Rc);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#CliExe}', '', SW_HIDE, ewWaitUntilTerminated, Rc);
end;

{ /RESTARTHUB on the command line: an update the running hub started (see [Run]). }
function RestartHub(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/RESTARTHUB') = 0 then
      Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopHub(WizardDirValue);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopHub(ExpandConstant('{app}'));
end;
