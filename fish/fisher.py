# fish/fisher.py
from __future__ import annotations

import os
import sys
import time
import threading
import logging
import ctypes
from ctypes import wintypes

import numpy as np
import mss
import cv2

# pynput есть у тебя в проекте (core_app.py уже импортирует его)
from pynput import keyboard as pynput_keyboard

# ===================== LOG =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("FISHER")

# ===================== PATHS =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

def asset(name: str) -> str:
    return os.path.join(ASSETS_DIR, name)

# ===================== STOP =====================
STOP_EVENT = threading.Event()

def request_stop(reason: str = ""):
    STOP_EVENT.set()
    release_ad(f"STOP {reason}".strip())
    msg = f"STOP → {reason}".strip() if reason else "STOP"
    log.info(msg)

def check_stop():
    if STOP_EVENT.is_set():
        raise SystemExit

# ===================== STDIN STOP =====================
def _stdin_stop_watcher():
    # ждём строку STOP\n
    try:
        for line in sys.stdin:
            if (line or "").strip().upper() == "STOP":
                request_stop("stdin")
                return
    except Exception:
        pass

# ===================== WINAPI SendInput (keyboard+mouse) =====================
user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0

KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

# scancodes (US layout; для A/D/Space/E это стабильные scancode)
SC_SPACE = 0x39
SC_A = 0x1E
SC_D = 0x20
SC_E = 0x12

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

def _send_input(inp: INPUT):
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    if n != 1:
        # не падаем, просто лог
        err = ctypes.get_last_error()
        log.debug("SendInput failed: %s", err)

def send_scancode(scan_code: int, hold_sec: float = 0.02):
    extra = ctypes.c_ulong(0)

    down = INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=KEYBDINPUT(0, scan_code, KEYEVENTF_SCANCODE, 0, ctypes.pointer(extra))),
    )
    up = INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=KEYBDINPUT(0, scan_code, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))),
    )
    _send_input(down)
    time.sleep(hold_sec)
    _send_input(up)

def send_scancode_down(scan_code: int):
    extra = ctypes.c_ulong(0)
    down = INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=KEYBDINPUT(0, scan_code, KEYEVENTF_SCANCODE, 0, ctypes.pointer(extra))),
    )
    _send_input(down)

def send_scancode_up(scan_code: int):
    extra = ctypes.c_ulong(0)
    up = INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=KEYBDINPUT(0, scan_code, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))),
    )
    _send_input(up)

def _to_absolute(x: int, y: int) -> tuple[int, int]:
    # абсолютные координаты 0..65535
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    ax = int(x * 65535 / max(1, sw - 1))
    ay = int(y * 65535 / max(1, sh - 1))
    return ax, ay

def mouse_move_to(x: int, y: int):
    extra = ctypes.c_ulong(0)
    ax, ay = _to_absolute(x, y)
    mi = MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, ctypes.pointer(extra))
    inp = INPUT(type=INPUT_MOUSE, u=_INPUT_UNION(mi=mi))
    _send_input(inp)

def mouse_click_left():
    extra = ctypes.c_ulong(0)
    down = INPUT(type=INPUT_MOUSE, u=_INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, ctypes.pointer(extra))))
    up   = INPUT(type=INPUT_MOUSE, u=_INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, ctypes.pointer(extra))))
    _send_input(down)
    time.sleep(0.01)
    _send_input(up)

def mouse_move_smooth(x: int, y: int, steps: int = 10, dur: float = 0.06):
    # простой плавный ход (без pydirectinput)
    try:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        x0, y0 = int(pt.x), int(pt.y)
    except Exception:
        x0, y0 = x, y

    for i in range(1, steps + 1):
        t = i / steps
        xi = int(x0 + (x - x0) * t)
        yi = int(y0 + (y - y0) * t)
        mouse_move_to(xi, yi)
        time.sleep(dur / steps)


# ===================== PRESS HELPERS =====================
SPACE_HOLD_SEC = 0.06

def press_space():
    check_stop()
    send_scancode(SC_SPACE, hold_sec=SPACE_HOLD_SEC)

def press_e():
    check_stop()
    send_scancode(SC_E, hold_sec=0.03)


