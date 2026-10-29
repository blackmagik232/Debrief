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
; Debrief.exe must not be running while its files are replaced or removed. A normal (interactive)
; install offers to close it. The in-app updater passes /NOCLOSEAPPLICATIONS instead: Setup then waits
; for Debrief's own "DebriefRunning" mutex to go (InitializeSetup below), so Debrief is never killed
; in the middle of writing a review.
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; checked by default (no "unchecked" flag)
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; leftovers of an earlier update that didn't finish
Type: filesandordirs; Name: "{app}\_internal.new"
Type: files; Name: "{app}\{#AppExe}.new"

[Files]
; The new version is copied next to the old one first and swapped in at the very end (SwapInNewVersion),
; so an install that fails part-way (antivirus, full disk, cancelled) leaves the old Debrief working.
; The swap also replaces the bundled Python files completely: nothing stale from an older version stays.
Source: "..\dist\Debrief\{#AppExe}"; DestDir: "{app}"; DestName: "{#AppExe}.new"; Flags: ignoreversion
Source: "..\dist\Debrief\_internal\*"; DestDir: "{app}\_internal.new"; Flags: ignoreversion recursesubdirs createallsubdirs

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

function InitializeSetup: Boolean;
var
  i: Integer;
begin
  // The in-app updater starts Setup, then Debrief closes on its own a few seconds later (after any
  // review it is writing is saved). Wait up to 90 s for that instead of force-closing it.
  Result := True;
  i := 0;
  while CheckForMutexes('DebriefRunning') and (i < 180) do
  begin
    Sleep(500);
    i := i + 1;
  end;
  if CheckForMutexes('DebriefRunning') then
  begin
    Log('Debrief is still running after 90 s.');
    if WizardSilent then
      Result := False   // nothing has been changed; Debrief reports the update didn't finish
    else
      Result := MsgBox('Debrief is still open. Close it, then click OK to carry on.', mbConfirmation, MB_OKCANCEL) = IDOK;
  end;
end;

// Windows won't rename a folder while a closing Debrief still has files open in it, so keep trying for up
// to a minute (an older Debrief that started this update closes itself a few seconds after starting it).
function RenameRetry(const Src, Dst: String): Boolean;
var
  i: Integer;
begin
  Result := RenameFile(Src, Dst);
  i := 0;
  while (not Result) and (i < 120) do
  begin
    Sleep(500);
    Result := RenameFile(Src, Dst);
    i := i + 1;
  end;
end;

function SwapInNewVersion(const AppDir: String): Boolean;
var
  Exe: String;
begin
  Result := False;
  Exe := AppDir + '\{#AppExe}';
  if not DirExists(AppDir + '\_internal.new') or not FileExists(Exe + '.new') then
  begin
    Log('The new files are missing; the old version was kept.');
    Exit;
  end;
  if DirExists(AppDir + '\_internal.old') then
    DelTree(AppDir + '\_internal.old', True, True, True);
  DeleteFile(Exe + '.old');
  if DirExists(AppDir + '\_internal') and not RenameRetry(AppDir + '\_internal', AppDir + '\_internal.old') then
  begin
    Log('Could not move the old _internal folder aside; the old version was kept.');
    Exit;
  end;
  if not RenameFile(AppDir + '\_internal.new', AppDir + '\_internal') then
  begin
    Log('Could not move the new _internal folder in; putting the old one back.');
    RenameFile(AppDir + '\_internal.old', AppDir + '\_internal');
    Exit;
  end;
  if FileExists(Exe) and not RenameRetry(Exe, Exe + '.old') then
  begin
    Log('Could not move the old Debrief.exe aside; putting the old version back.');
    RenameFile(AppDir + '\_internal', AppDir + '\_internal.new');
    RenameFile(AppDir + '\_internal.old', AppDir + '\_internal');
    Exit;
  end;
  if not RenameFile(Exe + '.new', Exe) then
  begin
    Log('Could not move the new Debrief.exe in; putting the old version back.');
    RenameFile(Exe + '.old', Exe);
    RenameFile(AppDir + '\_internal', AppDir + '\_internal.new');
    RenameFile(AppDir + '\_internal.old', AppDir + '\_internal');
    Exit;
  end;
  DelTree(AppDir + '\_internal.old', True, True, True);
  DeleteFile(Exe + '.old');
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if SwapInNewVersion(ExpandConstant('{app}')) then
      Log('New version swapped in.')
    else
      Log('UPDATE NOT APPLIED: the previous version is still installed.');
  end;
end;

[UninstallDelete]
; the program files (installed under staging names, then swapped in) and the program folder itself.
; User data lives elsewhere (%LOCALAPPDATA%\Debrief, Documents\Debrief Reviews) and is kept.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\_internal.old"
Type: filesandordirs; Name: "{app}\_internal.new"
Type: files; Name: "{app}\{#AppExe}"
Type: files; Name: "{app}\{#AppExe}.old"
Type: dirifempty; Name: "{app}"
