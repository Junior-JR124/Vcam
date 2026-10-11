#!/usr/bin/env python3
"""
vcam_ui — a small control panel for the virtual camera.

  * Install / Uninstall the OBS Virtual Camera (so it appears in Google Meet,
    Zoom, Teams, ... as a camera option).
  * Turn the camera ON / OFF (start/stop feeding it; ON == webcam is in use).
  * Pick the background: blur, an image, or a green screen.
  * Live preview.

Run with:  python vcam_ui.py
Then in Google Meet:  Settings -> Video -> Camera -> "OBS Virtual Camera".
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# A PyInstaller --windowed .exe has no console: sys.stdout/stderr are None, and
# the library's print() calls would crash. Route them to a sink before anything
# else imports / runs.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import cv2
from PIL import Image, ImageOps, ImageTk

import vcam

PREVIEW_W = 480

# Background gallery (under the preview): a Browse tile + one thumbnail per image
# in the chosen folder. 4 columns of 16:9 tiles fill the preview's width; the
# canvas shows 2 rows and scrolls for the rest.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
GAL_COLS = 4
THUMB_W, THUMB_H = 108, 61
TILE_BORDER = 3                       # selection highlight width (px)
TILE_PAD = 3
GAL_ROWS_VISIBLE = 2
SELECTED_COLOR = "#1a73e8"

# Remembers the gallery folder and chosen image between launches.
SETTINGS_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "vcam" / "ui_settings.json"

# label -> (capture_w, capture_h, out_w, out_h). "Smooth" captures at a fast
# low-res the webcam can deliver at 30 fps, then upscales the matted output.
# The capture size is 16:9 (matches the 1280x720 output) so the upscale is
# uniform — a 4:3 capture would stretch you horizontally — and 360p mattes
# ~20% faster than 480p (fewer pixels through the net).
SPEED_MODES = {
    "Sharp (720p, ~camera fps)": (1280, 720, None, None),
    "Smooth (30 fps, upscaled)": (640, 360, 1280, 720),
}
SPEED_DEFAULT = "Sharp (720p, ~camera fps)"

# label -> RVM downsample_ratio (the quality<->speed dial; resolution unchanged).
# Higher = sharper hair/edges but slower; lower = faster but softer. Applies live
# while the camera is on (RVM engine only). 0.375 is RVM's recommended 720p point.
# NOTE: above ~0.5 you leave RVM's trained coarse-resolution range, so the matte
# barely gets sharper while speed craters (0.75 ≈ 6x slower for ~no visible gain).
# The 0.60/0.75 rows are included so you can judge for yourself; 0.50 is the
# practical sharpest.
QUALITY_PRESETS = {
    "Max sharp (0.75, very slow)": 0.75,
    "Extra sharp (0.60, slow)": 0.60,
    "Sharpest (0.50)": 0.50,
    "Sharper (0.40)": 0.40,
    "Balanced (0.375, default)": 0.375,
    "Smoother (0.30)": 0.30,
    "Fast (0.25)": 0.25,
    "Fastest (0.20)": 0.20,
}
QUALITY_DEFAULT = "Balanced (0.375, default)"

# Blur sigma multiplier range for the "Blur balance" slider. Governs both the
# live "Blur" background mode and the "Blur image" checkbox; 1.0 reproduces
# the original fixed blur amount. Applies live (rebuilds the compositor, no
# camera reconnect).
BLUR_MIN = 0.1
BLUR_MAX = 3.0
BLUR_DEFAULT = 0.25

# Device dropdown labels / short names shown in the status line.
DEVICE_LABELS = {"auto": "Auto (fastest)", "cpu": "CPU",
                 "cuda": "GPU (NVIDIA CUDA)", "dml": "GPU (DirectML)"}
PROVIDER_SHORT = {"CPUExecutionProvider": "CPU", "CUDAExecutionProvider": "CUDA",
                  "DmlExecutionProvider": "DirectML"}

class VCamUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("vcam — Virtual Camera")
        root.resizable(False, False)
        self.worker = vcam.VCamWorker()
        self._preview_imgtk = None
        self._busy = False

        # Gallery state: tiles keyed by image path, filled in by a background
        # thumbnail thread via _thumb_q. _gal_gen invalidates a stale load when
        # another folder is browsed mid-way.
        self._tiles: dict[str, tk.Label] = {}
        self._thumb_q: queue.Queue = queue.Queue()
        self._gal_gen = 0

        # Restore the last gallery folder / image; else fall back to the user's
        # image background if one sits next to the app.
        settings = self._load_settings()
        self.gal_folder = settings.get("folder")
        if self.gal_folder and not Path(self.gal_folder).is_dir():
            self.gal_folder = None
        self.bg_path = settings.get("image")
        if not (self.bg_path and Path(self.bg_path).is_file()):
            default_img = vcam.HERE / "2.png"
            self.bg_path = str(default_img) if default_img.is_file() else None

        self._build()
        self._refresh_vc_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.gal_folder:
            self._load_folder(self.gal_folder)
        self._tick()

    # ------------------------------------------------------------------ UI --
    def _build(self):
        pad = dict(padx=8, pady=4)

        # Left column: controls --------------------------------------------
        left = ttk.Frame(self.root)
        left.grid(row=0, column=0, sticky="n")

        # Virtual camera install/uninstall
        vc = ttk.LabelFrame(left, text="1. Virtual camera (for Google Meet)")
        vc.grid(row=0, column=0, sticky="ew", **pad)
        self.vc_status = ttk.Label(vc, text="…", wraplength=300, justify="left")
        self.vc_status.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        self.install_btn = ttk.Button(vc, text="Install", command=self._install)
        self.install_btn.grid(row=1, column=0, sticky="ew", **pad)
        self.uninstall_btn = ttk.Button(vc, text="Uninstall", command=self._uninstall)
        self.uninstall_btn.grid(row=1, column=1, sticky="ew", **pad)

        # On / Off
        oc = ttk.LabelFrame(left, text="2. Camera on / off")
        oc.grid(row=1, column=0, sticky="ew", **pad)
        self.toggle_btn = ttk.Button(oc, text="Turn ON", command=self._toggle)
        self.toggle_btn.grid(row=0, column=0, sticky="ew", **pad)
        self.run_status = ttk.Label(oc, text="Stopped", foreground="gray")
        self.run_status.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(oc, text="Camera #").grid(row=1, column=0, sticky="e", **pad)
        self.cam_idx = tk.IntVar(value=0)
        cam_spin = ttk.Spinbox(oc, from_=0, to=8, width=4, textvariable=self.cam_idx,
                               command=self._apply_camera)
        cam_spin.grid(row=1, column=1, sticky="w", **pad)
        cam_spin.bind("<Return>", lambda _e: self._apply_camera())
        cam_spin.bind("<FocusOut>", lambda _e: self._apply_camera())

        # Background
        # (The background image itself is picked in the gallery under the preview.)
        bgf = ttk.LabelFrame(left, text="3. Background")
        bgf.grid(row=2, column=0, sticky="ew", **pad)
        self.blur_image = tk.BooleanVar(value=False)
        ttk.Checkbutton(bgf, text="Blur image (soft, out-of-focus background)",
                        variable=self.blur_image, command=self._apply_bg).grid(
            row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(bgf, text="Blur balance").grid(row=3, column=0, sticky="e", **pad)
        self.blur_strength = tk.DoubleVar(value=BLUR_DEFAULT)
        blur_scale = ttk.Scale(bgf, from_=BLUR_MIN, to=BLUR_MAX, orient="horizontal",
                               variable=self.blur_strength, command=self._on_blur_scale)
        blur_scale.grid(row=3, column=1, sticky="ew", **pad)
        self.blur_value_label = ttk.Label(bgf, text=f"{BLUR_DEFAULT:.2f}x", width=5)
        self.blur_value_label.grid(row=3, column=2, sticky="w", **pad)

        # Options
        op = ttk.LabelFrame(left, text="4. Options")
        op.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Label(op, text="Speed").grid(row=0, column=0, sticky="e", **pad)
        self.quality = tk.StringVar(value=SPEED_DEFAULT)
        speed_cb = ttk.Combobox(op, textvariable=self.quality, values=list(SPEED_MODES),
                                width=20, state="readonly")
        speed_cb.grid(row=0, column=1, columnspan=2, sticky="w", **pad)
        speed_cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_speed())
        ttk.Label(op, text="Quality").grid(row=1, column=0, sticky="e", **pad)
        self.qbalance = tk.StringVar(value=QUALITY_DEFAULT)
        qb = ttk.Combobox(op, textvariable=self.qbalance, values=list(QUALITY_PRESETS),
                          width=20, state="readonly")
        qb.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        qb.bind("<<ComboboxSelected>>", lambda _e: self._apply_quality())
        ttk.Label(op, text="Engine").grid(row=2, column=0, sticky="e", **pad)
        self.engine = tk.StringVar(value="auto")
        eng_cb = ttk.Combobox(op, textvariable=self.engine, values=["auto", "rvm", "mediapipe"],
                              width=10, state="readonly")
        eng_cb.grid(row=2, column=1, sticky="w", **pad)
        eng_cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_engine())
        self.mirror = tk.BooleanVar(value=False)
        ttk.Checkbutton(op, text="Mirror", variable=self.mirror,
                        command=self._apply_opts).grid(row=2, column=2, sticky="w", **pad)
        self.one_person = tk.BooleanVar(value=True)
        ttk.Checkbutton(op, text="Only one person", variable=self.one_person,
                        command=self._apply_oneperson).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(op, text="Device").grid(row=4, column=0, sticky="e", **pad)
        self._dev_by_label = {DEVICE_LABELS[k]: k for k in vcam.available_devices()}
        self.device = tk.StringVar(value=DEVICE_LABELS["auto"])
        dev_cb = ttk.Combobox(op, textvariable=self.device, values=list(self._dev_by_label),
                              width=20, state="readonly")
        dev_cb.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        dev_cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_device())
        ttk.Label(op, text="(All options apply live; changing Speed / Engine / Device / "
                           "Camera briefly reconnects the camera)",
                  foreground="#888", wraplength=320, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w", **pad)

        # Right column: preview + background gallery -----------------------
        right_col = ttk.Frame(self.root)
        right_col.grid(row=0, column=1, sticky="n")
        right = ttk.LabelFrame(right_col, text="Preview")
        right.grid(row=0, column=0, sticky="ew", **pad)
        self.preview = ttk.Label(right, text="(camera off)", anchor="center",
                                 width=40, background="#222", foreground="#aaa")
        self.preview.grid(row=0, column=0, padx=6, pady=6)
        self.fps_label = ttk.Label(right, text="", wraplength=PREVIEW_W, justify="left")
        self.fps_label.grid(row=1, column=0, sticky="w", padx=6)

        self.gal_frame = ttk.LabelFrame(right_col, text=self._gallery_title())
        self.gal_frame.grid(row=1, column=0, sticky="ew", **pad)
        tile_w = THUMB_W + 2 * (TILE_BORDER + TILE_PAD)
        tile_h = THUMB_H + 2 * (TILE_BORDER + TILE_PAD)
        self.gal_canvas = tk.Canvas(self.gal_frame, width=GAL_COLS * tile_w,
                                    height=GAL_ROWS_VISIBLE * tile_h,
                                    highlightthickness=0, yscrollincrement=tile_h)
        gal_scroll = ttk.Scrollbar(self.gal_frame, orient="vertical",
                                   command=self.gal_canvas.yview)
        self.gal_canvas.configure(yscrollcommand=gal_scroll.set)
        self.gal_canvas.grid(row=0, column=0, padx=(6, 0), pady=6)
        gal_scroll.grid(row=0, column=1, sticky="ns", padx=(0, 6), pady=6)
        self._gal_inner = tk.Frame(self.gal_canvas)
        self._tile_bg = self._gal_inner.cget("bg")
        self.gal_canvas.create_window((0, 0), window=self._gal_inner, anchor="nw")
        self._gal_inner.bind("<Configure>", lambda _e: self.gal_canvas.configure(
            scrollregion=self.gal_canvas.bbox("all")))
        # Mouse wheel scrolls the gallery only while the pointer is over it.
        self.gal_canvas.bind("<Enter>", lambda _e: self.gal_canvas.bind_all(
            "<MouseWheel>", self._on_gallery_wheel))
        self.gal_canvas.bind("<Leave>", lambda _e: self.gal_canvas.unbind_all("<MouseWheel>"))

        # Slot 0: the Browse tile (same size as an image tile).
        browse_cell = ttk.Frame(self._gal_inner, width=THUMB_W + 2 * TILE_BORDER,
                                height=THUMB_H + 2 * TILE_BORDER)
        browse_cell.grid_propagate(False)
        browse_cell.columnconfigure(0, weight=1)
        browse_cell.rowconfigure(0, weight=1)
        browse_cell.grid(row=0, column=0, padx=TILE_PAD, pady=TILE_PAD)
        ttk.Button(browse_cell, text="📁  Browse…", command=self._browse_folder).grid(
            row=0, column=0, sticky="nsew")

        ttk.Label(self.root,
                  text="In Google Meet: Settings ▸ Video ▸ Camera ▸ \"OBS Virtual Camera\"",
                  foreground="#444").grid(row=1, column=0, columnspan=2, sticky="w", **pad)

    # -------------------------------------------------------------- actions --
    def _mode(self):
        # An image background once one is picked; live blur of the real room until then.
        return "image" if self.bg_path else "blur"

    def _install(self):
        self._vcam_action(vcam.install_virtualcam, "Install")

    def _uninstall(self):
        self._vcam_action(vcam.uninstall_virtualcam, "Uninstall")

    def _vcam_action(self, fn, verb):
        if self._busy:
            return
        if verb == "Uninstall" and self.worker.running:
            messagebox.showwarning("Virtual Camera", "Turn the camera OFF first.")
            return
        if not messagebox.askyesno(
            "Virtual Camera",
            f"{verb} the OBS Virtual Camera now?\n\n"
            "This needs administrator rights — a Windows UAC prompt will appear."):
            return
        self._set_busy(True, f"{verb}ing…")

        def work():
            ok, msg = fn()
            self.root.after(0, lambda: self._vcam_done(ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def _vcam_done(self, ok, msg):
        self._set_busy(False)
        self._refresh_vc_status()
        (messagebox.showinfo if ok else messagebox.showerror)("Virtual Camera", msg)

    def _set_busy(self, busy, text=None):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.install_btn.config(state=state)
        self.uninstall_btn.config(state=state)
        if text:
            self.vc_status.config(text=text)

    def _refresh_vc_status(self):
        st = vcam.virtualcam_status()
        if st["installed"]:
            self.vc_status.config(text="Installed ✓  (\"OBS Virtual Camera\" is available)",
                                  foreground="green")
        elif st["obs_found"]:
            self.vc_status.config(text="Not registered — click Install.", foreground="#a60")
        else:
            self.vc_status.config(
                text="OBS Studio not found. Install it from obsproject.com, then Install here.",
                foreground="red")

    def _toggle(self):
        if self.worker.running or self.worker.status == "starting":
            self.toggle_btn.config(text="Turn ON")
            self.worker.stop()
            return
        cw, ch, ow, oh = SPEED_MODES[self.quality.get()]
        self.worker.configure(
            camera=self.cam_idx.get(), engine=self.engine.get(), mode=self._mode(),
            bg_path=self.bg_path, blur_image=self.blur_image.get(),
            blur_strength=self.blur_strength.get(),
            mirror=self.mirror.get(), multi_person=not self.one_person.get(),
            downsample=QUALITY_PRESETS[self.qbalance.get()],
            device=self._device_key(),
            width=cw, height=ch, out_width=ow, out_height=oh)
        self.toggle_btn.config(text="Turn OFF")
        self.worker.start()

    def _apply_bg(self):
        self.worker.set_background(self._mode(), path=self.bg_path,
                                   blur_image=self.blur_image.get())

    def _on_blur_scale(self, value):
        # Live: rebuilds the compositor with the new sigma (Blur mode and/or
        # Blur image), no camera reconnect.
        v = float(value)
        self.blur_value_label.config(text=f"{v:.2f}x")
        self.worker.set_blur_strength(v)

    def _apply_opts(self):
        self.worker.configure(mirror=self.mirror.get())

    def _apply_quality(self):
        # Live quality<->speed balance (RVM only); takes effect on the next frame.
        self.worker.set_quality(QUALITY_PRESETS[self.qbalance.get()])

    def _apply_oneperson(self):
        # Live (in-place): keep only the largest person, or everyone.
        self.worker.configure(multi_person=not self.one_person.get())

    def _apply_speed(self):
        # Capture/output size change -> worker reconnects the camera automatically.
        cw, ch, ow, oh = SPEED_MODES[self.quality.get()]
        self.worker.configure(width=cw, height=ch, out_width=ow, out_height=oh)

    def _apply_engine(self):
        # Engine swap -> worker rebuilds the matting engine (auto reconnect).
        self.worker.configure(engine=self.engine.get())

    def _apply_camera(self):
        # Camera index change -> worker reopens the device (auto reconnect).
        try:
            self.worker.configure(camera=int(self.cam_idx.get()))
        except (tk.TclError, ValueError):
            pass   # mid-typing / empty spinbox; ignore until a valid value

    # -------------------------------------------------------------- gallery --
    def _gallery_title(self):
        name = Path(self.gal_folder).name if self.gal_folder else ""
        return f"Background images — {name}" if name else "Background images"

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder of background images",
                                         initialdir=self.gal_folder or str(Path.home()))
        if not folder:
            return
        self.gal_folder = str(Path(folder))
        self.gal_frame.config(text=self._gallery_title())
        self._save_settings()
        self._load_folder(self.gal_folder)

    def _load_folder(self, folder):
        """Clear the gallery and thumbnail `folder`'s images on a background
        thread (big photos take a while to decode; the UI stays responsive).
        Tiles appear as they're ready, via _drain_thumbs() in the main loop."""
        self._gal_gen += 1
        for tile in self._tiles.values():
            tile.destroy()
        self._tiles.clear()
        self.gal_canvas.yview_moveto(0)
        if not folder:
            return                              # just cleared the gallery
        try:
            files = sorted((p for p in Path(folder).iterdir()
                            if p.suffix.lower() in IMAGE_EXTS and p.is_file()),
                           key=lambda p: p.name.lower())
        except OSError:
            files = []
        threading.Thread(target=self._thumb_worker, args=(self._gal_gen, files),
                         daemon=True).start()

    def _thumb_worker(self, gen, files):
        for p in files:
            if gen != self._gal_gen:
                return                          # another folder was browsed; abandon
            try:
                with Image.open(p) as im:
                    im.draft("RGB", (THUMB_W * 2, THUMB_H * 2))   # fast JPEG downscale
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    thumb = ImageOps.fit(im, (THUMB_W, THUMB_H), Image.LANCZOS)
            except Exception:  # noqa: BLE001  (unreadable/corrupt image: skip it)
                continue
            self._thumb_q.put((gen, str(p), thumb))

    def _drain_thumbs(self):
        """Turn finished thumbnails into tiles (Tk objects: main thread only)."""
        while True:
            try:
                gen, path, thumb = self._thumb_q.get_nowait()
            except queue.Empty:
                return
            if gen == self._gal_gen:
                self._add_tile(path, thumb)

    def _add_tile(self, path, thumb):
        slot = len(self._tiles) + 1            # slot 0 is the Browse tile
        imgtk = ImageTk.PhotoImage(thumb)
        tile = tk.Label(self._gal_inner, image=imgtk, borderwidth=0, cursor="hand2",
                        highlightthickness=TILE_BORDER,
                        highlightbackground=self._tile_color(path))
        tile.image = imgtk                     # keep a reference (Tk won't)
        tile.grid(row=slot // GAL_COLS, column=slot % GAL_COLS, padx=TILE_PAD, pady=TILE_PAD)
        tile.bind("<Button-1>", lambda _e, p=path: self._select_image(p))
        self._tiles[path] = tile

    def _tile_color(self, path):
        return SELECTED_COLOR if self._same_path(path, self.bg_path) else self._tile_bg

    @staticmethod
    def _same_path(a, b):
        return bool(a and b) and os.path.normcase(os.path.abspath(a)) == \
            os.path.normcase(os.path.abspath(b))

    def _select_image(self, path):
        # Tiles are thumbnailed once; the folder may since have been moved,
        # renamed or deleted. Don't hand the worker a dead path (it would
        # silently keep the live blur) — rescan, or ask for the folder again.
        if not Path(path).is_file():
            if self.gal_folder and Path(self.gal_folder).is_dir():
                messagebox.showwarning(
                    "Background image",
                    f"\"{Path(path).name}\" is no longer in this folder.\n\n"
                    "The gallery has been refreshed.")
                self._load_folder(self.gal_folder)
            else:
                messagebox.showwarning(
                    "Background image",
                    f"The folder \"{self.gal_folder}\" was moved or deleted.\n\n"
                    "Click Browse… to choose it again.")
                self.gal_folder = None
                self.gal_frame.config(text=self._gallery_title())
                self._load_folder(None)
                self._save_settings()
            return
        self.bg_path = path
        for p, tile in self._tiles.items():
            tile.config(highlightbackground=self._tile_color(p))
        self.worker.set_background("image", path=path, blur_image=self.blur_image.get())
        self._save_settings()

    def _on_gallery_wheel(self, event):
        # Only scroll when there's more than fits (Tk would otherwise let it drift).
        if self._gal_inner.winfo_height() > self.gal_canvas.winfo_height():
            self.gal_canvas.yview_scroll(int(-event.delta / 120), "units")

    # ------------------------------------------------------------- settings --
    @staticmethod
    def _load_settings():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps({"folder": self.gal_folder,
                                                 "image": self.bg_path}, indent=2),
                                     encoding="utf-8")
        except OSError:
            pass                                # not critical; just won't be remembered

    # ----------------------------------------------------------- main loop --
    def _tick(self):
        self._drain_thumbs()
        st = self.worker.status
        if self.worker.running and st == "running":
            self.run_status.config(text="● Running", foreground="green")
            self.toggle_btn.config(text="Turn OFF")
            prov = PROVIDER_SHORT.get(self.worker.provider, self.worker.provider or "")
            text = f"{self.worker.fps:.0f} fps · {prov} · {self.worker.device or ''}"
            if self.worker.device_note:
                text += f"\n{self.worker.device_note}"
            if self.worker.bg_warning:
                text += f"\n⚠ {self.worker.bg_warning} (using blur)"
            self.fps_label.config(text=text)
        elif st == "starting":
            self.run_status.config(text="Starting…", foreground="#a60")
            self.toggle_btn.config(text="Turn OFF")
        elif st == "error":
            self.run_status.config(text="Error (see below)", foreground="red")
            self.toggle_btn.config(text="Turn ON")
            self.fps_label.config(text=self.worker.error or "")
        else:
            self.run_status.config(text="Stopped", foreground="gray")
            self.toggle_btn.config(text="Turn ON")
            self.fps_label.config(text="")

        # No point drawing the preview while minimized; the camera keeps running.
        frame = None if self.root.state() == "iconic" else self.worker.latest_frame()
        if frame is not None:
            h, w = frame.shape[:2]
            disp = cv2.resize(frame, (PREVIEW_W, int(h * PREVIEW_W / w)))
            rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            self._preview_imgtk = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.preview.config(image=self._preview_imgtk, text="")
        elif not self.worker.running:
            self.preview.config(image="", text="(camera off)")

        self.root.after(40, self._tick)

    def _device_key(self):
        return self._dev_by_label.get(self.device.get(), "auto")

    def _apply_device(self):
        # Device change -> worker rebuilds the ONNX session (auto reconnect).
        self.worker.configure(device=self._device_key())

    def _on_close(self):
        msg = ("The virtual camera is ON — quit and turn it off?"
               if self.worker.running else "Quit vcam?")
        if not messagebox.askokcancel("Quit vcam", msg, icon="question"):
            return
        try:
            self.worker.stop()
        finally:
            self.root.destroy()