# ===================== CV HELPERS =====================
def load_template(path: str):
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Не найден файл: {path}")
    return img

def match_template(gray, tpl):
    res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return max_val, max_loc

def match_template_multiscale(gray, tpl, scales=(0.80, 0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15)):
    best_score = -1.0
    best_loc = (0, 0)
    best_wh = (tpl.shape[1], tpl.shape[0])

    th, tw = tpl.shape[:2]
    for s in scales:
        check_stop()
        w = int(tw * s)
        h = int(th * s)
        if w < 10 or h < 10:
            continue
        if w > gray.shape[1] or h > gray.shape[0]:
            continue
        resized = cv2.resize(tpl, (w, h), interpolation=cv2.INTER_AREA)
        score, loc = match_template(gray, resized)
        if score > best_score:
            best_score = score
            best_loc = loc
            best_wh = (w, h)

    return best_score, best_loc, best_wh[0], best_wh[1]


# ===================== STAGE 1 (GREEN BAR) =====================
ROI_W = 900
ROI_H = 90
ROI_Y_OFFSET_FROM_BOTTOM = 190

WAIT_BAR_SEC = 7.0
CATCH_TIMEOUT_SEC = 5.0
MAX_ATTEMPTS = 3
GREEN_CONFIRM_FRAMES = 6
MARGIN = 6

GREEN_LO = (35, 80, 80)
GREEN_HI = (90, 255, 255)
WHITE_LO = (0, 0, 170)
WHITE_HI = (180, 70, 255)

def green_range_fast(hsv):
    mask = cv2.inRange(hsv, GREEN_LO, GREEN_HI)
    col = mask.sum(axis=0)
    thr = 255 * (hsv.shape[0] // 6)
    idx = np.where(col > thr)[0]
    if idx.size == 0:
        return None
    return int(idx[0]), int(idx[-1])

def marker_x_fast(hsv):
    mask = cv2.inRange(hsv, WHITE_LO, WHITE_HI)
    col = mask.sum(axis=0)
    thr = 255 * (hsv.shape[0] // 10)
    x = int(np.argmax(col))
    if col[x] < thr:
        return None
    return x

def wait_for_bar(sct, region, wait_sec: float):
    start = time.perf_counter()
    seen = 0
    while time.perf_counter() - start < wait_sec:
        check_stop()
        img = np.array(sct.grab(region))
        hsv = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2HSV)
        if green_range_fast(hsv):
            seen += 1
            if seen >= GREEN_CONFIRM_FRAMES:
                return True
        else:
            seen = 0
        time.sleep(0.002)
    return False

def catch_green_once(sct, region):
    start = time.perf_counter()
    while time.perf_counter() - start < CATCH_TIMEOUT_SEC:
        check_stop()
        img = np.array(sct.grab(region))
        hsv = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2HSV)
        g = green_range_fast(hsv)
        mx = marker_x_fast(hsv)
        if g and mx is not None:
            g1, g2 = g
            if g1 + MARGIN <= mx <= g2 - MARGIN:
                log.info("Стадия 1: маркер в зелёной → SPACE")
                press_space()
                return True
        time.sleep(0.001)
    return False


# ===================== STAGE 2 (BUBBLES) =====================
WAIT_BUBBLES_SEC = 35.0

SECTOR_RIGHT_FROM = 0.62
SECTOR_RIGHT_TO = 0.97
SECTOR_BOTTOM_FROM = 0.55
SECTOR_BOTTOM_TO = 0.93

BOBBER_THR = 0.72
BUBBLES_THR = 0.78
CONFIRM_FRAMES = 2
BUBBLE_SEARCH_TOP_RATIO = 0.75
STAGE2_SLEEP = 0.01

TPL_BOBBER = load_template(asset("bobber_off.png"))
TPL_BUBBLES = load_template(asset("bubbles.png"))
TPL_FISH_ICON = load_template(asset("fish_icon.png"))
TPL_RELEASE = load_template(asset("release_btn.png"))

