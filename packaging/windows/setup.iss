; Inno Setup script for ride-the-api (Windows server + Windows service).
; Produces an installer .exe that packages dist\ride-the-api (PyInstaller onedir)
; and registers the server as an auto-start Windows service via NSSM.
;
; Build with: iscc packaging\windows\setup.iss   (from the repo root)

[Setup]
AppId={{CE2157C0-550F-4318-AD20-7A494D6264B2}}
AppName=ride-the-api
; Keep in sync with `version` in pyproject.toml.
AppVersion=0.2.0
AppPublisher=ride-the-api
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

; Bundle the PowerShell script used to register the service.
[Files]
Source: "..\..\dist\ride-the-api\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
Source: "service\install_service.ps1"; DestDir: "{app}\packaging"; Flags: skipifsourcedoesntexist

; Place map of working dir for the service in ProgramData (config:ProgramData\ride-the-api).
[Registry]
Root: HKLM; Subkey: "SOFTWARE\ride-the-api"; ValueType: string; ValueName: "DataDir"; ValueData: "{commonappdata}\ride-the-api"; Flags: uninsdeletekey

[Run]
; Register + start the Windows service (Ida una tantam amministrativa).
Filename: "{cmd}"; Parameters: "/c powershell -NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\install_service.ps1"""; Flags: runhidden runascurrentuser; StatusMsg: "Registrazione servizio ride-the-api..."
; Offer to open the web UI after install. "http://localhost:8911" has no file
; extension, so Inno Setup treats it as an executable and tries CreateProcess
; ("CreateProcess failed; code 2") unless we force ShellExecute via shellexec.
Filename: "http://localhost:8911"; Description: "Apri dashboard ride-the-api"; Flags: postinstall nowait skipifsilent shellexec

[Icons]
Name: "{group}\ride-the-api Dashboard"; Filename: "http://localhost:8911"
Name: "{group}\Rimuovi ride-the-api"; Filename: "{uninstallexe}"

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c powershell -NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\install_service.ps1"" -Uninstall"; Flags: runhidden runascurrentuser
