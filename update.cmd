@echo %cmdcmdline% | find /i "%~nx0" >nul && set VCAM_PAUSE=1 & python -x "%~f0" %* && exit /b 0 || exit /b 1
# ---------------------------------------------------------------------------
# update.cmd -- adds "rotate camera image" (0/90/180/270) to vcam.
#
# This file is a batch/Python hybrid: line 1 (batch) launches Python on this
# same file with -x, which makes Python skip that first line. Everything below
# is plain Python 3. Requires `python` on PATH (any 3.8+; no extra packages).
#
# Usage (run it from the folder that contains vcam.py and vcam_ui.py, or
# double-click it from there):
#     update.cmd              apply the patch
#     update.cmd --dry-run    check that every edit matches, write nothing
#     update.cmd --revert     restore the files from the newest backup
#
# Safety: all edits are verified in memory first (each anchor must match
# exactly once, and the result must compile). Only then are the originals
# copied to update_backup_<timestamp>\ and the new files written. If anything
# doesn't match, nothing is changed. Running it twice is harmless.
# ---------------------------------------------------------------------------
import datetime
import os
import shutil
import sys
from pathlib import Path

HERE = Path(sys.argv[0]).resolve().parent


def T(s):
    """Drop the newline that follows the opening quotes of a patch string."""
    return s[1:]


# =========================================================================== #
# vcam.py
# =========================================================================== #
VCAM_PATCHES = []


def v(label, old, new):
    VCAM_PATCHES.append((label, T(old), T(new)))


# 1. helpers, placed just before FrameGrabber
v("helpers", r'''
class FrameGrabber:
''', r'''
ROTATE_CODES = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                270: cv2.ROTATE_90_COUNTERCLOCKWISE}   # degrees clockwise


def normalize_rotation(deg) -> int:
    """Validate a rotation in degrees clockwise (0/90/180/270)."""
    deg = int(deg) % 360
    if deg not in ROTATE_CODES:
        raise ValueError("rotation must be 0, 90, 180 or 270")
    return deg


def rotated_size(w: int, h: int, rot: int) -> tuple[int, int]:
    """(w, h) after rotating a frame by `rot` degrees (90/270 swap them)."""
    return (h, w) if rot in (90, 270) else (w, h)


class FrameGrabber:
''')

# 2. FrameGrabber takes a rotation
v("FrameGrabber.__init__", r'''
    def __init__(self, cap: cv2.VideoCapture):
        self.cap = cap
''', r'''
    def __init__(self, cap: cv2.VideoCapture, rotation: int = 0):
        self.cap = cap
        self._rot_code = ROTATE_CODES[normalize_rotation(rotation)]
''')

# 3. rotate in the grabber thread (overlaps inference)
v("FrameGrabber._loop", r'''
            with self._lock:
                self._frame = frame
''', r'''
            if self._rot_code is not None:
                frame = cv2.rotate(frame, self._rot_code)
            with self._lock:
                self._frame = frame
''')

# 4. worker default + restart key
v("VCamWorker.DEFAULTS", r'''
        edge_radius=12, temporal=0.5, mask_scale=1.0,
    )
''', r'''
        edge_radius=12, temporal=0.5, mask_scale=1.0, rotation=0,
    )
''')

v("VCamWorker._RESTART_KEYS", r'''
                               "out_height", "engine", "fps", "threads", "device"})
''', r'''
                               "out_height", "engine", "fps", "threads", "device",
                               "rotation"})
''')

# 5. session: swap sizes BEFORE make_engine so everything is built at the real shape
v("_run_session sizes", r'''
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or cfg["height"]
            engine = make_engine(types.SimpleNamespace(**cfg), width, height)
''', r'''
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or cfg["height"]
            rot = normalize_rotation(cfg.get("rotation", 0))
            width, height = rotated_size(width, height, rot)   # 90/270 swap
            engine = make_engine(types.SimpleNamespace(**cfg), width, height)
''')