def wait_for_bubbles(sct, region_search):
    start = time.perf_counter()
    ok = 0
    best_bubbles = 0.0
    last_report = time.perf_counter()

    while time.perf_counter() - start < WAIT_BUBBLES_SEC:
        check_stop()
        frame = np.array(sct.grab(region_search))[:, :, :3]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        bobber_score, bobber_loc = match_template(gray, TPL_BOBBER)

        if bobber_score >= BOBBER_THR:
            x, y = bobber_loc
            h_b, w_b = TPL_BOBBER.shape[:2]

            x1 = max(0, x)
            y1 = max(0, y)
            x2 = min(gray.shape[1], x + w_b)
            y2 = min(gray.shape[0], y + h_b)

            y_cut = int(y1 + (y2 - y1) * BUBBLE_SEARCH_TOP_RATIO)
            roi = gray[y1:y_cut, x1:x2]

            if roi.size > 0 and roi.shape[0] >= TPL_BUBBLES.shape[0] and roi.shape[1] >= TPL_BUBBLES.shape[1]:
                bub_score, _ = match_template(roi, TPL_BUBBLES)
                best_bubbles = max(best_bubbles, bub_score)

                if bub_score >= BUBBLES_THR:
                    ok += 1
                    if ok >= CONFIRM_FRAMES:
                        log.info(f"Стадия 2: ПУЗЫРЬКИ (bubbles={bub_score:.3f}) → SPACE")
                        press_space()
                        return True
                else:
                    ok = 0
            else:
                ok = 0
        else:
            ok = 0

        now = time.perf_counter()
        if now - last_report >= 1.0:
            last_report = now
            log.info(f"Стадия 2: best_bubbles={best_bubbles:.3f} (thr={BUBBLES_THR})")

        time.sleep(STAGE2_SLEEP)

    log.warning(f"Стадия 2: таймаут. best_bubbles={best_bubbles:.3f}")
    return False


# ===================== STAGE 4 (RELEASE BUTTON) =====================
WAIT_RELEASE_MENU_SEC = 12.0
RELEASE_THR = 0.62
RELEASE_CONFIRM_FRAMES = 2

RELEASE_ZONE_LEFT = 0.25
RELEASE_ZONE_RIGHT = 0.75
RELEASE_ZONE_TOP = 0.55
RELEASE_ZONE_BOTTOM = 0.90

def find_release_button_once(sct, sw, sh, off_x: int, off_y: int):
    region = {
        "left": off_x + int(sw * RELEASE_ZONE_LEFT),
        "top":  off_y + int(sh * RELEASE_ZONE_TOP),
        "width": max(1, int(sw * (RELEASE_ZONE_RIGHT - RELEASE_ZONE_LEFT))),
        "height": max(1, int(sh * (RELEASE_ZONE_BOTTOM - RELEASE_ZONE_TOP))),
    }

    frame = np.array(sct.grab(region))[:, :, :3]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    score, loc, tw, th = match_template_multiscale(gray, TPL_RELEASE)
    x, y = loc
    cx = region["left"] + x + tw // 2
    cy = region["top"] + y + th // 2
    return (score >= RELEASE_THR), score, cx, cy

def stage4_click_release(sct, sw, sh, off_x: int, off_y: int):
    log.info('Стадия 4: жду кнопку "Отпустить"...')
    ok = 0
    best = 0.0
    start = time.perf_counter()

    while time.perf_counter() - start < WAIT_RELEASE_MENU_SEC:
        check_stop()
        found, score, cx, cy = find_release_button_once(sct, sw, sh, off_x, off_y)
        best = max(best, score)

        if found:
            ok += 1
            if ok >= RELEASE_CONFIRM_FRAMES:
                log.info(f'Стадия 4: "Отпустить" найдено score={score:.3f} → click ({cx},{cy})')
                mouse_move_smooth(cx, cy, steps=12, dur=0.08)
                time.sleep(0.02)
                mouse_click_left()
                return True
        else:
            ok = 0

        time.sleep(0.02)

    log.warning(f'Стадия 4: не нашёл кнопку "Отпустить" (best={best:.3f}, thr={RELEASE_THR})')
    return False

# ===================== STAGE 3 (FISH: OPTICAL FLOW) =====================
FISH_MAX_SEC = 120.0

FISH_REGION_W = 1600
FISH_REGION_H = 900
FISH_REGION_X_OFFSET = 0
FISH_REGION_Y_OFFSET = -50

FISH_SEARCH_EVERY = 0.03

