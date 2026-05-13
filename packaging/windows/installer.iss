; Inno Setup script for Pioneer DJ Link
; Build with: ISCC.exe packaging\windows\installer.iss
; Produces:   packaging\windows\Output\PioneerDJLink-Setup.exe

#define AppName       "Pioneer DJ Link"
#define AppExeName    "PioneerDJLink.exe"
#define AppVersion    "0.3.0"
#define AppPublisher  "Vito"
#define AppId         "{{B0F1B3F4-7C8C-4C3D-9C2A-DA1A7E1F0E11}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=PioneerDJLink-Setup
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}
; Uncomment if you supply app.ico next to this script:
;SetupIconFile=app.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
; Single-file PyInstaller build — just copy the one exe.
Source: "..\..\dist\PioneerDJLink.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent

; ── Open the three DJ Link UDP ports in the Windows firewall ──
; Rule WITHOUT program= filter so it works regardless of install path.
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""Pioneer DJ Link UDP"" > nul 2>&1"; \
    Flags: runhidden
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""Pioneer DJ Link UDP"" dir=in action=allow protocol=UDP localport=50000-50002"; \
    Flags: runhidden
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""Pioneer DJ Link UDP out"" dir=out action=allow protocol=UDP localport=50000-50002"; \
    Flags: runhidden
; Reserve ports 50000-50002 from the Windows dynamic (ephemeral) port range so
; Hyper-V / WireGuard / Tailscale cannot grab them before our sockets bind.
Filename: "netsh"; Parameters: "int ipv4 add excludedportrange udp 50000 3"; \
    Flags: runhidden

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""Pioneer DJ Link UDP"""; Flags: runhidden
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""Pioneer DJ Link UDP out"""; Flags: runhidden
Filename: "netsh"; Parameters: "int ipv4 delete excludedportrange udp 50000 3"; Flags: runhidden
