import os
import sys
import time
import threading
import random
import math
import argparse

import cv2
import numpy as np
import mss

from pynput import keyboard
from pynput.keyboard import Controller as KeyboardController, KeyCode
from pynput.mouse import Controller as MouseController, Button

# ==================== DPI AWARE (Windows) ====================
if os.name == "nt":
    import ctypes

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
    except (OSError, ctypes.ArgumentError, TypeError):
        pass

def app_dir() -> str:
    # PyInstaller onefile/onedir: prefer extracted bundle dir
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ==================== НАСТРОЙКИ ====================
TOGGLE_KEY = keyboard.Key.f7
EXIT_KEY = keyboard.Key.esc

# общий порог
MATCH_THRESHOLD_DEFAULT = 0.68
# отдельный порог для Settings (часто матчится хуже на FullHD/другом HUD)
MATCH_THRESHOLD_SETTINGS = 0.62

SEARCH_TIMEOUT = 6.0

# --- референсное разрешение, под которое делались шаблоны ---
REF_W, REF_H = 2560, 1440

# SCALES будет вычисляться автоматически при старте worker_loop()
SCALES = (1.0,)

# подтверждение (2 раза подряд почти в том же месте)
HITS_REQUIRED = 2
HIT_POS_TOLERANCE_PX = 8

SCROLL_DURATION_SEC = 1.3
SCROLL_STEP = -3
SCROLL_TICK = 0.03

WAIT_BASE_SEC = 300.0  # 5 минут
WAIT_JITTER = 0.25     # ±25%

# --- РАНДОМ WASD ---
WASD_HOLD_BASE = 0.50         # базовое удержание
WASD_HOLD_JITTER = 0.35       # ±35% к удержанию
WASD_GAP_RANGE = (0.03, 0.12) # пауза между нажатиями

ESC_DELAY_RANGE = (0.9, 5.3)

ARROW_UP_PROB = 0.50
_arrow_state = {"last": None, "streak": 0}

# ==================== INPUT ====================
kb = KeyboardController()
mouse = MouseController()

VK_A = 0x41
VK_D = 0x44
VK_W = 0x57
VK_S = 0x53
VK_DOWN = 0x28
VK_UP = 0x26
VK_ESC = 0x1B

IGNORE_ESC_UNTIL = 0.0

RUN_EVENT = threading.Event()
STOP_ALL = threading.Event()

WORKER_THREAD = None

# ==================== ARGS ====================
def parse_args():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--autorun", action="store_true", help="Запустить сразу")
    p.add_argument("--no-hotkey", action="store_true", help="Не вешать F7/ESC listener")
    p.add_argument("--control-stdin", action="store_true", help="Слушать stdin команды (STOP)")
    p.add_argument("--assets-dir", type=str, default="", help="Папка assets (по умолчанию рядом со скриптом)")
    return p.parse_args()

# ==================== HELPERS ====================
def key_vk(vk: int):
    return KeyCode.from_vk(vk)

def sleep_cancel(sec: float) -> bool:
    end = time.time() + float(sec)
    while time.time() < end:
        if (not RUN_EVENT.is_set()) or STOP_ALL.is_set():
            return False
        time.sleep(0.02)
    return True

def tap_vk(vk: int, hold: float = 0.0):
    global IGNORE_ESC_UNTIL
    if vk == VK_ESC:
        IGNORE_ESC_UNTIL = time.time() + 0.8

    k = key_vk(vk)
    kb.press(k)
    try:
        if hold and hold > 0:
            sleep_cancel(hold)
    finally:
        try:
            kb.release(k)
        except Exception:
            pass

def release_wasd():
    for vk in (VK_W, VK_A, VK_S, VK_D, VK_UP, VK_DOWN):
        try:
            kb.release(key_vk(vk))
        except Exception:
            pass

def load_template_gray(path: str):
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Не найден шаблон: {path}")
    return img