FLOW_DIR_THR = 0.35
FLOW_MAG_THR = 0.8

SWITCH_COOLDOWN = 0.10
SWITCH_CONFIRM_FRAMES = 2
FISH_LOG_EVERY_SEC = 0.25

FISH_ICON_TOP = 0.00
FISH_ICON_H = 0.22
FISH_ICON_LEFT = 0.00
FISH_ICON_W = 0.75

FISH_ICON_THR_ON = 0.58
FISH_ICON_THR_OFF = 0.52
FISH_ICON_LOST_FRAMES = 90

_ad_state = "none"

def hold_a(reason: str = ""):
    global _ad_state
    if _ad_state == "a":
        return
    _ad_state = "a"
    send_scancode_up(SC_D)
    send_scancode_down(SC_A)
    log.info(f"FISH → HOLD A {reason}".strip())

def hold_d(reason: str = ""):
    global _ad_state
    if _ad_state == "d":
        return
    _ad_state = "d"
    send_scancode_up(SC_A)
    send_scancode_down(SC_D)
    log.info(f"FISH → HOLD D {reason}".strip())

def release_ad(reason: str = ""):
    global _ad_state
    if _ad_state == "none":
        return
    _ad_state = "none"
    send_scancode_up(SC_A)
    send_scancode_up(SC_D)
    log.info(f"FISH → RELEASE A/D {reason}".strip())

def fish_icon_score(sct, sw, sh, off_x: int, off_y: int):
    region = {
        "left": off_x + int(sw * FISH_ICON_LEFT),
        "top":  off_y + int(sh * FISH_ICON_TOP),
        "width": max(1, int(sw * FISH_ICON_W)),
        "height": max(1, int(sh * FISH_ICON_H)),
    }
    frame = np.array(sct.grab(region))[:, :, :3]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    score, _ = match_template(gray, TPL_FISH_ICON)
    return score

def stage3_follow_fish_until_menu(sct, sw, sh, off_x: int, off_y: int):
    log.info("Стадия 3: веду рыбу до появления меню (кнопки Отпустить)...")

    cx = off_x + sw // 2 + FISH_REGION_X_OFFSET
    cy = off_y + sh // 2 + FISH_REGION_Y_OFFSET

    region_fish = {
        "left": int(cx - FISH_REGION_W // 2),
        "top":  int(cy - FISH_REGION_H // 2),
        "width": int(FISH_REGION_W),
        "height": int(FISH_REGION_H),
    }

    # ---- clamp region_fish inside selected monitor ----
    min_x, min_y = int(off_x), int(off_y)
    max_x, max_y = int(off_x + sw), int(off_y + sh)

    region_fish["width"] = max(1, int(region_fish["width"]))
    region_fish["height"] = max(1, int(region_fish["height"]))

    # left/top clamp
    if region_fish["left"] < min_x:
        region_fish["left"] = min_x
    if region_fish["top"] < min_y:
        region_fish["top"] = min_y

    # right/bottom clamp (move region back so it fits)
    if region_fish["left"] + region_fish["width"] > max_x:
        region_fish["left"] = max(min_x, max_x - region_fish["width"])
    if region_fish["top"] + region_fish["height"] > max_y:
        region_fish["top"] = max(min_y, max_y - region_fish["height"])
    # -----------------------------------------------

    start = time.perf_counter()
    last_switch = 0.0
    last_log = 0.0

    dir_streak = 0
    last_dir = "STILL"
    lost = 0

    release_ad("start")

    prev = np.array(sct.grab(region_fish))[:, :, :3]
    prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)

    r_score = 0.0  # чтобы не было UnboundLocalError в логах

    while time.perf_counter() - start < FISH_MAX_SEC:
        check_stop()

        # если появилось меню — стоп
        found_release, r_score, _, _ = find_release_button_once(sct, sw, sh, off_x, off_y)
        if found_release:
            release_ad(f"menu detected (release={r_score:.3f})")
            log.info(f"Стадия 3: меню найдено (release={r_score:.3f}) → выхожу")
            return True

        # запасной выход по fish_icon
        icon = fish_icon_score(sct, sw, sh, off_x, off_y)
        if lost == 0:
            icon_present = (icon >= FISH_ICON_THR_ON)
        else:
            icon_present = (icon >= FISH_ICON_THR_OFF)

        if icon_present:
            lost = 0
        else:
            lost += 1
            if lost >= FISH_ICON_LOST_FRAMES:
                release_ad("fish_icon gone")
                log.info(f"Стадия 3: fish_icon пропал (score={icon:.3f}) → выхожу")
                return True

        frame = np.array(sct.grab(region_fish))[:, :, :3]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)

        fx = flow[..., 0]
        mag = np.sqrt(fx * fx + flow[..., 1] * flow[..., 1])

        mask = mag > FLOW_MAG_THR
        flow_x = float(np.median(fx[mask])) if np.any(mask) else 0.0

        if flow_x > FLOW_DIR_THR:
            fish_dir = "RIGHT"
        elif flow_x < -FLOW_DIR_THR:
            fish_dir = "LEFT"
        else:
            fish_dir = "STILL"

        if fish_dir == last_dir and fish_dir != "STILL":
            dir_streak += 1
        elif fish_dir != "STILL":
            dir_streak = 1
            last_dir = fish_dir
        else:
            dir_streak = 0
            last_dir = "STILL"

        now = time.perf_counter()
        if dir_streak >= SWITCH_CONFIRM_FRAMES and (now - last_switch) >= SWITCH_COOLDOWN:
            # RIGHT -> D, LEFT -> A
            if fish_dir == "RIGHT" and _ad_state != "d":
                hold_d(f"(flow_x={flow_x:+.2f})")
                last_switch = now
            elif fish_dir == "LEFT" and _ad_state != "a":
                hold_a(f"(flow_x={flow_x:+.2f})")
                last_switch = now

        if now - last_log >= FISH_LOG_EVERY_SEC:
            last_log = now
            log.info(
                "FISH DBG | flow_x=%+.2f dir=%s streak=%d hold=%s | icon=%.3f lost=%d | release=%.3f",
                flow_x, fish_dir, dir_streak,
                _ad_state.upper() if _ad_state != "none" else "-",
                icon, lost, r_score
            )

        prev_gray = gray
        time.sleep(FISH_SEARCH_EVERY)

    release_ad("failsafe timeout")
    log.warning("Стадия 3: фейлсейф-таймаут")
    return False

