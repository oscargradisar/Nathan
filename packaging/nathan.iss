; Windows installer for Nathan. Build: ISCC /DArch=arm64 packaging\nathan.iss  (or x64)
; Installs per-user (no admin prompt) and adds Start-menu and Desktop shortcuts.

#ifndef Arch
  #define Arch "x64"
#endif
; Inno names: "arm64" = ARM64 Windows only, "x64compatible" = x64 Windows (and ARM64 via emulation)
#if Arch == "arm64"
  #define ArchId "arm64"
#else
  #define ArchId "x64compatible"
#endif
#ifndef Version
  #define Version "0.1.0"
#endif

[Setup]
AppId={{6F3C2A51-9B7E-4D0A-8C1F-2D4E6A8B0C13}
AppName=Nathan
AppVersion={#Version}
AppPublisher=oscargradisar
DefaultDirName={autopf}\Nathan
DefaultGroupName=Nathan
PrivilegesRequired=lowest
ArchitecturesAllowed={#ArchId}
ArchitecturesInstallIn64BitMode={#ArchId}
OutputDir=..\dist
OutputBaseFilename=Nathan-{#Version}-windows-{#Arch}-setup
SetupIconFile=nathan.ico
UninstallDisplayIcon={app}\Nathan.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Put a Nathan icon on the Desktop"

[Files]
Source: "..\dist\Nathan\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Nathan"; Filename: "{app}\Nathan.exe"
Name: "{autodesktop}\Nathan"; Filename: "{app}\Nathan.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Nathan.exe"; Description: "Open Nathan now"; Flags: nowait postinstall skipifsilent
