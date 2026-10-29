; Inno Setup 6 script for the Debrief installer.
;
; Build (from the repository root, after PyInstaller has made dist\Debrief):
;     iscc /DAppVersion=2.0.0 packaging\debrief.iss
; Output: dist-installer\Debrief-Setup-2.0.0.exe
;
; Per-user install (no admin prompt) to %LOCALAPPDATA%\Programs\Debrief.
; The uninstaller removes only the program files. It never touches the user's data:
;   %LOCALAPPDATA%\Debrief            settings.toml (token), .cache, debrief.log, .edge-profile
;   Documents\Debrief Reviews         the review library
; Both are created by Debrief.exe at run time, outside {app}, so Inno Setup doesn't know about them.

#ifndef AppVersion
  #error Pass the version on the command line: iscc /DAppVersion=x.y.z packaging\debrief.iss
#endif

#define AppName "Debrief"
#define AppExe "Debrief.exe"

[Setup]
; Fixed AppId: every version installs over the previous one instead of side by side. Never change it.
AppId={{14DA940F-67E3-4A45-8F42-4FC631C59763}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Overdriver
AppComments=Reviews your iRacing races from Garage 61
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} installer
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
OutputDir=..\dist-installer
OutputBaseFilename=Debrief-Setup-{#AppVersion}
SetupIconFile=..\debrief\desktop\debrief.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Debrief.exe must not be running while its files are replaced or removed
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; checked by default (no "unchecked" flag)
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; an upgrade replaces the bundled Python files completely, so nothing stale from an older version stays behind
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Debrief\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\Debrief\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Review your iRacing races from Garage 61"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Review your iRacing races from Garage 61"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
; the in-app updater runs this installer silently with /RELAUNCH=1: reopen Debrief when it's done
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: RelaunchAfterUpdate

[Code]
function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

[UninstallDelete]
; only the program folder itself (if anything is left in it); user data lives elsewhere and is kept
Type: dirifempty; Name: "{app}"
