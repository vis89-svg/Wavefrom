#!/usr/bin/env python
import sys
import os
import subprocess

# Use the hermes venv directly
venv_python = r"C:\Users\visha\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"

# Set PYTHONPATH to include hermes venv site-packages
os.environ['PYTHONPATH'] = r"C:\Users\visha\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages;" + os.environ.get('PYTHONPATH', '')

# Change to project directory
proj_dir = r"E:\XgenF1"
os.chdir(proj_dir)

# Step 1: Build PyInstaller executable
print("\n=== Building PyInstaller executable ===")
result = subprocess.run(
    [venv_python, "-m", "pyinstaller", "dictation.spec"],
    capture_output=True,
    text=True
)
print("STDOUT:", result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)
if result.returncode != 0:
    print(f"PyInstaller build failed with return code {result.returncode}")
    sys.exit(1)

print("\n=== PyInstaller build complete ===")

# Step 2: Build Inno Setup installer
print("\n=== Building Inno Setup installer ===")
# Check if ISCC.exe exists
iscc_paths = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]
iscc_found = False
for path in iscc_paths:
    if os.path.exists(path):
        print(f"Found ISCC.exe at: {path}")
        iscc_found = True
        # Run the installer build
        result = subprocess.run(
            [path, "/c", "voiceflow_install.iss"],
            capture_output=True,
            text=True,
            cwd=proj_dir
        )
        print("STDOUT:", result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        if result.returncode != 0:
            print(f"Inno Setup build returned {result.returncode}")
        break

if not iscc_found:
    print("Inno Setup (ISCC.exe) not found - skipping installer build")
    print("Installer source file: voiceflow_install.iss")

print("\n=== Build process complete ===")
print("\nOutput files:")
print("- PyInstaller: dist/dictation.exe (and dlls)")
print("- Inno Setup: VoiceFlow_Dictation_Installer.exe (if built)")