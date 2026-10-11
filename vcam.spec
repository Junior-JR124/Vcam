# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for vcam.exe (the GUI control panel).

Single file (default) -- one portable exe, but it unpacks itself to a temp dir
on every launch, so startup is slow:
    pyinstaller --noconfirm vcam.spec                -> dist/vcam.exe

Folder (--onedir) -- nothing to unpack, so it starts much faster; ship/zip the
whole dist/vcam/ folder (vcam.exe + _internal/):
    pyinstaller --noconfirm vcam.spec -- --onedir    -> dist/vcam/vcam.exe
"""
import argparse
import os
from PyInstaller.utils.hooks import collect_all

# Options after `--` on the pyinstaller command line reach the spec (PyInstaller 6+).
_opts = argparse.ArgumentParser()
_opts.add_argument("--onedir", action="store_true", help="build a folder instead of one exe")
ONEDIR = _opts.parse_args().onedir

datas = [
    ("models/rvm_mobilenetv3_fp32.onnx", "models"),        # bundle the RVM model
    ("assets/icon.ico", "assets"),                          # window icon
    ("assets/icon-256.png", "assets"),
]
binaries = []
hiddenimports = []

# Native packages whose DLLs/data PyInstaller's default analysis can miss.
for pkg in ("onnxruntime", "pyvirtualcam", "cv2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# vcam reads the webcam via DirectShow (CAP_DSHOW) and never decodes video files,
# so OpenCV's ffmpeg video-I/O DLLs (~28 MB each, two versions get bundled) are
# dead weight. Dropping them slims the exe by ~55 MB with no loss of function.
binaries = [b for b in binaries
            if "opencv_videoio_ffmpeg" not in (str(b[0]) + str(b[1])).lower()]

a = Analysis(
    ["vcam_ui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # MediaPipe (and its heavy deps) are not used by the RVM engine; excluding
    # them keeps the exe small and avoids MediaPipe's painful packaging. The
    # second group (yt_dlp / requests / websockets / curl_cffi and friends) just
    # happens to be installed in the build env and gets swept in by a hook —
    # vcam never imports it (it uses stdlib urllib.request for the one download).
    excludes=["mediapipe", "matplotlib", "jax", "jaxlib", "tensorflow",
              "torch", "pandas", "scipy", "IPython", "notebook",
              "yt_dlp", "websockets", "requests", "urllib3", "mutagen",
              "brotli", "brotlicffi", "certifi", "secretstorage",
              "curl_cffi", "Cryptodome"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    *([] if ONEDIR else [a.binaries, a.datas]),   # onedir: COLLECT copies these
    [],
    exclude_binaries=ONEDIR,
    name="vcam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # GUI app — no console window
    disable_windowed_traceback=False,
    icon="assets/icon.ico",
)

if ONEDIR:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="vcam",        # -> dist/vcam/
    )
