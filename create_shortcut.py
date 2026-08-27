#!/usr/bin/env python
import os
import sys

desktop = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
lnk_path = os.path.join(desktop, 'Waveform.lnk')
exe_path = r'E:\XgenF1\dist\dictation.exe'

try:
    import win32com.client
    wsh = win32com.client.Dispatch('WScript.Shell')
    shortcut = wsh.CreateShortcut(lnk_path)
    shortcut.TargetPath = exe_path
    shortcut.WorkingDirectory = os.path.dirname(exe_path)
    shortcut.Save()
    print(f'Shortcut created: {lnk_path}')
except ImportError:
    print('win32com not available')