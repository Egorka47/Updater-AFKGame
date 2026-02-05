import time
import os
import sys
import argparse
import threading
import numpy as np
import mss
import cv2
import ctypes
from ctypes import wintypes

from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import KeyCode

# ===================== DPI AWARE (Windows) =====================
# Важно: до захватов экрана/окон
if os.name == "nt":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# ===================== PATH HELPERS (py + exe) =====================
def app_dir() -> str:
    # Работает и в .py, и в PyInstaller .exe
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _dist2(a, b):
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx * dx + dy * dy

BASE_DIR = app_dir()

# Логи лучше писать рядом с exe/скриптом
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "stuck_clicks.txt")

# Шаблоны (под твою структуру: GTA_RP_CAR\apelsin\assets\...)
TEMPLATE_PATH = os.path.join(BASE_DIR, "apelsin", "assets", "prompt_E.png")
DONE_TEMPLATE_PATH = os.path.join(BASE_DIR, "apelsin", "assets", "notif_orange.png")

# -------------------- НАСТРОЙКИ --------------------
TARGET_HEX = [
    "#f9eaa9",
    "#f59f18",
    "#f8a14e",
    "#a8300d",
]

EXCLUDE_HEX = [
    "#fdf3cf",
    "#b08e61",
    "#e9daa7",
    "#f5e3b8",
    "#f4e2b8",
    "#e6aa5b",
    "#efc01c",
    "#e4b019",
    "#eaad1d",
    "#ebaf1d",
    "#e4aa1d",
    "#e5b01e",
    "#e8b21f",
    "#e6af1e",
    "#ecb11b",
    "#ebb31c",
    "#e4b01f",
    "#dea722",
    "#ffe9a1",
]

CLICK_INTERVAL = 0.1
TOL = 18
COARSE_STEP = 6
LOCAL_RADIUS = 70

# --- Ограничение поиска апельсинов центральной зоной (мини-квест в центре) ---
LIMIT_SEARCH_TO_CENTER = True
CENTER_ROI_W_FRAC = 0.30
CENTER_ROI_H_FRAC = 0.85

WHITE_MIN = 245

CLUSTER_RADIUS = 4
MIN_CLUSTER_PIXELS = 22

STUCK_SECONDS = 2.5
STUCK_RADIUS_PX = 7

PRESS_E_ON_START = True
START_E_DELAY_SEC = 0.05

ENABLE_E_PROMPT = True
PROMPT_ROI_LOCAL = {"left": 0, "top": 0, "width": 520, "height": 140}
MATCH_THRESHOLD = 0.72
E_DEBOUNCE_SEC = 1.0

ENABLE_DONE_NOTIF = True
DONE_MATCH_THRESHOLD = 0.70
DONE_DEBOUNCE_SEC = 2.0

HOLD_S_SEC = 2.75
W_WAIT_PROMPT_POLL_SEC = 0.03

# --- Анти-залип по месту: 1 клик = бан зоны до следующего E ---
SKIP_AFTER_SAME_CLICKS = 1
SKIP_RADIUS_PX = 12
BANNED_TTL_SEC = 0

# --- Центровка камеры по стволу во время удержания S (плавно) ---
CENTER_ON_S = True
CENTER_ROI_W = 900
CENTER_ROI_H = 520
CENTER_MIN_AREA = 4500
CENTER_DEADZONE_PX = 10

CENTER_TICK = 0.012
CENTER_GAIN = 0.06
CENTER_MAX_STEP = 6

CENTER_SMOOTH_ALPHA = 0.18
CENTER_MAX_ACCEL = 1.2

# --- anti-stuck cursor / anti-repeat ---
SAME_POINT_RADIUS_PX = 6
SAME_POINT_MAX_CLICKS = 6
SAME_POINT_COOLDOWN_SEC = 2.0

NUDGE_AFTER_SAME = True
NUDGE_PIXELS = 25

# --------------------------------------------------