# ===================== MAIN LOOP =====================
_running = False
_lock = threading.Lock()
_thread: threading.Thread | None = None
CAPTURE_MONITOR = 1

def one_cycle(sct, sw, sh, off_x: int, off_y: int):
    # helper: clamp region inside monitor bounds
    def _clamp_region(r: dict) -> dict:
        # monitor bounds in absolute desktop coords
        min_x = off_x
        min_y = off_y
        max_x = off_x + sw
        max_y = off_y + sh

        left = int(r.get("left", min_x))
        top = int(r.get("top", min_y))
        width = int(r.get("width", 1))
        height = int(r.get("height", 1))

        width = max(1, width)
        height = max(1, height)

        # clamp left/top so right/bottom fit
        if left < min_x:
            left = min_x
        if top < min_y:
            top = min_y
        if left + width > max_x:
            left = max(min_x, max_x - width)
        if top + height > max_y:
            top = max(min_y, max_y - height)

        return {"left": left, "top": top, "width": width, "height": height}

    # Stage 1 region (green bar) — центр/низ выбранного монитора + offset
    region_bar = _clamp_region({
        "left": off_x + (sw - ROI_W) // 2,
        "top":  off_y + sh - ROI_Y_OFFSET_FROM_BOTTOM - ROI_H,
        "width": ROI_W,
        "height": ROI_H,
    })

    # Stage 2 region (search bubbles) — сектор справа-снизу на выбранном мониторе + offset
    left = off_x + int(sw * SECTOR_RIGHT_FROM)
    top = off_y + int(sh * SECTOR_BOTTOM_FROM)
    right = off_x + int(sw * SECTOR_RIGHT_TO)
    bottom = off_y + int(sh * SECTOR_BOTTOM_TO)

    region_search = _clamp_region({
        "left": left,
        "top": top,
        "width": max(1, right - left),
        "height": max(1, bottom - top),
    })

    # Stage 1
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log.info(f"Стадия 1: попытка {attempt}/{MAX_ATTEMPTS} — жду полоску...")
        if not wait_for_bar(sct, region_bar, wait_sec=WAIT_BAR_SEC):
            log.warning("Стадия 1: полоска не появилась (таймаут)")
            continue

        log.info("Стадия 1: полоска появилась, ловлю зелёную...")
        if catch_green_once(sct, region_bar):
            log.info("Стадия 1: успех → стадия 2 (жду пузырьки)")
            break
        else:
            log.warning("Стадия 1: не поймал зелёную (таймаут)")
    else:
        log.warning("Стадия 1: не получилось — цикл прерван")
        return False

    # Stage 2
    if not wait_for_bubbles(sct, region_search):
        return False

    # Stage 3/4 — тоже должны знать offset, иначе будут считать от (0,0)
    if not stage3_follow_fish_until_menu(sct, sw, sh, off_x, off_y):
        return False

    if not stage4_click_release(sct, sw, sh, off_x, off_y):
        return False

    return True

