; Inno Setup Script for Waveform
; Build script compiled with Inno Setup 6.7.3

[Setup]
; Basic info
AppName=Waveform
AppVersion=1.0
AppPublisher=XgenF1
; No admin/UAC prompt; installs to the current user's Program Files equivalent
PrivilegesRequired=lowest
; Default output directory
DefaultDirName={userpf}\Waveform
; Default group name in Start Menu
DefaultGroupName=Waveform
; Output base filename
OutputBaseFilename=Waveform_Installer
; Installer wizard icon
SetupIconFile=assets\app.ico
; Default language
; Languages
; Installable languages
; [Languages]
; Icons
[Icons]
Name: "{userdesktop}\Waveform"; Filename: "{app}\dictation.exe"
Name: "{group}\Waveform"; Filename: "{app}\dictation.exe"
; Files
[Files]
Source: "dist\dictation.exe"; DestDir: "{app}"
; Ship the blank template, never a real key -- each user gets their own via
; the "Get a free API key" link in Settings.
Source: ".env.example"; DestDir: "{app}"; DestName: ".env"
; Include the app icon
Source: "assets\app.ico"; DestDir: "{app}"
; User strings
[Strings]
AppName = "Waveform"
Publisher = "XgenF1"
; Autostart: same HKCU Run key the app itself writes via autostart_set() in src/config.py
[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Waveform"; ValueData: """{app}\dictation.exe"" dictate"; Flags: uninsdeletevalue
; User strings
[Messages]
RunButton = '&Run'
SkipButton = 'Skip'
