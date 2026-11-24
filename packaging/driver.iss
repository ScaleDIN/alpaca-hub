; Inno Setup 6 script for one driver, compiled by packaging/build.py driver <key>, which stages the driver in
; build\driver-<key>\ (driver.json and its package, without tests) and passes:
;   /DKey=wcover /DTitle="WandererCover V4-EC" /DAppVersion=1.0.0 /DApi=1 /DMinHub=0.3.0 /DPkg=wcover /DAppGuid=...
;   /DSrc=...  (MinHub: driver.json's min_hub, or 0 when it has none)
;
; How a driver installs (REFACTOR.md, "Where drivers live"):
; - into %ProgramData%\AlpacaHub\drivers\<key>\<version>\, a new folder for each version, so an update never touches
;   files a running driver process has open. The running hub notices the new version within a few seconds and
;   switches to it (at once if no program is connected to its devices, else when they disconnect or on Overview's
;   "Switch now"); it deletes the old version's folder once nothing uses it.
; - driver.json is written last, so the hub never starts half a driver.
; - uninstalling deletes driver.json first, waits up to 10 s for the hub to show the driver stopped, then deletes the
;   rest. The hub releases its ports and looks at them again.
; - the hub must be installed, with a hub API at least the driver's and a version at least its min_hub.

#ifndef Key
  #error Key is required (build.py passes it)
#endif
#ifndef MinHub
  #define MinHub "0"
#endif

[Setup]
AppId={{{#AppGuid}}
AppName=Alpaca Hub driver: {#Title}
AppVersion={#AppVersion}
AppVerName=Alpaca Hub driver: {#Title} {#AppVersion}
AppPublisher=Open source
AppComments=A driver for the Alpaca Hub (installed separately from the hub)
DefaultDirName={commonappdata}\AlpacaHub\drivers\{#Key}\{#AppVersion}
DisableDirPage=yes
UsePreviousAppDir=no
DisableProgramGroupPage=yes
CreateAppDir=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=AlpacaHub-{#Key}-{#AppVersion}-Setup
SetupIconFile=..\build\app.ico
UninstallDisplayName=Alpaca Hub driver: {#Title}
UninstallDisplayIcon={sys}\shell32.dll,165
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Alpaca Hub driver: {#Title}
VersionInfoDescription=Alpaca Hub driver {#Key} setup

[Files]
Source: "{#Src}\{#Pkg}\*"; DestDir: "{app}\{#Pkg}"; Flags: ignoreversion recursesubdirs createallsubdirs
; last: the hub only looks at a version folder once its driver.json is there
Source: "{#Src}\driver.json"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: dirifempty; Name: "{commonappdata}\AlpacaHub\drivers\{#Key}"

[Code]
{ -1, 0 or 1 as version A ("0.3.0") is older than, the same as or newer than B; missing parts count as 0. }
function CompareVersions(A, B: String): Integer;
var
  NA, NB, P: Integer;
begin
  Result := 0;
  while (Result = 0) and ((A <> '') or (B <> '')) do
  begin
    P := Pos('.', A);
    if P = 0 then begin NA := StrToIntDef(A, 0); A := ''; end
    else begin NA := StrToIntDef(Copy(A, 1, P - 1), 0); Delete(A, 1, P); end;
    P := Pos('.', B);
    if P = 0 then begin NB := StrToIntDef(B, 0); B := ''; end
    else begin NB := StrToIntDef(Copy(B, 1, P - 1), 0); Delete(B, 1, P); end;
    if NA < NB then Result := -1
    else if NA > NB then Result := 1;
  end;
end;

function InitializeSetup(): Boolean;
var
  Api, HubVersion: String;
begin
  Result := True;
  if not RegQueryStringValue(HKLM, 'Software\AlpacaHub', 'Api', Api) then
  begin
    MsgBox('The Alpaca Hub is not installed on this PC.' #13#10#13#10 +
           'Install the Alpaca Hub first, then this driver.', mbError, MB_OK);
    Result := False;
  end
  else if StrToIntDef(Api, 0) < {#Api} then
  begin
    MsgBox('This driver needs a newer Alpaca Hub (hub API ' + IntToStr({#Api}) + '; the hub installed here has API ' +
           Api + ').' #13#10#13#10 + 'Update the Alpaca Hub first, then install this driver.', mbError, MB_OK);
    Result := False;
  end
  else if RegQueryStringValue(HKLM, 'Software\AlpacaHub', 'Version', HubVersion) and
          (CompareVersions(HubVersion, '{#MinHub}') < 0) then
  begin
    MsgBox('This driver needs Alpaca Hub {#MinHub} or newer; the hub installed here is ' + HubVersion + '.' #13#10#13#10 +
           'Update the Alpaca Hub first, then install this driver.', mbError, MB_OK);
    Result := False;
  end;
end;

// The running hub's port, from %ProgramData%\AlpacaHub\running.json (its "port" field); 0 if it isn't running.
function HubPort(): Integer;
var
  S: AnsiString;
  Txt: String;
  P, I: Integer;
  Num: String;
begin
  Result := 0;
  if not LoadStringFromFile(ExpandConstant('{commonappdata}\AlpacaHub\running.json'), S) then
    exit;
  Txt := String(S);
  P := Pos('"port":', Txt);
  if P = 0 then
    exit;
  I := P + 7;
  Num := '';
  while (I <= Length(Txt)) and ((Txt[I] = ' ') or ((Txt[I] >= '0') and (Txt[I] <= '9'))) do
  begin
    if Txt[I] <> ' ' then
      Num := Num + Txt[I];
    I := I + 1;
  end;
  Result := StrToIntDef(Num, 0);
end;

{ True while the hub at Port still lists this driver (GET /hub/drivers): without its driver.json it drops it. }
function HubStillRuns(Port: Integer): Boolean;
var
  Http: Variant;
  Body: String;
begin
  Result := False;
  try
    Http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Http.Open('GET', 'http://127.0.0.1:' + IntToStr(Port) + '/hub/drivers', False);
    Http.SetTimeouts(2000, 2000, 2000, 2000);
    Http.Send('');
    Body := Http.ResponseText;
    Result := Pos('"name": "{#Key}", ', Body) > 0;
  except
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Port, I: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    { driver.json first: the hub stops the driver and releases its ports; then the rest can go }
    DeleteFile(ExpandConstant('{app}\driver.json'));
    Port := HubPort();
    if Port > 0 then
      for I := 1 to 40 do
      begin
        if not HubStillRuns(Port) then
          break;
        Sleep(250);
      end;
  end;
end;