def grab_screen_gray(sct):
    mon = sct.monitors[0]  # виртуальный экран (все мониторы)
    img = np.array(sct.grab(mon), dtype=np.uint8)
    gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)
    return gray, mon["left"], mon["top"]

def _build_scales(scale: float) -> tuple:
    lo = max(0.30, scale - 0.35)
    hi = min(2.50, scale + 0.35)

    vals = []
    s = lo
    step = 0.04
    while s <= hi + 1e-9:
        vals.append(round(s, 2))
        s += step

    sc = round(scale, 2)
    if sc not in vals:
        vals.append(sc)
        vals = sorted(set(vals))

    return tuple(vals)

def find_template_center(sct, template_gray, threshold: float):
    start = time.time()
    last_ok = None  # (cx, cy)

    while (time.time() - start < SEARCH_TIMEOUT) and RUN_EVENT.is_set() and (not STOP_ALL.is_set()):
        gray, off_x, off_y = grab_screen_gray(sct)

        best = None
        best_scale = 1.0

        for scale in SCALES:
            if scale == 1.0:
                templ = template_gray
            else:
                h0, w0 = template_gray.shape
                nw = max(8, int(w0 * scale))
                nh = max(8, int(h0 * scale))
                interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                templ = cv2.resize(template_gray, (nw, nh), interpolation=interp)

            h, w = templ.shape
            if h > gray.shape[0] or w > gray.shape[1]:
                continue

            res = cv2.matchTemplate(gray, templ, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if best is None or max_val > best[0]:
                best = (max_val, max_loc, w, h)
                best_scale = scale

        if best is not None:
            score, (x, y), w, h = best
            cx = off_x + x + w // 2
            cy = off_y + y + h // 2

            if score >= threshold:
                if HITS_REQUIRED <= 1:
                    return cx, cy

                if last_ok is None:
                    last_ok = (cx, cy)
                else:
                    lx, ly = last_ok
                    if abs(cx - lx) <= HIT_POS_TOLERANCE_PX and abs(cy - ly) <= HIT_POS_TOLERANCE_PX:
                        return cx, cy
                    last_ok = (cx, cy)
            else:
                last_ok = None

        sleep_cancel(0.05)

    return None

def find_template_only(sct, template_gray, threshold: float) -> bool:
    return bool(find_template_center(sct, template_gray, threshold))

def move_mouse_smooth(x, y):
    sx, sy = mouse.position
    dx = x - sx
    dy = y - sy
    dist = math.hypot(dx, dy)

    duration = 0.15 + min(0.4, dist / 2500)
    duration *= random.uniform(0.85, 1.15)
    steps = max(12, min(120, int(dist / 18)))

    for i in range(1, steps + 1):
        if not RUN_EVENT.is_set() or STOP_ALL.is_set():
            return
        t = i / steps
        t2 = t * t * (3 - 2 * t)  # smoothstep
        nx = int(sx + dx * t2)
        ny = int(sy + dy * t2)
        mouse.position = (nx, ny)
        sleep_cancel(duration / steps)

def click_left(x, y):
    move_mouse_smooth(x, y)
    if not RUN_EVENT.is_set() or STOP_ALL.is_set():
        return
    sleep_cancel(random.uniform(0.015, 0.035))
    try:
        mouse.click(Button.left, 1)
    except Exception:
        pass

def scroll_down_for(sec: float):
    t0 = time.time()
    while (time.time() - t0 < sec) and RUN_EVENT.is_set() and (not STOP_ALL.is_set()):
        try:
            mouse.scroll(0, SCROLL_STEP)
        except Exception:
            pass
        sleep_cancel(SCROLL_TICK)

def find_and_click(sct, template_gray, threshold: float):
    pos = find_template_center(sct, template_gray, threshold)
    if not pos:
        return False
    click_left(*pos)
    return True

def find_and_click_random(sct, templates: list, threshold: float):
    order = templates[:]
    random.shuffle(order)

    for templ in order:
        if not RUN_EVENT.is_set() or STOP_ALL.is_set():
            return False
        if find_and_click(sct, templ, threshold):
            return True
    return False

def rand_hold(base: float, jitter: float) -> float:
    lo = base * (1.0 - jitter)
    hi = base * (1.0 + jitter)
    return random.uniform(max(0.01, lo), max(0.02, hi))

def do_random_wasd_sequence():
    count = random.randint(1, 4)

    keys = [VK_W, VK_A, VK_S, VK_D]
    chosen = random.sample(keys, k=count)
    random.shuffle(chosen)

    for vk in chosen:
        if not RUN_EVENT.is_set() or STOP_ALL.is_set():
            return
        tap_vk(vk, rand_hold(WASD_HOLD_BASE, WASD_HOLD_JITTER))
        if not sleep_cancel(random.uniform(*WASD_GAP_RANGE)):
            return

# ==================== CYCLE ====================
def choose_arrow_direction() -> str:
    last = _arrow_state["last"]
    streak = int(_arrow_state["streak"] or 0)

    direction = "up" if (random.random() < ARROW_UP_PROB) else "down"

    if last == direction and streak >= 2:
        direction = "down" if direction == "up" else "up"
        streak = 0

    if direction == last:
        _arrow_state["streak"] = streak + 1
    else:
        _arrow_state["last"] = direction
        _arrow_state["streak"] = 1

    return direction

def do_one_cycle(sct, t_power, t_market, t_cars, t_cars2, t_items, t_items2, t_up_lock, t_settings, t_wallpaper,  t_esc):
    do_random_wasd_sequence()
    if not (RUN_EVENT.is_set() and not STOP_ALL.is_set()):
        return

    direction = choose_arrow_direction()
    go_up = (direction == "up")

    if go_up:
        tap_vk(VK_UP, 0.05)
        if not (RUN_EVENT.is_set() and not STOP_ALL.is_set()):
            return

        if not sleep_cancel(random.uniform(0.15, 0.35)):
            return

        # Up_lock (обычный порог)
        if not find_and_click(sct, t_up_lock, MATCH_THRESHOLD_DEFAULT):
            return
        if not sleep_cancel(0.25):
            return

        # Settings (пониженный порог + подтверждение 2 раза)
        if not find_and_click(sct, t_settings, MATCH_THRESHOLD_SETTINGS):
            return
        if not sleep_cancel(0.25):
            return

        # Wallpaper (обычный порог)
        if not find_and_click(sct, t_wallpaper, MATCH_THRESHOLD_DEFAULT):
            return
        if not sleep_cancel(0.25):
            return

        tap_vk(VK_UP, 0.05)
        return

    tap_vk(VK_DOWN, 0.05)
    if not (RUN_EVENT.is_set() and not STOP_ALL.is_set()):
        return

    if not find_and_click(sct, t_power, MATCH_THRESHOLD_DEFAULT):
        return
    if not sleep_cancel(0.25):
        return

    if not find_and_click(sct, t_market, MATCH_THRESHOLD_DEFAULT):
        return
    if not sleep_cancel(0.25):
        return

    if find_template_only(sct, t_esc, MATCH_THRESHOLD_DEFAULT):
        tap_vk(VK_ESC)
        if not sleep_cancel(0.25):
            return

    scroll_down_for(SCROLL_DURATION_SEC)
    if not sleep_cancel(0.25):
        return

    if not find_and_click_random(sct, [t_cars, t_cars2], MATCH_THRESHOLD_DEFAULT):
        return
    if not sleep_cancel(0.25):
        return

    if not find_and_click_random(sct, [t_items, t_items2], MATCH_THRESHOLD_DEFAULT):
        return
    if not sleep_cancel(0.25):
        return

    if not sleep_cancel(random.uniform(*ESC_DELAY_RANGE)):
        return
    tap_vk(VK_ESC)
    if not sleep_cancel(0.12):
        return
    tap_vk(VK_ESC)

def worker_loop(assets_dir):
    global SCALES

    t_power = load_template_gray(os.path.join(assets_dir, "Power.jpg"))
    t_market   = load_template_gray(os.path.join(assets_dir, "MarketPlace.jpg"))
    t_esc = load_template_gray(os.path.join(assets_dir, "ESC.jpg"))
    t_cars     = load_template_gray(os.path.join(assets_dir, "cars.jpg"))
    t_cars2    = load_template_gray(os.path.join(assets_dir, "cars2.jpg"))
    t_items    = load_template_gray(os.path.join(assets_dir, "Items.jpg"))
    t_items2   = load_template_gray(os.path.join(assets_dir, "Items2.jpg"))

    t_up_lock   = load_template_gray(os.path.join(assets_dir, "Up_lock.jpg"))
    t_settings  = load_template_gray(os.path.join(assets_dir, "Settings.jpg"))
    t_wallpaper = load_template_gray(os.path.join(assets_dir, "Wallpaper.jpg"))

    try:
        with mss.mss() as sct:
            mon = sct.monitors[0]
            cur_w, cur_h = mon["width"], mon["height"]

            base_scale = min(cur_w / REF_W, cur_h / REF_H)
            if not (0.30 <= base_scale <= 2.50):
                base_scale = 1.0

            SCALES = _build_scales(base_scale)

            while RUN_EVENT.is_set() and (not STOP_ALL.is_set()):
                do_one_cycle(
                    sct,
                    t_power, t_market, t_cars, t_cars2, t_items, t_items2,
                    t_up_lock, t_settings, t_wallpaper, t_esc)

                wait_sec = random.uniform(
                    WAIT_BASE_SEC * (1 - WAIT_JITTER),
                    WAIT_BASE_SEC * (1 + WAIT_JITTER))

                t0 = time.time()
                while RUN_EVENT.is_set() and (not STOP_ALL.is_set()) and (time.time() - t0 < wait_sec):
                    sleep_cancel(0.2)
    finally:
        release_wasd()

# ==================== CONTROL ====================
def start_worker(assets_dir):
    global WORKER_THREAD
    RUN_EVENT.set()
    if WORKER_THREAD is None or not WORKER_THREAD.is_alive():
        WORKER_THREAD = threading.Thread(target=worker_loop, args=(assets_dir,), daemon=True)
        WORKER_THREAD.start()

def stop_worker():
    RUN_EVENT.clear()
    release_wasd()

def stdin_control_loop():
    try:
        while not STOP_ALL.is_set():
            line = sys.stdin.readline()
            if not line:
                break

            if line.strip().upper() == "STOP":
                stop_worker()
                STOP_ALL.set()
                break

    except (OSError, ValueError, RuntimeError):
        pass
    finally:
        stop_worker()
        STOP_ALL.set()

# ==================== LISTENER ====================
def on_press(key, assets_dir):
    if key == TOGGLE_KEY:
        if RUN_EVENT.is_set():
            stop_worker()
        else:
            start_worker(assets_dir)
        return None

    if key == EXIT_KEY:
        if time.time() < IGNORE_ESC_UNTIL:
            return None
        stop_worker()
        STOP_ALL.set()
        return False

    return None

def main():
    args = parse_args()
    base_dir = app_dir()

    assets_dir = (args.assets_dir or "").strip()
    if not assets_dir:
        assets_dir = os.path.join(base_dir, "afk", "assets")

    if args.autorun:
        start_worker(assets_dir)

    if args.control_stdin:
        threading.Thread(target=stdin_control_loop, daemon=True).start()

    if args.no_hotkey:
        while not STOP_ALL.is_set():
            sleep_cancel(0.2)
        return

    listener = keyboard.Listener(on_press=lambda k: on_press(k, assets_dir))
    listener.start()
    listener.join()

if __name__ == "__main__":
    main()