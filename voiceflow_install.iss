; Inno Setup Script for VoiceFlow Dictation
; Build script compiled with Inno Setup 6.7.3

[Setup]
; Basic info
AppName=VoiceFlow Dictation
AppVersion=1.0
AppPublisher=XgenF1
; No admin/UAC prompt; installs to the current user's Program Files equivalent
PrivilegesRequired=lowest
; Default output directory
DefaultDirName={userpf}\VoiceFlow Dictation
; Default group name in Start Menu
DefaultGroupName=VoiceFlow Dictation
; Output base filename
OutputBaseFilename=VoiceFlow_Dictation_Installer
; Default language
; Languages
; Installable languages
; [Languages]
; Icons
[Icons]
Name: "{userdesktop}\VoiceFlow Dictation"; Filename: "{app}\dictation.exe"
Name: "{group}\VoiceFlow Dictation"; Filename: "{app}\dictation.exe"
; Files
[Files]
Source: "dist\dictation.exe"; DestDir: "{app}"
Source: "dist\.env"; DestDir: "{app}"
; Include the app icon
Source: "assets\app.ico"; DestDir: "{app}"
; User strings
[Strings]
AppName = "VoiceFlow Dictation"
Publisher = "XgenF1"
; Autostart: same HKCU Run key the app itself writes via autostart_set() in src/config.py
[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "VoiceFlowDictation"; ValueData: """{app}\dictation.exe"" dictate"; Flags: uninsdeletevalue
; User strings
[Messages]
RunButton = '&Run'
SkipButton = 'Skip'