# --- РЕФЕРЕНС (под что делались шаблоны/ROI) ---
REF_W, REF_H = 2560, 1440

def parse_args():
    p = argparse.ArgumentParser(add_help=True)

    # GUI/service режим: запускаем сразу, управление идёт через stdin RUN/PAUSE/STOP
    p.add_argument("--autorun", action="store_true",
                   help="Запустить сразу (GUI режим)")

    p.add_argument("--control-stdin", action="store_true",
                   help="Слушать stdin команды: STOP / PAUSE / RUN")

    p.add_argument("--monitor", type=int, default=1,
                   help="Номер монитора mss: 1/2/... (если 1 нет — возьмём 0)")

    # --- совместимость со старым запуском/GUI ---
    # GUI мог передавать это раньше — чтобы не падало:
    p.add_argument("--no-hotkey", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--hotkey", type=str, default="F7",
                   help=argparse.SUPPRESS)

    # иногда GUI/onefile прокидывают assets-dir
    p.add_argument("--assets-dir", type=str, default="",
                   help=argparse.SUPPRESS)

    return p.parse_args()

def _build_scales(scale: float) -> tuple:
    lo = max(0.30, scale - 0.22)
    hi = min(2.50, scale + 0.22)
    vals = []
    s = lo
    step = 0.05
    while s <= hi + 1e-9:
        vals.append(round(s, 2))
        s += step
    sc = round(scale, 2)
    vals.append(sc)
    return tuple(sorted(set(vals)))

def _scale_roi_local(roi_local: dict, base_scale: float) -> dict:
    return {
        "left": int(roi_local["left"] * base_scale),
        "top": int(roi_local["top"] * base_scale),
        "width": max(8, int(roi_local["width"] * base_scale)),
        "height": max(8, int(roi_local["height"] * base_scale))}

# ---- WinAPI SendInput (FIX ULONG_PTR) ----
if sys.maxsize > 2**32:
    ULONG_PTR = ctypes.c_uint64
else:
    ULONG_PTR = ctypes.c_uint32

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

def send_mouse_move(dx: int, dy: int):
    inp = INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=0),)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

def hex_to_rgb(h: str):
    h = h.lstrip("#")
    return np.array([int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)], dtype=np.int16)

def rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))

TARGET_RGB = np.stack([hex_to_rgb(h) for h in TARGET_HEX], axis=0)
EXCLUDE_RGB = np.stack([hex_to_rgb(h) for h in EXCLUDE_HEX], axis=0)

mouse = MouseController()
kb = KeyboardController()

running = False
need_press_e_on_start = False

# мягкая остановка процесса
STOP_EVENT = threading.Event()

VK_E = 0x45
VK_W = 0x57
VK_S = 0x53

KEY_E = KeyCode.from_vk(VK_E)
KEY_W = KeyCode.from_vk(VK_W)
KEY_S = KeyCode.from_vk(VK_S)

# будем знать что потенциально может быть зажато
POSSIBLE_HELD_KEYS = [KEY_E, KEY_W, KEY_S]

def release_all_keys():
    for k in POSSIBLE_HELD_KEYS:
        try:
            kb.release(k)
        except Exception:
            pass

def key_vk(vk: int):
    return KeyCode.from_vk(vk)

def tap_vk(vk: int, hold: float = 0.0):
    k = key_vk(vk)
    kb.press(k)
    try:
        if hold > 0:
            t0 = time.time()
            while time.time() - t0 < hold:
                if STOP_EVENT.is_set():
                    break
                time.sleep(0.01)
    finally:
        try:
            kb.release(k)
        except Exception:
            pass

# ---------------- анти-залип по месту ----------------
banned_zones = []  # {"x": int, "y": int, "t": float}
last_clicked_pos = None
same_clicks_in_row = 0

def dist2(a, b):
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx * dx + dy * dy

def clear_banned():
    banned_zones.clear()

