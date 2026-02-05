import os
import sys
import time
import threading
import ctypes
import argparse
from ctypes import wintypes

import cv2
import numpy as np
import mss
from pynput import keyboard

# ===================== SERVICE STOP =====================
STOP_EVENT = threading.Event()


def request_stop():
    STOP_EVENT.set()


def _stdin_control_thread():
    # GUI (core_app) сможет остановить процесс, отправив "STOP\n" в stdin
    try:
        while not STOP_EVENT.is_set():
            line = sys.stdin.readline()
            if not line:
                time.sleep(0.05)
                continue
            cmd = line.strip().upper()
            if cmd == "STOP":
                request_stop()
                return
    except Exception:
        # если stdin недоступен — просто игнор
        return


# ===================== PATH HELPERS =====================
def _res_dir() -> str:
    # где лежат ресурсы (в onefile это _MEIPASS)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


# ===================== REGIONS =====================
CENTER_ROI_WIDTH_RATIO = 0.07
CENTER_ROI_HEIGHT_RATIO = 0.10
CENTER_ROI_BOTTOM_MARGIN_RATIO = 0.02

# ===================== TIMINGS =====================
MATCH_THRESHOLD_E = 0.58
HITS_REQUIRED_E = 2

CHECK_DELAY = 0.10
PRESS_COOLDOWN = 0.22

WASD_PRESS_DELAY = 0.15

# default hotkey (можно переопределить аргументом --hotkey)
TOGGLE_KEY = keyboard.Key.f10
CAPTURE_MONITOR = 1

# ===================== CIRCLE / DARK MASK =====================
CIRCLE_V_MIN = 165
CIRCLE_ERODE = 2

SAT_MAX = 90
DARK_PCTL = 20
DARK_EXTRA = 6

# ===================== OCR =====================
OCR_HITS_REQUIRED = 2
OCR_SCORE_THRESHOLD = 0.30
OCR_MARGIN_THRESHOLD = 0.06
SHAPE_MAX_DIST = 1.25

SCALES = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30]

# ===================== DPI AWARE =====================
def set_dpi_aware():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

set_dpi_aware()

# ===================== SENDINPUT (SCANCODE) =====================
user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002

SC = {"e": 0x12, "w": 0x11, "a": 0x1E, "s": 0x1F, "d": 0x20}

try:
    ULONG_PTR = wintypes.ULONG_PTR
except AttributeError:
    ULONG_PTR = ctypes.c_void_p


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


def send_scancode(scan: int) -> bool:
    arr = (INPUT * 2)()

    arr[0].type = INPUT_KEYBOARD
    arr[0].union.ki = KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE, 0, 0)

    arr[1].type = INPUT_KEYBOARD
    arr[1].union.ki = KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, 0)

    sent = user32.SendInput(2, arr, ctypes.sizeof(INPUT))
    if sent != 2:
        err = ctypes.get_last_error()
        print(f"\nSendInput failed, GetLastError={err}")
        return False
    return True


def press_key(letter: str):
    scan = SC.get(letter.lower())
    if scan is None:
        return
    send_scancode(scan)


# ===================== TEMPLATE E =====================
def load_edges(path: str) -> np.ndarray:
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"Не удалось загрузить: {path}")
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.Canny(g, 50, 150)


def build_prompt_e_path(assets_dir: str | None) -> str:
    # приоритет: явный assets_dir -> _MEIPASS/sila/assets -> рядом с файлом
    if assets_dir:
        return os.path.join(assets_dir, "prompt_E.png")

    # если onefile и ассеты собраны как .../_MEIPASS/sila/assets
    cand1 = os.path.join(_res_dir(), "sila", "assets", "prompt_E.png")
    if os.path.exists(cand1):
        return cand1

    # обычный запуск из исходников: .../sila/assets
    cand2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "prompt_E.png")
    return cand2


tmpl_e_edges: np.ndarray | None = None


