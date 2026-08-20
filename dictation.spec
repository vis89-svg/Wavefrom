# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ["src\\main.py"],
    pathex=["."],
    binaries=[],
    datas=[(".env.example", ".")],
    hiddenimports=["keyboard", "pystray", "PIL._tkinter_finder", "sounddevice"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter.test", "numpy.test", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="dictation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Run elevated so the global keyboard hook + SendInput can also reach
    # admin/elevated windows (else dictation silently fails there).
    uac_admin=True,
)