# RNSD.spec — PyInstaller spec for the RNSD Menu Bar app
#
# Build:
#   uv pip install pyinstaller
#   uv run pyinstaller RNSD.spec
#
# Result: dist/RNSD.app
# Drag it to /Applications and add to Login Items in
# System Settings > General > Login Items.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# Pull in EVERYTHING from RNS — submodules, data files, binaries.
# RNS dynamically imports interface classes which PyInstaller can't see.
rns_datas, rns_binaries, rns_hiddenimports = collect_all('RNS')

block_cipher = None

a = Analysis(
    ['rnsd_menubar.py'],
    pathex=[],
    binaries=rns_binaries,
    datas=[
        ('rns_icon.png', '.'),
        ('rns_menu_icon.png', '.'),
    ] + rns_datas,
    hiddenimports=[
        'rumps',
        'AppKit',
        'Foundation',
        'objc',
        'cryptography',
        'serial',
    ] + rns_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RNSD',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # GUI app, no terminal window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='RNSD',
)

app = BUNDLE(
    coll,
    name='RNSD.app',
    icon='rns_icon.icns',
    bundle_identifier='com.reticulum.rnsd-menubar',
    version='1.0.0',
    info_plist={
        'CFBundleName': 'RNSD',
        'CFBundleDisplayName': 'RNSD Menu Bar',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        # LSUIElement = True hides the Dock icon (menu bar app only)
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
    },
)
