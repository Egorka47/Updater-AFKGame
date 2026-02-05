import os
import time
import threading
import ctypes

import cv2
import numpy as np
import mss

from pynput import keyboard
from pynput.keyboard import Key, Controller as KeyboardController, KeyCode

# ==================== DPI AWARE (Windows) ====================
if os.name == "nt":
    try:
        shcore = getattr(ctypes.windll, "shcore", None)
        fn = getattr(shcore, "SetProcessDpiAwareness", None) if shcore else None
        if callable(fn):
            fn(2)  # per-monitor DPI aware
        else:
            user32 = getattr(ctypes.windll, "user32", None)
            fn2 = getattr(user32, "SetProcessDPIAware", None) if user32 else None
            if callable(fn2):
                fn2()
    except Exception:
        pass

# ===================== PATHS =====================
T_PROMPT_E = r"E:\Python Project's\GTA_RP_CAR\mines\assets\prompt_E.png"
T_LKM      = r"E:\Python Project's\GTA_RP_CAR\mines\assets\LKM.jpg"

# ===================== CONFIG =====================
ROI_LT_W, ROI_LT_H = 900, 600   # левый верх (prompt_E)
ROI_RB_W, ROI_RB_H = 900, 600   # правый низ (LKM)

SCALES = [0.65, 0.75, 0.85, 0.95, 1.0, 1.08, 1.15, 1.25, 1.35]

THRESH_PROMPT_E = 0.65

# анти-ложные для LKM
ARM_THRESHOLD = 0.75
CLICK_THRESHOLD = 0.87
CONFIRM_FRAMES = 3

# ГЕЙТ: обычно кликаем только если prompt_E был недавно...
E_GATE_SECONDS = 2.5
# ...но если совпадение LKM "супер-высокое", кликаем и без prompt_E
LKM_FORCE_THRESHOLD = 0.92

CLICK_DELAY = 0.5
SCAN_DELAY = 0.08

# по твоему логу нужный монитор — mon 1 (2560x1440 @(0,0))
FORCE_MONITOR_INDEX = 1

# ===================== INPUT (keyboard via VK) =====================
kb = KeyboardController()
VK_E = 0x45

def tap_vk(vk: int, hold: float = 0.0):
    k = KeyCode.from_vk(vk)
    kb.press(k)
    try:
        if hold > 0:
            time.sleep(hold)
    finally:
        kb.release(k)

# ===================== MOUSE CLICK via SendInput (NO MOVE) =====================
user32 = ctypes.windll.user32

INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]

def sendinput_left_click_no_move():
    inp_down = INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(dx=0, dy=0, mouseData=0,
                          dwFlags=MOUSEEVENTF_LEFTDOWN,
                          time=0, dwExtraInfo=None)
        ),
    )
    inp_up = INPUT(
        type=INPUT_MOUSE,
        union=INPUT_UNION(
            mi=MOUSEINPUT(dx=0, dy=0, mouseData=0,
                          dwFlags=MOUSEEVENTF_LEFTUP,
                          time=0, dwExtraInfo=None)
        ),
    )

    user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(inp_down))
    time.sleep(0.003)
    user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(inp_up))

# ===================== CV HELPERS =====================
def load_template_gray(path: str):
    raw = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise FileNotFoundError(f"Не найден шаблон: {path}")

    if raw.ndim == 2:
        gray = raw
        mask = None
    else:
        if raw.shape[2] == 4:
            bgr = raw[:, :, :3]
            a = raw[:, :, 3]
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            mask = cv2.threshold(a, 10, 255, cv2.THRESH_BINARY)[1]
        else:
            gray = cv2.cvtColor(raw[:, :, :3], cv2.COLOR_BGR2GRAY)
            mask = None

    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    return gray, mask

templ_e, mask_e = load_template_gray(T_PROMPT_E)
templ_lkm, mask_lkm = load_template_gray(T_LKM)

def to_gray(bgr):
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (3, 3), 0)
    return g