def cleanup_banned():
    if BANNED_TTL_SEC <= 0:
        return
    now = time.time()
    banned_zones[:] = [z for z in banned_zones if (now - z["t"]) <= BANNED_TTL_SEC]

def is_in_banned(pos):
    cleanup_banned()
    r2 = SKIP_RADIUS_PX * SKIP_RADIUS_PX
    for z in banned_zones:
        if dist2(pos, (z["x"], z["y"])) <= r2:
            return True
    return False

def add_banned(pos):
    banned_zones.append({"x": int(pos[0]), "y": int(pos[1]), "t": time.time()})

def reset_same_click_counter():
    global last_clicked_pos, same_clicks_in_row
    last_clicked_pos = None
    same_clicks_in_row = 0

def register_click(pos):
    add_banned(pos)
    reset_same_click_counter()
    return True
# ----------------------------------------------------

def nudge_mouse():
    try:
        x, y = mouse.position
        mouse.position = (x + NUDGE_PIXELS, y)
        time.sleep(0.01)
        mouse.position = (x, y)
    except Exception:
        pass

def press_e_once():
    clear_banned()
    tap_vk(VK_E)

def is_near_white(rgb: np.ndarray) -> np.ndarray:
    return (
        (rgb[:, :, 0] >= WHITE_MIN)
        & (rgb[:, :, 1] >= WHITE_MIN)
        & (rgb[:, :, 2] >= WHITE_MIN)
    )

def is_excluded_color(rgb: np.ndarray) -> np.ndarray:
    diffs = np.abs(rgb[:, :, None, :] - EXCLUDE_RGB[None, None, :, :])
    dist = diffs.max(axis=3)
    return dist.min(axis=2) <= TOL

def grab_region_rgb(sct, left, top, width, height):
    mon = {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}
    img = np.array(sct.grab(mon), dtype=np.uint8)  # BGRA
    return img[:, :, :3][:, :, ::-1]  # RGB

def grab_region_gray(sct, left, top, width, height):
    rgb = grab_region_rgb(sct, left, top, width, height)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

def get_pixel_rgb(sct, x, y):
    mon = {"left": int(x), "top": int(y), "width": 1, "height": 1}
    img = np.array(sct.grab(mon), dtype=np.uint8)
    b, g, r, _ = img[0, 0]
    return (int(r), int(g), int(b))

def ensure_log_dir(path):
    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)

def append_stuck_log(path, x, y, rgb, stuck_for):
    ensure_log_dir(path)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    hx = rgb_to_hex(rgb)
    line = f"[{ts}] STUCK {stuck_for:.1f}s within R={STUCK_RADIUS_PX}px near ({x},{y}) pixel RGB={rgb} HEX={hx}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)

def find_color_in_image(img_rgb: np.ndarray, step: int = 1):
    H, W, _ = img_rgb.shape
    sampled = img_rgb[::step, ::step, :].astype(np.int16)

    white_mask = is_near_white(sampled)
    exclude_mask = is_excluded_color(sampled)

    diffs = np.abs(sampled[:, :, None, :] - TARGET_RGB[None, None, :, :])
    dist = diffs.max(axis=3)
    best = dist.min(axis=2)

    best = np.where(white_mask | exclude_mask, 10_000, best)

    ys, xs = np.where(best <= TOL)
    if len(xs) == 0:
        return None

    for i in range(min(len(xs), 400)):
        x = int(xs[i] * step)
        y = int(ys[i] * step)

        x0 = max(0, x - CLUSTER_RADIUS)
        y0 = max(0, y - CLUSTER_RADIUS)
        x1 = min(W, x + CLUSTER_RADIUS + 1)
        y1 = min(H, y + CLUSTER_RADIUS + 1)

        patch = img_rgb[y0:y1, x0:x1, :].astype(np.int16)

        pdiffs = np.abs(patch[:, :, None, :] - TARGET_RGB[None, None, :, :])
        pdist = pdiffs.max(axis=3)
        pbest = pdist.min(axis=2)

        pwhite = is_near_white(patch)
        pexclude = is_excluded_color(patch)
        pbest = np.where(pwhite | pexclude, 10_000, pbest)

        if int((pbest <= TOL).sum()) >= MIN_CLUSTER_PIXELS:
            return (x, y)

    return None

