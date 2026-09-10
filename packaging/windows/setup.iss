; Inno Setup script for ride-the-api (Windows server + Windows service).
; Produces an installer .exe that packages dist\ride-the-api (PyInstaller onedir)
; and registers the server as a Windows service via NSSM. During installation
; the user chooses whether the service starts automatically with Windows.
; On upgrade the installer detects the previous installation and terminates
; the running service/processes before overwriting the files.
;
; Build with: iscc packaging\windows\setup.iss   (from the repo root)

[Setup]
AppId={{CE2157C0-550F-4318-AD20-7A494D6264B2}}
AppName=ride-the-api
; Keep in sync with `version` in pyproject.toml.
AppVersion=0.2.0
AppPublisher=ride-the-api
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\ride-the-api.exe
DefaultDirName={pf}\ride-the-api
DefaultGroupName=ride-the-api
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=ride-the-api-setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Let the user decide whether the service starts automatically with Windows.
[Tasks]
Name: "autostart"; Description: "Avvia ride-the-api automaticamente all'avvio di Windows"; GroupDescription: "Avvio automatico:"; Flags: checked

; Bundle the PowerShell script used to register the service.
[Files]
Source: "..\..\dist\ride-the-api\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
Source: "service\install_service.ps1"; DestDir: "{app}\packaging"; Flags: skipifsourcedoesntexist

; Place map of working dir for the service in ProgramData (config:ProgramData\ride-the-api).
[Registry]
Root: HKLM; Subkey: "SOFTWARE\ride-the-api"; ValueType: string; ValueName: "DataDir"; ValueData: "{commonappdata}\ride-the-api"; Flags: uninsdeletekey

[Run]
; Register + start the Windows service (Ida una tantam amministrativa).
; The {code:GetStartTypeParam} function passes -AutoStart / -ManualStart to
; install_service.ps1 based on the "autostart" task selected by the user.
Filename: "{cmd}"; Parameters: "/c powershell -NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\install_service.ps1"" {code:GetStartTypeParam}"; Flags: runhidden runascurrentuser; StatusMsg: "Registrazione servizio ride-the-api..."
; Offer to open the web UI after install. "http://localhost:8911" has no file
; extension, so Inno Setup treats it as an executable and tries CreateProcess
; ("CreateProcess failed; code 2") unless we force ShellExecute via shellexec.
Filename: "http://localhost:8911"; Description: "Apri dashboard ride-the-api"; Flags: postinstall nowait skipifsilent shellexec

[Icons]
Name: "{group}\ride-the-api"; Filename: "{app}\ride-the-api.exe" ; Comment: "Lancia la console di controllo ride-the-api"
Name: "{group}\ride-the-api Dashboard"; Filename: "http://localhost:8911"
Name: "{group}\Rimuovi ride-the-api"; Filename: "{uninstallexe}"

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c powershell -NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\install_service.ps1"" -Uninstall"; Flags: runhidden runascurrentuser

[Code]
const
  ServiceName = 'ride-the-api';

// Returns the start-type switch for install_service.ps1 based on the
// "autostart" task the user selected during installation.
function GetStartTypeParam(Param: String): String;
begin
  if WizardIsTaskSelected('autostart') then
    Result := '-AutoStart'
  else
    Result := '-ManualStart';
end;

// Stops the old service and terminates any running ride-the-api.exe process
// so the new version's files can be overwritten during an upgrade.
procedure StopOldInstallation();
var
  ResultCode: Integer;
begin
  // Stop the NSSM service if it is registered (ignore errors: it may not exist).
  Exec('sc.exe', 'stop ' + ServiceName, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Kill any leftover ride-the-api.exe processes (control panel, manual runs).
  Exec('taskkill.exe', '/F /IM ride-the-api.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Runs before files are installed: terminate the old installation so the
// upgrade can overwrite the files in {app}.
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopOldInstallation();
  Result := '';
end;

// Detects a previous installation and informs the user that it will be
// upgraded (processes are terminated in PrepareToInstall).
function InitializeSetup(): Boolean;
begin
  Result := True;
  if RegKeyExists(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{CE2157C0-550F-4318-AD20-7A494D6264B2}_is1') then
    MsgBox('Rilevata un''installazione precedente di ride-the-api.' + #13#10 +
           'Il servizio e i processi in esecuzione verranno terminati e la versione verrà aggiornata.',
           mbInformation, MB_OK);
end;
