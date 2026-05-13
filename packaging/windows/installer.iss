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
; ── Configure the DJ Link Ethernet NIC for reliable link-local connectivity ──
; Find the first physical Ethernet adapter that has an APIPA (169.254.x.x) or
; no IP at all, and assign a static link-local IP 169.254.200.1/16 with DAD
; disabled.  This avoids the Windows DAD Tentative delay (up to 60 s) that
; prevents our VirtualCDJ socket from binding on first launch.
; If the adapter already has a non-APIPA IP (DHCP or static), leave it alone.
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ""$eth = Get-NetAdapter | Where-Object {{ $_.PhysicalMediaType -eq '802.3' -and $_.Status -eq 'Up' }} | Sort-Object InterfaceIndex | Select-Object -First 1; if ($eth) {{ $existing = Get-NetIPAddress -InterfaceIndex $eth.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {{ $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' }}; if (-not $existing) {{ Remove-NetIPAddress -InterfaceIndex $eth.InterfaceIndex -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue; New-NetIPAddress -InterfaceIndex $eth.InterfaceIndex -IPAddress '169.254.200.1' -PrefixLength 16 -ErrorAction SilentlyContinue; Set-NetIPInterface -InterfaceIndex $eth.InterfaceIndex -AddressFamily IPv4 -DadTransmits 0 -ErrorAction SilentlyContinue; Write-Host 'DJ Link NIC configured: 169.254.200.1/16 DAD=0' }} else {{ Write-Host ('DJ Link NIC already has IP: ' + ($existing | Select-Object -First 1 -ExpandProperty IPAddress)) }} }} else {{ Write-Host 'No Ethernet adapter found' }}"""; \
    Flags: runhidden

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""Pioneer DJ Link UDP"""; Flags: runhidden
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""Pioneer DJ Link UDP out"""; Flags: runhidden
Filename: "netsh"; Parameters: "int ipv4 delete excludedportrange udp 50000 3"; Flags: runhidden