def _selftest() -> int:
    """`vcam.exe --selftest`: exercise the bundle (onnxruntime + cv2 + RVM model)
    without a camera/GUI, writing the result to vcam_selftest.txt (handy because
    the windowed build has no console)."""
    import numpy as np
    lines, ok = [], True
    try:
        eng = vcam.RvmEngine(640, 480, device="auto")
        a, fg = eng.process((np.random.rand(480, 640, 3) * 255).astype("uint8"))
        lines.append(f"RVM OK: alpha{a.shape} fg{fg.shape} dsr={eng.dsr} "
                     f"provider={eng.provider} note={eng.note!r}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"RVM FAIL: {exc!r}")
    lines.append(f"virtualcam_installed={vcam.virtualcam_installed()}")
    report = ("PASS\n" if ok else "FAIL\n") + "\n".join(lines)
    try:
        Path("vcam_selftest.txt").write_text(report, encoding="ascii", errors="replace")
    except OSError:
        pass
    print(report)
    return 0 if ok else 1


def _set_window_icon(root):
    """Set the window/taskbar icon from assets (works from source and frozen)."""
    assets = vcam.BUNDLE_DIR / "assets"
    try:
        ico = assets / "icon.ico"
        if ico.is_file():
            root.iconbitmap(default=str(ico))
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        png = assets / "icon-256.png"
        if png.is_file():
            root._icon_img = tk.PhotoImage(file=str(png))   # keep a reference
            root.iconphoto(True, root._icon_img)
    except Exception:  # noqa: BLE001
        pass


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    # Keep matting at full speed when the window is minimized or covered.
    vcam.disable_power_throttling()
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    _set_window_icon(root)
    VCamUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
