; Inno Setup script for PDF Studio.
; Build locally:  ISCC.exe installer.iss        (after running build.bat)
; CI passes the version in:  ISCC.exe /DAppVersion=1.2.3 installer.iss

#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif

[Setup]
; Generate your own GUID once (PowerShell: [guid]::NewGuid()) and keep it forever.
; It is how Windows recognizes upgrades of the same app. Keep the doubled {{ at the start.
AppId={{3f1c6eff-cbf9-485a-aba6-8c8f3a88bbb4}
AppName=PDF Studio
AppVersion={#AppVersion}
AppPublisher=Forge Crunch Studio
DefaultDirName={autopf}\PDF Studio
DefaultGroupName=PDF Studio
UninstallDisplayIcon={app}\PDFStudio.exe
; Installs just for the current user by default (no admin prompt); the wizard offers "all users".
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
OutputDir=installer_output
; A constant file name gives you a permanent "latest" download link on GitHub:
; https://github.com/OWNER/REPO/releases/latest/download/PDFStudio-Setup.exe
OutputBaseFilename=PDFStudio-Setup

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "pdfassoc"; Description: "Show PDF Studio in the ""Open with"" list for .pdf files"; Flags: unchecked

[Files]
Source: "dist\PDFStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PDF Studio"; Filename: "{app}\PDFStudio.exe"
Name: "{autodesktop}\PDF Studio"; Filename: "{app}\PDFStudio.exe"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "PDFStudio.pdf"; ValueData: ""; Flags: uninsdeletevalue; Tasks: pdfassoc
Root: HKA; Subkey: "Software\Classes\PDFStudio.pdf"; ValueType: string; ValueData: "PDF document"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKA; Subkey: "Software\Classes\PDFStudio.pdf\shell\open\command"; ValueType: string; ValueData: """{app}\PDFStudio.exe"" ""%1"""; Tasks: pdfassoc

[Run]
Filename: "{app}\PDFStudio.exe"; Description: "Launch PDF Studio"; Flags: nowait postinstall skipifsilent
