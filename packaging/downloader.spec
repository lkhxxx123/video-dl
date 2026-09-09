# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置 — 绿色便携包（onedir）。

构建: pyinstaller --noconfirm packaging/downloader.spec （仓库根执行）
产物: dist/视频下载器/  （+ 构建脚本会拷入 runtime/browsers 与 runtime/tools）
"""
import os

# SPECPATH = 本 spec 所在目录(packaging/)；仓库根是其上一级
ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

block_cipher = None

a = Analysis(
    [os.path.join(ROOT, 'run.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # UI 页面随包分发
        (os.path.join(ROOT, 'app/ui/index.html'), 'app/ui'),
    ],
    hiddenimports=[
        # pywebview Windows 后端（WebView2/WinForms）
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        # 子命令懒加载的模块必须显式列入（run.py 里是函数内 import）
        'modes.jx', 'modes.clips', 'modes.auto',
        'platforms.douyin.dl', 'platforms.douyin.search',
        'platforms.douyin.series', 'platforms.douyin.config',
        'core.watermark', 'core.browser', 'core.download',
        'core.state', 'core.filter', 'core.naming', 'core.reporting',
        'app.service', 'app.tasks', 'app.ui.bridge', 'app.ui.gui',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
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
    name='视频下载器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # 隐藏终端窗口; print 类输出重定向 logs/运行日志.txt
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, 'packaging', 'app.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='视频下载器',
)