v("_run_session output size", r'''
            out_w = cfg["out_width"] or width
            out_h = cfg["out_height"] or height
''', r'''
            ow, oh = cfg["out_width"], cfg["out_height"]
            if ow and oh and rot in (90, 270):     # Smooth mode: 1280x720 -> 720x1280
                ow, oh = oh, ow
            out_w = ow or width
            out_h = oh or height
''')

v("_run_session grabber", r'''
            grabber = FrameGrabber(cap).start()
            self.running = True
''', r'''
            grabber = FrameGrabber(cap, rot).start()
            self.running = True
''')

# 6. command-line path (run())
v("run() rotation", r'''
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
''', r'''
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    rot = normalize_rotation(getattr(args, "rotate", 0))
    width, height = rotated_size(width, height, rot)
    if rot:
        print(f"[vcam] Rotating camera image {rot} deg clockwise")
''')

v("run() output size", r'''
    out_w = args.out_width or width
    out_h = args.out_height or height
''', r'''
    ow, oh = args.out_width, args.out_height
    if ow and oh and rot in (90, 270):
        ow, oh = oh, ow
    out_w = ow or width
    out_h = oh or height
''')

v("run() grabber", r'''
    grabber = FrameGrabber(cap).start()
    # Compositing + cam.send run on a worker thread so they overlap the next
''', r'''
    grabber = FrameGrabber(cap, rot).start()
    # Compositing + cam.send run on a worker thread so they overlap the next
''')

v("--rotate option", r'''
    p.add_argument("--mirror", action="store_true", help="horizontally flip (selfie view)")
''', r'''
    p.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0,
                   help="rotate the camera image clockwise by N degrees (applied before --mirror)")
    p.add_argument("--mirror", action="store_true", help="horizontally flip (selfie view)")
''')

# =========================================================================== #
# vcam_ui.py
# =========================================================================== #
UI_PATCHES = []


def u(label, old, new):
    UI_PATCHES.append((label, T(old), T(new)))


u("constants", r'''
PREVIEW_W = 480
''', r'''
PREVIEW_W = 480
PREVIEW_H = 270                       # fixed preview box (portrait frames are letterboxed)

# Rotate dropdown: label -> degrees clockwise. Applied to the camera image before
# Mirror, so "Mirror" always means a horizontal flip of what you see.
ROTATIONS = {"0\u00b0 (none)": 0, "90\u00b0 clockwise": 90, "180\u00b0": 180,
             "270\u00b0 (90\u00b0 counter-clockwise)": 270}
''')

u("load rotation", r'''
        self.bg_path = settings.get("image")
''', r'''
        self.rotation = settings.get("rotation", 0)
        if self.rotation not in ROTATIONS.values():
            self.rotation = 0
        self.bg_path = settings.get("image")
''')

u("rotate row + hint", r'''
        ttk.Label(op, text="(All options apply live; changing Speed / Engine / Device / "
                           "Camera briefly reconnects the camera)",
                  foreground="#888", wraplength=320, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", **pad)
''', r'''
        ttk.Label(op, text="Rotate").grid(row=5, column=0, sticky="e", **pad)
        self.rot_var = tk.StringVar(value=next(
            k for k, v in ROTATIONS.items() if v == self.rotation))
        rot_cb = ttk.Combobox(op, textvariable=self.rot_var, values=list(ROTATIONS),
                              width=20, state="readonly")
        rot_cb.grid(row=5, column=1, columnspan=2, sticky="w", **pad)
        rot_cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_rotation())
        ttk.Label(op, text="(All options apply live; changing Speed / Engine / Device / "
                           "Rotate / Camera briefly reconnects the camera)",
                  foreground="#888", wraplength=320, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w", **pad)
''')

u("toggle passes rotation", r'''
            device=self._device_key(),
            width=cw, height=ch, out_width=ow, out_height=oh)
''', r'''
            device=self._device_key(), rotation=self.rotation,
            width=cw, height=ch, out_width=ow, out_height=oh)
''')

u("_apply_rotation", r'''
    def _apply_camera(self):
''', r'''
    def _apply_rotation(self):
        # Rotation can swap width/height -> worker reconnects the camera automatically.
        self.rotation = ROTATIONS[self.rot_var.get()]
        self.worker.configure(rotation=self.rotation)
        self._save_settings()

    def _apply_camera(self):
''')