def match_best(gray_img, templ_gray, templ_mask):
    best = (-1.0, None, None)  # val, (x,y), (w,h)
    for sc in SCALES:
        if sc == 1.0:
            t = templ_gray
            m = templ_mask
        else:
            h0, w0 = templ_gray.shape[:2]
            nw = max(12, int(w0 * sc))
            nh = max(12, int(h0 * sc))
            t = cv2.resize(templ_gray, (nw, nh), interpolation=cv2.INTER_AREA)
            if templ_mask is not None:
                m = cv2.resize(templ_mask, (nw, nh), interpolation=cv2.INTER_NEAREST)
            else:
                m = None

        h, w = t.shape[:2]
        if h > gray_img.shape[0] or w > gray_img.shape[1]:
            continue

        if m is not None:
            res = cv2.matchTemplate(gray_img, t, cv2.TM_CCORR_NORMED, mask=m)
        else:
            res = cv2.matchTemplate(gray_img, t, cv2.TM_CCOEFF_NORMED)

        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if float(max_val) > best[0]:
            best = (float(max_val), max_loc, (w, h))
    return best

# ===================== STATE =====================
running = False
stop_event = threading.Event()

_last_e_tap = 0.0
_last_e_seen = 0.0

_lkm_confirm = 0
_last_click = 0.0

# ===================== WORKER =====================
def worker():
    global running, _last_e_tap, _last_e_seen, _lkm_confirm, _last_click

    with mss.mss() as sct:
        idx = FORCE_MONITOR_INDEX
        m = sct.monitors[idx]
        print(f"[worker] monitor {idx}: {m['width']}x{m['height']} @({m['left']},{m['top']})")

        while not stop_event.is_set():
            if not running:
                time.sleep(0.1)
                continue

            now = time.time()

            # ---------- prompt_E (LT) ----------
            r_lt = {"left": m["left"], "top": m["top"], "width": ROI_LT_W, "height": ROI_LT_H}
            img_lt = np.array(sct.grab(r_lt), dtype=np.uint8)[:, :, :3]
            g_lt = to_gray(img_lt)

            e_val, _, _ = match_best(g_lt, templ_e, mask_e)
            if e_val >= THRESH_PROMPT_E:
                _last_e_seen = now
                if (now - _last_e_tap) >= 0.8:
                    tap_vk(VK_E, 0.02)
                    _last_e_tap = now

            # ---------- LKM (RB) ----------
            r_rb = {
                "left": m["left"] + m["width"] - ROI_RB_W,
                "top":  m["top"]  + m["height"] - ROI_RB_H,
                "width": ROI_RB_W,
                "height": ROI_RB_H,
            }
            img_rb = np.array(sct.grab(r_rb), dtype=np.uint8)[:, :, :3]
            g_rb = to_gray(img_rb)

            l_val, _, _ = match_best(g_rb, templ_lkm, mask_lkm)

            # 1) накопление подтверждений LKM
            if l_val >= ARM_THRESHOLD:
                _lkm_confirm += 1
            else:
                _lkm_confirm = 0

            # 2) гейт: либо E видели недавно, либо LKM совпал "очень сильно"
            e_gate_ok = (now - _last_e_seen) <= E_GATE_SECONDS
            lkm_force_ok = l_val >= LKM_FORCE_THRESHOLD
            allowed_to_click = e_gate_ok or lkm_force_ok

            if not allowed_to_click:
                time.sleep(SCAN_DELAY)
                continue

            # 3) клик только при стабильном детекте + задержка
            if (
                _lkm_confirm >= CONFIRM_FRAMES
                and l_val >= CLICK_THRESHOLD
                and (now - _last_click) >= CLICK_DELAY
            ):
                sendinput_left_click_no_move()
                _last_click = now
                _lkm_confirm = 0
                print(f"CLICK (lkm={l_val:.3f}, e={e_val:.3f}, gate={'E' if e_gate_ok else 'FORCE'})")
            else:
                time.sleep(SCAN_DELAY)

# ===================== HOTKEY =====================
def on_press(key):
    global running
    if key == Key.f9:
        running = not running
        print("[F9]", "ON" if running else "OFF")

# ===================== START =====================
threading.Thread(target=worker, daemon=True).start()
print("F9 — ON/OFF (E via VK, LKM via SendInput no-move; smart gate)")
with keyboard.Listener(on_press=on_press) as listener:
    listener.join()