def run_flow():
    global _running
    try:
        STOP_EVENT.clear()
        with mss.mss() as sct:
            # monitors[0] = весь виртуальный desktop, monitors[1..N] = физические мониторы
            mons = getattr(sct, "monitors", []) or []
            total = len(mons)
            max_idx = max(1, total - 1)

            idx = CAPTURE_MONITOR if isinstance(CAPTURE_MONITOR, int) else 1
            if idx < 1:
                idx = 1
            if idx > max_idx:
                idx = max_idx

            mon = sct.monitors[idx]
            off_x = int(mon.get("left", 0))
            off_y = int(mon.get("top", 0))
            sw = int(mon["width"])
            sh = int(mon["height"])

            log.info("[FISH] monitor=%d offset=(%d,%d) size=%dx%d", idx, off_x, off_y, sw, sh)

            log.info("Старт: жму E один раз (заброс)")
            press_e()

            cycle_num = 0
            while not STOP_EVENT.is_set():
                cycle_num += 1
                log.info(f"=== ЦИКЛ #{cycle_num} ===")

                ok = one_cycle(sct, sw, sh, off_x, off_y)

                if not ok:
                    log.warning("Цикл завершился неуспешно, продолжаю попытки...")
                time.sleep(0.10)

    except SystemExit:
        pass
    except Exception as e:
        log.exception(f"Ошибка: {e}")
    finally:
        with _lock:
            _running = False
        release_ad("final")
        log.info("Остановлено.")

def trigger():
    global _running, _thread
    with _lock:
        if _running:
            # TOGGLE: повторный запуск = остановка
            request_stop("toggle")
            return
        _running = True

    _thread = threading.Thread(target=run_flow, daemon=True)
    _thread.start()

# ===================== CLI MAIN =====================
def main():
    import argparse
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--autorun", action="store_true", help="Запустить цикл сразу")
    ap.add_argument("--no-hotkey", action="store_true", help="Не регистрировать хоткеи")
    ap.add_argument("--control-stdin", action="store_true", help="Слушать stdin для STOP")
    ap.add_argument("--monitor", type=int, default=1, help="номер монитора mss: 1..N")
    args, _ = ap.parse_known_args()

    global CAPTURE_MONITOR
    try:
        CAPTURE_MONITOR = int(args.monitor or 1)
    except Exception:
        CAPTURE_MONITOR = 1

    log.info("fisher.py стартовал | autorun=%s no_hotkey=%s control_stdin=%s",
             args.autorun, args.no_hotkey, args.control_stdin)

    if args.control_stdin:
        threading.Thread(target=_stdin_stop_watcher, daemon=True).start()

    # Хоткеи: F7 = toggle, ESC = stop. F8 НЕ используем.
    listener = None
    if not args.no_hotkey:
        def on_press(key):
            try:
                if key == pynput_keyboard.Key.esc:
                    request_stop("ESC")
                    return
                # F7 toggle
                if key == pynput_keyboard.Key.f7:
                    trigger()
            except Exception:
                pass

        listener = pynput_keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
        log.info("Хоткеи: F7=toggle (start/stop), ESC=stop")

    if args.autorun:
        trigger()

    try:
        while not STOP_EVENT.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        request_stop("KeyboardInterrupt")

    try:
        if listener:
            listener.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