u("save rotation", r'''
                                                 "image": self.bg_path}, indent=2),
''', r'''
                                                 "image": self.bg_path,
                                                 "rotation": self.rotation}, indent=2),
''')

u("letterboxed preview", r'''
            disp = cv2.resize(frame, (PREVIEW_W, int(h * PREVIEW_W / w)))
''', r'''
            s = min(PREVIEW_W / w, PREVIEW_H / h)          # fit inside the box (portrait ok)
            disp = cv2.resize(frame, (max(1, int(w * s)), max(1, int(h * s))))
            dh, dw = disp.shape[:2]
            top, left = (PREVIEW_H - dh) // 2, (PREVIEW_W - dw) // 2
            disp = cv2.copyMakeBorder(disp, top, PREVIEW_H - dh - top,
                                      left, PREVIEW_W - dw - left,
                                      cv2.BORDER_CONSTANT, value=(34, 34, 34))
''')

# file -> (marker proving the patch is already applied, patch list)
TARGETS = {
    "vcam.py": ("ROTATE_CODES = {", VCAM_PATCHES),
    "vcam_ui.py": ("ROTATIONS = {", UI_PATCHES),
}


# =========================================================================== #
# patch engine
# =========================================================================== #
def read_text(path):
    raw = path.read_bytes()
    return raw.decode("utf-8").replace("\r\n", "\n"), (b"\r\n" in raw)


def write_text(path, text, crlf):
    if crlf:
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))


def revert():
    backups = sorted(p for p in HERE.glob("update_backup_*") if p.is_dir())
    if not backups:
        print("No update_backup_* folder found; nothing to revert.")
        return 1
    newest = backups[-1]
    for f in newest.iterdir():
        if f.is_file():
            shutil.copy2(f, HERE / f.name)
            print(f"[restored] {f.name}  <- {newest.name}")
    return 0


def apply(dry_run):
    plan, errors = {}, []
    for name, (marker, patches) in TARGETS.items():
        path = HERE / name
        if not path.is_file():
            errors.append(f"{name}: not found in {HERE}")
            continue
        original, crlf = read_text(path)
        if marker in original:
            print(f"[skip]  {name}: rotation support is already present")
            continue
        text, bad = original, []
        for label, old, new in patches:
            n = original.count(old)
            if n != 1:
                bad.append(f"{name}: edit '{label}' matched {n} times (expected exactly 1) "
                           "- the file differs from the version this patch was written for")
                continue
            text = text.replace(old, new, 1)
        if not bad:
            try:
                compile(text, name, "exec")
            except SyntaxError as exc:
                bad.append(f"{name}: patched code failed to compile ({exc})")
        if bad:
            errors.extend(bad)
        else:
            plan[name] = (path, original, text, crlf, len(patches))

    if errors:
        print("\nNOTHING WAS CHANGED. Problems:")
        for e in errors:
            print("  -", e)
        return 1
    if not plan:
        print("Nothing to do.")
        return 0

    for name, (_p, _o, _t, _c, n) in plan.items():
        print(f"[ok]    {name}: {n} edits verified")
    if dry_run:
        print("\nDry run only - no files written.")
        return 0

    backup = HERE / ("update_backup_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup.mkdir()
    for name, (path, _o, _t, _c, _n) in plan.items():
        shutil.copy2(path, backup / name)
    for name, (path, _o, text, crlf, _n) in plan.items():
        write_text(path, text, crlf)
        print(f"[write] {name}")
    print(f"\nDone. Originals saved in {backup.name}\\")
    print("Rebuild the exe with build.cmd to include the change.")
    return 0


def main():
    args = sys.argv[1:]
    try:
        if "--revert" in args:
            return revert()
        return apply("--dry-run" in args)
    finally:
        if os.environ.get("VCAM_PAUSE"):          # double-clicked: keep the window open
            try:
                input("\nPress Enter to close...")
            except EOFError:
                pass


sys.exit(main())