def best_match_value(search_gray: np.ndarray, tmpl_edges: np.ndarray) -> float:
    search_edges = cv2.Canny(search_gray, 50, 150)
    best_val = 0.0

    th, tw = tmpl_edges.shape[:2]
    sh, sw = search_edges.shape[:2]

    for s in SCALES:
        nw, nh = int(tw * s), int(th * s)
        if nw < 12 or nh < 12:
            continue
        # важно: допускаем ровно в размер ROI
        if nw > sw or nh > sh:
            continue

        resized = cv2.resize(tmpl_edges, (nw, nh), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(search_edges, resized, cv2.TM_CCOEFF_NORMED)
        _, mv, _, _ = cv2.minMaxLoc(res)

        if mv > best_val:
            best_val = float(mv)
            if best_val >= 0.93:
                break

    return best_val


# ===================== MASK HELPERS =====================
def _keep_top_components(bin_img: np.ndarray, top_k: int = 3, min_area: int = 120) -> np.ndarray:
    num, labels, stats, _ = cv2.connectedComponentsWithStats(bin_img, connectivity=8)
    if num <= 1:
        return bin_img

    comps = []
    for i in range(1, num):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area >= min_area:
            comps.append((area, i))
    if not comps:
        return np.zeros_like(bin_img)

    comps.sort(reverse=True)
    keep_ids = [i for _, i in comps[:top_k]]

    out = np.zeros_like(bin_img)
    for i in keep_ids:
        out[labels == i] = 255
    return out


def extract_letter_mask64_from_roi(roi_bgr: np.ndarray):
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]

    bright = (v >= CIRCLE_V_MIN).astype(np.uint8) * 255
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)

    cnts, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None

    cnt = max(cnts, key=cv2.contourArea)
    circle = np.zeros_like(bright)
    cv2.drawContours(circle, [cnt], -1, 255, thickness=-1)

    if CIRCLE_ERODE > 0:
        circle = cv2.erode(circle, np.ones((3, 3), np.uint8), iterations=CIRCLE_ERODE)

    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    gray_blur = cv2.medianBlur(gray, 3)

    inside_vals = gray_blur[circle > 0]
    if inside_vals.size < 60:
        return None

    thr = int(np.percentile(inside_vals, DARK_PCTL)) + DARK_EXTRA
    thr = max(20, min(thr, 160))

    good_clean = None

    for sat_max_try in (SAT_MAX, 140, 200, 255):
        dark = ((gray_blur <= thr) & (s <= sat_max_try)).astype(np.uint8) * 255
        inside = cv2.bitwise_and(dark, circle)

        inside2 = cv2.morphologyEx(inside, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
        inside2 = cv2.morphologyEx(inside2, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)

        clean = _keep_top_components(inside2, top_k=3, min_area=140)

        if np.count_nonzero(clean) >= 160:
            good_clean = clean
            break

    if good_clean is None:
        return None

    ys, xs = np.where(good_clean > 0)
    if xs.size == 0 or ys.size == 0:
        return None

    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    w = x1 - x0 + 1
    h2 = y1 - y0 + 1
    if w < 10 or h2 < 10:
        return None

    letter = good_clean[y0:y1 + 1, x0:x1 + 1]

    size = 64
    pad = 10
    side = max(w, h2) + pad * 2
    canvas = np.zeros((side, side), dtype=np.uint8)

    ox = (side - w) // 2
    oy = (side - h2) // 2
    canvas[oy:oy + h2, ox:ox + w] = letter

    norm = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
    _, norm = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY)
    return norm