def within_radius(p1, p2, r):
    if p1 is None or p2 is None:
        return False
    dx = p1[0] - p2[0]
    dy = p1[1] - p2[1]
    return (dx * dx + dy * dy) <= (r * r)

def load_template_gray(path):
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Не найден шаблон: {path}")
    return img

def template_visible(sct, template_gray, roi, threshold, scales):
    gray = grab_region_gray(sct, roi["left"], roi["top"], roi["width"], roi["height"])
    th, tw = template_gray.shape[:2]

    for scale in scales:
        new_w = max(8, int(tw * scale))
        new_h = max(8, int(th * scale))
        templ = cv2.resize(template_gray, (new_w, new_h), interpolation=cv2.INTER_AREA)

        if gray.shape[0] < templ.shape[0] or gray.shape[1] < templ.shape[1]:
            continue

        res = cv2.matchTemplate(gray, templ, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(res)
        if max_val >= threshold:
            return True
    return False

def make_done_roi(screen_w: int, screen_h: int):
    roi_w = int(screen_w * 0.55)
    roi_h = int(screen_h * 0.18)
    left = (screen_w - roi_w) // 2
    top = screen_h - roi_h
    return {"left": left, "top": top, "width": roi_w, "height": roi_h}

# ---------------- центровка камеры (ПЛАВНО) ----------------
def center_camera_on_tree(sct, screen_left, screen_top, screen_w, screen_h, duration_sec):
    start = time.time()

    roi_w = min(CENTER_ROI_W, screen_w)
    roi_h = min(CENTER_ROI_H, screen_h)
    roi_left = screen_left + (screen_w - roi_w) // 2
    roi_top = screen_top + (screen_h - roi_h) // 2

    smooth_dx = 0.0
    frac_dx = 0.0

    while (time.time() - start < duration_sec) and (not STOP_EVENT.is_set()) and running:
        rgb = grab_region_rgb(sct, roi_left, roi_top, roi_w, roi_h)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(gray, 45, 135)

        kernel = np.ones((7, 7), np.uint8)
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)

        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best = None
        best_score = 0.0

        for c in contours:
            area = cv2.contourArea(c)
            if area < CENTER_MIN_AREA:
                continue

            x, y, w, h = cv2.boundingRect(c)
            if w <= 0 or h <= 0:
                continue

            aspect = h / float(w)
            if aspect < 1.4:
                continue

            cx = x + w / 2.0
            dx = abs(cx - roi_w / 2.0)
            center_bonus = 1.0 / (1.0 + dx)

            score = area * aspect * center_bonus
            if score > best_score:
                best_score = score
                best = (x, y, w, h)

        target_dx = 0.0

        if best is not None:
            x, y, w, h = best
            trunk_cx = x + w / 2.0
            err = trunk_cx - (roi_w / 2.0)

            if abs(err) > CENTER_DEADZONE_PX:
                target_dx = float(np.clip(err * CENTER_GAIN, -CENTER_MAX_STEP, CENTER_MAX_STEP))

        smooth_dx = smooth_dx + CENTER_SMOOTH_ALPHA * (target_dx - smooth_dx)

        if smooth_dx > target_dx + CENTER_MAX_ACCEL:
            smooth_dx = target_dx + CENTER_MAX_ACCEL
        elif smooth_dx < target_dx - CENTER_MAX_ACCEL:
            smooth_dx = target_dx - CENTER_MAX_ACCEL

        frac_dx += smooth_dx
        step_dx = int(frac_dx)
        frac_dx -= step_dx

        if step_dx != 0:
            send_mouse_move(step_dx, 0)

        time.sleep(CENTER_TICK)

