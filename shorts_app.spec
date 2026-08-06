# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置（跨平台）。

按运行平台选择图标与平台专属依赖：
- Windows：app.ico；无额外原生依赖。
- macOS  ：app.png（若无 .icns）；打包 pynput、pyobjc 的 Quartz 等。
"""
import sys

block_cipher = None

# 图标：Windows 用 .ico，macOS 用 .png（或 .icns 若存在）
_icon_win = 'resources/icons/app.ico'
_icon_mac = 'resources/icons/app.png'  # 有 .icns 时可改为 app.icns
if sys.platform == 'darwin':
    import os
    if os.path.exists('resources/icons/app.icns'):
        _icon_mac = 'resources/icons/app.icns'
    icon = _icon_mac
else:
    icon = _icon_win

hiddenimports = ['PyQt6', 'mss', 'PIL', 'pynput', 'numpy']
if sys.platform == 'darwin':
    # macOS：窗口枚举用到 Quartz；pynput 的 darwin 后端；ApplicationServices 辅助功能检测
    hiddenimports += [
        'pynput.keyboard._darwin',
        'pynput.mouse._darwin',
        'pynput._util.darwin',
        'Quartz',
        'AppKit',
        'ApplicationServices',
        'CoreFoundation',
        'objc',
    ]

# macOS 打包 Swift 编译的 ocr_helper 二进制到 core/ 下（OCR 用）
_extra_bins = [('core/ocr_helper', 'core')] if sys.platform == 'darwin' else []

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_extra_bins,
    datas=[('resources/icons', 'resources/icons')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure)

# macOS 生成 .app bundle；Windows 生成单 exe。
if sys.platform == 'darwin':
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='Shorts',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,  # macOS 不建议 upx，会触发签名/公证问题
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon,
    )
    app = BUNDLE(
        exe,
        name='Shorts.app',
        icon=icon,
        bundle_identifier='com.shorts.app',
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='Shorts',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon,
    )