# ===================== OCR: prototypes + matching =====================
def render_letter(letter: str, size=64, font=cv2.FONT_HERSHEY_SIMPLEX, scale=2.1, thickness=6) -> np.ndarray:
    img = np.zeros((size, size), dtype=np.uint8)
    text = letter.upper()
    (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
    x = max(1, (size - tw) // 2)
    y = max(th + 1, (size + th) // 2)
    cv2.putText(img, text, (x, y), font, scale, 255, thickness, cv2.LINE_AA)
    _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY)
    return img


def warp_affine(img: np.ndarray, angle=0.0, shear=0.0, scale=1.0) -> np.ndarray:
    h, w = img.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    M = cv2.getRotationMatrix2D((cx, cy), angle, scale)
    Sh = np.array([[1.0, shear, 0.0],
                   [0.0, 1.0, 0.0]], dtype=np.float32)

    M3 = np.vstack([M, [0, 0, 1]]).astype(np.float32)
    Sh3 = np.vstack([Sh, [0, 0, 1]]).astype(np.float32)
    A3 = Sh3 @ M3
    out = cv2.warpAffine(img, A3[:2], (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
    _, out = cv2.threshold(out, 0, 255, cv2.THRESH_BINARY)
    return out


def _mask_to_contour(bin64: np.ndarray):
    cnts, _ = cv2.findContours(bin64, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    return max(cnts, key=cv2.contourArea)


def build_proto_db():
    letters = ["w", "a", "s", "d", "o"]  # o -> d
    fonts = [cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX, cv2.FONT_HERSHEY_COMPLEX]
    scales = [1.9, 2.1, 2.3]
    thicks = [5, 6]
    angles = [-12, -8, -4, 0, 4, 8, 12]
    shears = [-0.18, -0.10, 0.0, 0.10, 0.18]

    db = {ch: [] for ch in letters}
    contours = {ch: [] for ch in letters}

    for ch in letters:
        for f in fonts:
            for sc in scales:
                for th in thicks:
                    base = render_letter(ch, 64, f, sc, th)
                    for ang in angles:
                        for sh in shears:
                            m = warp_affine(base, angle=ang, shear=sh, scale=1.0)
                            db[ch].append(m)
                            c = _mask_to_contour(m)
                            if c is not None:
                                contours[ch].append(c)
    return db, contours


PROTO_DB, PROTO_CONTOURS = build_proto_db()


def score_match(a: np.ndarray, b: np.ndarray) -> float:
    af = a.astype(np.float32) / 255.0
    bf = b.astype(np.float32) / 255.0
    res = cv2.matchTemplate(af, bf, cv2.TM_CCOEFF_NORMED)
    return float(res[0, 0])


def classify_letter(mask64: np.ndarray):
    scores = []
    for ch, plist in PROTO_DB.items():
        best = -1.0
        for p in plist:
            sc = score_match(mask64, p)
            if sc > best:
                best = sc
        scores.append((best, ch))
    scores.sort(reverse=True)
    best_sc, best_ch = scores[0]
    second_sc, _ = scores[1]

    confident = (best_sc >= OCR_SCORE_THRESHOLD) and ((best_sc - second_sc) >= OCR_MARGIN_THRESHOLD)
    if confident:
        if best_ch == "o":
            return "d", best_sc
        if best_ch in ("w", "a", "s", "d"):
            return best_ch, best_sc
        return None, best_sc

    c_in = _mask_to_contour(mask64)
    if c_in is None:
        return None, best_sc

    best_dist = 1e9
    best_shape_ch = None
    for ch, clist in PROTO_CONTOURS.items():
        local = 1e9
        for c in clist:
            d = cv2.matchShapes(c_in, c, cv2.CONTOURS_MATCH_I1, 0.0)
            if d < local:
                local = d
        if local < best_dist:
            best_dist = local
            best_shape_ch = ch

    final = best_ch if best_dist > SHAPE_MAX_DIST else best_shape_ch

    if final == "o":
        return "d", best_sc
    if final in ("w", "a", "s", "d"):
        return final, best_sc
    return None, best_sc


# ===================== STATE MACHINE =====================
running = False
last_press_ts = 0.0

state = "WAIT_E"     # WAIT_E -> WASD_LOOP
hits_e = 0

# latch: чтобы не спамить E, пока prompt_E не исчезнет хотя бы раз
e_latched = False

ocr_last = None
ocr_hits = 0


def _switch_to_wait_e():
    global state, hits_e, ocr_last, ocr_hits, e_latched
    state = "WAIT_E"
    hits_e = 0
    ocr_last = None
    ocr_hits = 0
    e_latched = False


def _switch_to_wasd():
    global state, ocr_last, ocr_hits, hits_e, e_latched
    state = "WASD_LOOP"
    ocr_last = None
    ocr_hits = 0
    hits_e = 0
    e_latched = False


def worker():
    global running, last_press_ts, state, hits_e, e_latched
    global ocr_last, ocr_hits
    global tmpl_e_edges

    if tmpl_e_edges is None:
        print("\n[ERR] tmpl_e_edges не загружен")
        running = False
        return

    with mss.mss() as sct:
        # MSS: monitors[0] = весь виртуальный десктоп, monitors[1..N] = физические мониторы
        mons = getattr(sct, "monitors", []) or []
        total = len(mons)
        max_idx = max(1, total - 1)

        idx = CAPTURE_MONITOR if isinstance(CAPTURE_MONITOR, int) else 1
        if idx < 1:
            idx = 1
        if idx > max_idx:
            idx = max_idx

        mon_full = sct.monitors[idx]
        off_x = int(mon_full.get("left", 0))
        off_y = int(mon_full.get("top", 0))

        screen_w = int(mon_full.get("width", 0))
        screen_h = int(mon_full.get("height", 0))

        print(f"\n[SILA] monitor={idx} {screen_w}x{screen_h} @({off_x},{off_y})")

        mon_e = {
            "left": int(off_x),
            "top": int(off_y),
            "width": int(screen_w // 2),
            "height": int(screen_h // 2),
        }

        roi_w = max(140, int(screen_w * CENTER_ROI_WIDTH_RATIO))
        roi_h = max(140, int(screen_h * CENTER_ROI_HEIGHT_RATIO))
        roi_left = int((screen_w - roi_w) / 2)
        roi_top = int(screen_h - roi_h - screen_h * CENTER_ROI_BOTTOM_MARGIN_RATIO)

        mon_center = {
            "left": off_x + roi_left,
            "top": off_y + roi_top,
            "width": roi_w,
            "height": roi_h,
        }

        while running and (not STOP_EVENT.is_set()):
            now = time.time()

            # --- ALWAYS check prompt_E to decide phase ---
            img_e = np.array(sct.grab(mon_e))
            gray_e = cv2.cvtColor(img_e, cv2.COLOR_BGRA2GRAY)
            mv = best_match_value(gray_e, tmpl_e_edges)
            e_seen = (mv >= MATCH_THRESHOLD_E)

            if state == "WAIT_E":
                hits_e = hits_e + 1 if e_seen else 0
                print(f"[WAIT_E] maxE={mv:.3f} hits={hits_e}/{HITS_REQUIRED_E}   ", end="\r")

                if hits_e >= HITS_REQUIRED_E and (now - last_press_ts) >= PRESS_COOLDOWN:
                    press_key("e")
                    last_press_ts = now
                    _switch_to_wasd()

                time.sleep(CHECK_DELAY)
                continue

            # state == WASD_LOOP
            if e_seen:
                # если уже нажали E и prompt_E ещё не исчезал — НЕ нажимаем повторно
                if e_latched:
                    print(f"[WASD_LOOP] prompt_E still visible (latched) maxE={mv:.3f}   ", end="\r")
                    time.sleep(CHECK_DELAY)
                    continue

                hits_e = hits_e + 1
                if hits_e >= HITS_REQUIRED_E and (now - last_press_ts) >= PRESS_COOLDOWN:
                    print(f"\n[WASD_LOOP] prompt_E появился (maxE={mv:.3f}) -> press E")
                    press_key("e")
                    last_press_ts = now

                    # защёлка: ждём, пока prompt_E пропадёт
                    e_latched = True
                    hits_e = 0

                    # сброс OCR-части
                    ocr_last = None
                    ocr_hits = 0

                time.sleep(CHECK_DELAY)
                continue
            else:
                # prompt_E пропал — можно снова разрешить нажатие при следующем появлении
                hits_e = 0
                e_latched = False

            # --- распознаём WASD ---
            img = np.array(sct.grab(mon_center))
            bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

            mask64 = extract_letter_mask64_from_roi(bgr)
            if mask64 is None:
                ocr_last = None
                ocr_hits = 0
                print(f"[WASD_LOOP] no letter   maxE={mv:.3f}   ", end="\r")
                time.sleep(CHECK_DELAY)
                continue

            ch, sc = classify_letter(mask64)

            if ch is not None:
                if ocr_last == ch:
                    ocr_hits += 1
                else:
                    ocr_last = ch
                    ocr_hits = 1
            else:
                ocr_last = None
                ocr_hits = 0

            print(
                f"[WASD_LOOP] ocr={ch} score={sc:.2f} hits={ocr_hits}/{OCR_HITS_REQUIRED}  maxE={mv:.3f}   ",
                end="\r",
            )

            if ocr_hits >= OCR_HITS_REQUIRED and ocr_last and (now - last_press_ts) >= PRESS_COOLDOWN:
                key_to_press = ocr_last
                ocr_last = None
                ocr_hits = 0

                time.sleep(WASD_PRESS_DELAY)
                press_key(key_to_press)
                last_press_ts = time.time()

            time.sleep(CHECK_DELAY)


def toggle():
    global running
    running = not running
    _switch_to_wait_e()
    if running:
        print("\n[F10] ON")
        threading.Thread(target=worker, daemon=True).start()
    else:
        print("\n[F10] OFF")


def on_press(key):
    if key == TOGGLE_KEY:
        toggle()


def _parse_hotkey(s: str):
    s = (s or "").strip().lower()
    if s.startswith("f") and s[1:].isdigit():
        n = int(s[1:])
        name = f"f{n}"
        return getattr(keyboard.Key, name, keyboard.Key.f10)
    return keyboard.Key.f10


def main():
    global TOGGLE_KEY, tmpl_e_edges, running, CAPTURE_MONITOR

    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--assets-dir", default="", help="папка assets (где prompt_E.png)")
    ap.add_argument("--hotkey", default="f10", help="hotkey toggle: f1..f24")
    ap.add_argument("--autorun", action="store_true", help="сразу включить worker")
    ap.add_argument("--no-hotkey", action="store_true", help="не слушать клавиатуру (для GUI)")
    ap.add_argument("--control-stdin", action="store_true", help="слушать STOP из stdin (для GUI)")
    ap.add_argument("--monitor", type=int, default=1, help="номер монитора mss: 1..N")
    args = ap.parse_args()

    # ✅ монитор приходит из core_app как --monitor N
    try:
        CAPTURE_MONITOR = int(args.monitor or 1)
    except Exception:
        CAPTURE_MONITOR = 1
    if CAPTURE_MONITOR < 1:
        CAPTURE_MONITOR = 1

    TOGGLE_KEY = _parse_hotkey(args.hotkey)

    prompt_path = build_prompt_e_path(args.assets_dir if args.assets_dir else None)
    try:
        tmpl_e_edges = load_edges(prompt_path)
    except Exception as e:
        print(f"[ERR] Не удалось загрузить шаблон E: {prompt_path} ({e})")
        return

    if args.control_stdin:
        threading.Thread(target=_stdin_control_thread, daemon=True).start()

    if args.autorun:
        running = True
        _switch_to_wait_e()
        threading.Thread(target=worker, daemon=True).start()
        print("[SILA] autorun ON (ожидаю STOP из stdin)" if args.no_hotkey else "[SILA] autorun ON")

        if args.no_hotkey:
            try:
                while not STOP_EVENT.is_set():
                    time.sleep(0.2)
            except KeyboardInterrupt:
                pass
            return

    if args.no_hotkey:
        print("[SILA] no-hotkey, ожидаю STOP")
        try:
            while not STOP_EVENT.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        return

    print("F10 — ON/OFF")
    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()


if __name__ == "__main__":
    main()