def hold_s_with_centering(sct, L, T, W, H, hold_sec):
    kb.press(KEY_S)
    try:
        if CENTER_ON_S:
            center_camera_on_tree(sct, L, T, W, H, hold_sec)
        else:
            t0 = time.time()
            while time.time() - t0 < hold_sec:
                if STOP_EVENT.is_set() or (not running):
                    break
                time.sleep(0.01)
    finally:
        try:
            kb.release(KEY_S)
        except Exception:
            pass

# -----------------------------------------------------------

def _stdin_control_thread():
    global running, need_press_e_on_start
    try:
        for line in sys.stdin:
            cmd = (line or "").strip().upper()
            if not cmd:
                continue
            if cmd == "STOP":
                STOP_EVENT.set()
                running = False
                release_all_keys()
                break
            elif cmd == "PAUSE":
                running = False
                release_all_keys()
            elif cmd == "RUN":
                running = True
                if PRESS_E_ON_START:
                    need_press_e_on_start = True
    except Exception:
        pass

def main():
    global running, need_press_e_on_start

    args = parse_args()

    # проверим ассеты (в exe часто сразу видно что не найдено)
    for p in (TEMPLATE_PATH, DONE_TEMPLATE_PATH):
        if not os.path.exists(p):
            print("ASSET NOT FOUND:", p)

    # autorun: сразу включаем running
    if args.autorun:
        running = True
        if PRESS_E_ON_START:
            need_press_e_on_start = True

    # stdin control
    if args.control_stdin:
        t = threading.Thread(target=_stdin_control_thread, daemon=True)
        t.start()
        print("STDIN CONTROL: ON (RUN/PAUSE/STOP)")
    else:
        print("STDIN CONTROL: OFF")

    last_pos = None

    anchor_pos = None
    stuck_start = None
    stuck_logged = False

    last_click_pos = None
    same_click_count = 0

    prompt_template = None
    prompt_seen = False
    last_e_time = 0.0

    done_template = None
    done_seen = False
    last_done_time = 0.0

    BASE_SCALE = 1.0
    SCALES = (1.0,)

    W_WAIT_PROMPT_MAX_SEC = float(globals().get("W_WAIT_PROMPT_MAX_SEC", 3.0))
    W_WAIT_PROMPT_POLL_SEC = float(globals().get("W_WAIT_PROMPT_POLL_SEC", 0.03))

    def keep_running():
        return not STOP_EVENT.is_set()

    def _hold_w_until_prompt_or_timeout(sct, prompt_template, PROMPT_ROI, threshold, scales) -> bool:
        kb.press(KEY_W)
        try:
            deadline = time.time() + W_WAIT_PROMPT_MAX_SEC
            while True:
                if STOP_EVENT.is_set() or (not running):
                    return False
                if time.time() >= deadline:
                    return False
                if prompt_template is not None:
                    if template_visible(sct, prompt_template, PROMPT_ROI, threshold, scales):
                        return True
                time.sleep(W_WAIT_PROMPT_POLL_SEC)
        finally:
            try:
                kb.release(KEY_W)
            except Exception:
                pass

    with mss.mss() as sct:
        # безопасный выбор монитора
        idx = int(args.monitor)
        if idx < 0 or idx >= len(sct.monitors):
            idx = 1 if len(sct.monitors) > 1 else 0

        monitor = sct.monitors[idx]
        L, T = monitor["left"], monitor["top"]
        W, H = monitor["width"], monitor["height"]

        # --- Центральный ROI для мини-квеста ---
        if LIMIT_SEARCH_TO_CENTER:
            roi_w = int(W * CENTER_ROI_W_FRAC)
            roi_h = int(H * CENTER_ROI_H_FRAC)
            roi_left = L + (W - roi_w) // 2
            roi_top = T + (H - roi_h) // 2
        else:
            roi_left, roi_top, roi_w, roi_h = L, T, W, H

        SEARCH_ROI = {"left": roi_left, "top": roi_top, "width": roi_w, "height": roi_h}

        # ===== AUTO SCALE (2K -> FHD и т.д.) =====
        try:
            BASE_SCALE = min(W / float(REF_W), H / float(REF_H))
            if not (0.30 <= BASE_SCALE <= 2.50):
                BASE_SCALE = 1.0
        except Exception:
            BASE_SCALE = 1.0

        SCALES = _build_scales(BASE_SCALE)

        # ROI подсказки E: локальный ROI масштабируем под текущий монитор
        scaled_prompt_local = _scale_roi_local(PROMPT_ROI_LOCAL, BASE_SCALE)
        PROMPT_ROI = {
            "left": L + scaled_prompt_local["left"],
            "top":  T + scaled_prompt_local["top"],
            "width": scaled_prompt_local["width"],
            "height": scaled_prompt_local["height"],
        }

        DONE_ROI_LOCAL = make_done_roi(W, H)
        DONE_ROI = {
            "left": L + DONE_ROI_LOCAL["left"],
            "top":  T + DONE_ROI_LOCAL["top"],
            "width": DONE_ROI_LOCAL["width"],
            "height": DONE_ROI_LOCAL["height"],
        }

        print("MONITOR IDX:", idx, "RECT:", {"L": L, "T": T, "W": W, "H": H})
        print("SEARCH_ROI:", SEARCH_ROI)
        print("BASE_SCALE:", round(BASE_SCALE, 3), "SCALES:", SCALES)
        print("PROMPT_ROI:", PROMPT_ROI)
        print("DONE_ROI:", DONE_ROI)

        if ENABLE_E_PROMPT:
            try:
                prompt_template = load_template_gray(TEMPLATE_PATH)
                print("Шаблон подсказки E загружен:", TEMPLATE_PATH)
            except Exception as e:
                prompt_template = None
                print("Не удалось загрузить TEMPLATE_PATH:", e)

        if ENABLE_DONE_NOTIF:
            try:
                done_template = load_template_gray(DONE_TEMPLATE_PATH)
                print("Шаблон уведомления 'собрали' загружен:", DONE_TEMPLATE_PATH)
            except Exception as e:
                done_template = None
                print("Не удалось загрузить DONE_TEMPLATE_PATH:", e)

        try:
            while keep_running():
                if not running:
                    time.sleep(0.05)
                    anchor_pos = None
                    stuck_start = None
                    stuck_logged = False
                    prompt_seen = False
                    done_seen = False
                    need_press_e_on_start = False
                    clear_banned()
                    reset_same_click_counter()
                    continue

                if need_press_e_on_start:
                    time.sleep(START_E_DELAY_SEC)
                    press_e_once()
                    need_press_e_on_start = False
                    time.sleep(0.05)

                now = time.time()

                # ---------------- DONE логика ----------------
                if ENABLE_DONE_NOTIF and done_template is not None:
                    visible_done = template_visible(sct, done_template, DONE_ROI, DONE_MATCH_THRESHOLD, SCALES)

                    if visible_done and (not done_seen) and (now - last_done_time >= DONE_DEBOUNCE_SEC):
                        hold_s_with_centering(sct, L, T, W, H, HOLD_S_SEC)
                        if STOP_EVENT.is_set() or (not running):
                            release_all_keys()
                            continue

                        found_prompt = _hold_w_until_prompt_or_timeout(
                            sct, prompt_template, PROMPT_ROI, MATCH_THRESHOLD, SCALES
                        )
                        if STOP_EVENT.is_set() or (not running):
                            release_all_keys()
                            continue

                        if found_prompt:
                            now_e = time.time()
                            if now_e - last_e_time >= E_DEBOUNCE_SEC:
                                press_e_once()
                                last_e_time = now_e
                                prompt_seen = True

                        last_done_time = time.time()
                        done_seen = True

                        last_pos = None
                        anchor_pos = None
                        stuck_start = None
                        stuck_logged = False
                        reset_same_click_counter()

                        time.sleep(0.05)
                        continue
                    elif not visible_done:
                        done_seen = False

                # ---------------- prompt E логика (обычная) ----------------
                if ENABLE_E_PROMPT and prompt_template is not None:
                    visible_prompt = template_visible(sct, prompt_template, PROMPT_ROI, MATCH_THRESHOLD, SCALES)
                    now2 = time.time()

                    if visible_prompt and (not prompt_seen) and (now2 - last_e_time >= E_DEBOUNCE_SEC):
                        press_e_once()
                        last_e_time = now2
                        prompt_seen = True
                    elif not visible_prompt:
                        prompt_seen = False

                found = None

                # локальный поиск около последней точки
                if last_pos is not None:
                    lx, ly = last_pos
                    left = max(L, lx - LOCAL_RADIUS)
                    top = max(T, ly - LOCAL_RADIUS)
                    right = min(L + W, lx + LOCAL_RADIUS)
                    bottom = min(T + H, ly + LOCAL_RADIUS)

                    if right > left and bottom > top:
                        img_local = grab_region_rgb(sct, left, top, right - left, bottom - top)
                        p = find_color_in_image(img_local, step=1)
                        if p is not None:
                            found = (left + p[0], top + p[1])

                # глобальный поиск по ROI
                if found is None:
                    sr = SEARCH_ROI
                    img_full = grab_region_rgb(sct, sr["left"], sr["top"], sr["width"], sr["height"])
                    p = find_color_in_image(img_full, step=COARSE_STEP)
                    if p is not None:
                        gx, gy = p
                        left = max(0, gx - COARSE_STEP * 2)
                        top = max(0, gy - COARSE_STEP * 2)
                        right = min(sr["width"], gx + COARSE_STEP * 2)
                        bottom = min(sr["height"], gy + COARSE_STEP * 2)

                        img_ref = img_full[top:bottom, left:right, :]
                        pr = find_color_in_image(img_ref, step=1)
                        if pr is not None:
                            found = (sr["left"] + left + pr[0], sr["top"] + top + pr[1])
                        else:
                            found = (sr["left"] + gx, sr["top"] + gy)

                if found is not None and is_in_banned(found):
                    reset_same_click_counter()
                    time.sleep(CLICK_INTERVAL)
                    continue

                if found is not None:
                    if last_click_pos is not None and _dist2(found, last_click_pos) <= (
                        SAME_POINT_RADIUS_PX * SAME_POINT_RADIUS_PX
                    ):
                        same_click_count += 1
                    else:
                        same_click_count = 0
                        last_click_pos = found

                    if same_click_count >= SAME_POINT_MAX_CLICKS:
                        add_banned(found)
                        if NUDGE_AFTER_SAME:
                            nudge_mouse()
                        same_click_count = 0
                        time.sleep(CLICK_INTERVAL)
                        continue

                    mouse.position = found
                    mouse.click(Button.left, 1)

                    register_click(found)
                    last_pos = None

                    now3 = time.time()
                    if anchor_pos is None:
                        anchor_pos = found
                        stuck_start = now3
                        stuck_logged = False
                    else:
                        if within_radius(found, anchor_pos, STUCK_RADIUS_PX):
                            stuck_for = now3 - (stuck_start or now3)
                            if (stuck_for >= STUCK_SECONDS) and (not stuck_logged):
                                rgb = get_pixel_rgb(sct, found[0], found[1])
                                append_stuck_log(LOG_PATH, anchor_pos[0], anchor_pos[1], rgb, stuck_for)
                                stuck_logged = True
                        else:
                            anchor_pos = found
                            stuck_start = now3
                            stuck_logged = False

                time.sleep(CLICK_INTERVAL)

        finally:
            release_all_keys()

if __name__ == "__main__":
    main()