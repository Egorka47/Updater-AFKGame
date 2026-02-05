# gui.py
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import re
import threading
import shutil
import urllib.request
from typing import Any, Optional

from config import BOT_TOKEN
from core_app import (GlobalHotkeys, HotkeySettings, ServiceManager, run_service_mode_if_requested, get_user_data_dir, get_exe_dir, get_res_dir)

from paths import get_app_data_dir, res_dir, exe_dir

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainterPath

# ===================== SERVICE MODE =====================
if run_service_mode_if_requested():
    raise SystemExit

# ===================== WINDOWS APP ID =====================
if os.name == "nt":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AFKGame.app")
    except (AttributeError, OSError):
        pass

# ===================== WINDOWS TITLE BAR COLOR (Win10/11) =====================
if os.name == "nt":
    from ctypes import wintypes

    _DARK_ATTR_CANDIDATES = (20, 19)

    DWMWA_BORDER_COLOR = 34
    DWMWA_CAPTION_COLOR = 35
    DWMWA_TEXT_COLOR = 36
    DWMWA_SYSTEMBACKDROP_TYPE = 38
    DWMWA_MICA_EFFECT = 1029

    DWMSBT_NONE = 0
    GA_ROOT = 2

    _CAPTION_BORDER_SUPPORTED: bool | None = None

    def _hex_to_dwmargb(hex_color: str, a: int = 255) -> int:
        s = (hex_color or "").lstrip("#")
        if len(s) != 6:
            s = "000000"
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (a << 24) | (r << 16) | (g << 8) | b

    _dwm = ctypes.WinDLL("dwmapi")
    _dwm.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        wintypes.LPCVOID,
        wintypes.DWORD]
    _dwm.DwmSetWindowAttribute.restype = ctypes.c_long

    _user32 = ctypes.WinDLL("user32")

    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND

    _user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT]
    _user32.SetWindowPos.restype = wintypes.BOOL

    _user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    _user32.RedrawWindow.restype = wintypes.BOOL

    try:
        _dwm.DwmFlush.argtypes = []
        _dwm.DwmFlush.restype = ctypes.c_long
        _HAS_FLUSH = True
    except (AttributeError, OSError):
        _HAS_FLUSH = False

    def _get_top_hwnd_from_winid(win_id: int) -> int:
        try:
            raw = int(win_id) if win_id else 0
        except (TypeError, ValueError):
            return 0

        if not raw:
            return 0

        try:
            top = int(_user32.GetAncestor(wintypes.HWND(raw), GA_ROOT))
            return top if top else raw
        except (OSError, AttributeError, ctypes.ArgumentError, ValueError, TypeError):
            return raw

    def _dwm_set_attr(hwnd: int, attr: int, value_ptr, value_size: int) -> bool:
        try:
            hr = int(
                _dwm.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd),
                    wintypes.DWORD(attr),
                    value_ptr,
                    wintypes.DWORD(value_size)))
            return hr == 0
        except (OSError, AttributeError, ctypes.ArgumentError, ValueError, TypeError):
            return False


    def _force_nc_redraw(hwnd: int) -> None:
        try:
            swp_nosize = 0x0001
            swp_nomove = 0x0002
            swp_nozorder = 0x0004
            swp_noactivate = 0x0010
            swp_framechanged = 0x0020

            rdw_invalidate = 0x0001
            rdw_updatenow = 0x0100
            rdw_frame = 0x0400

            _user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(0),
                0, 0, 0, 0,
                swp_nomove | swp_nosize | swp_nozorder | swp_noactivate | swp_framechanged,
            )
            _user32.RedrawWindow(
                wintypes.HWND(hwnd),
                None,
                None,
                rdw_invalidate | rdw_updatenow | rdw_frame,
            )

            if _HAS_FLUSH:
                _dwm.DwmFlush()
        except (OSError, AttributeError, ctypes.ArgumentError, TypeError, ValueError):
            pass

    def set_titlebar_colors_qt(widget: QtWidgets.QWidget, *, caption: str | None = None, text: str | None = None, border: str | None = None, dark: bool = True, disable_backdrop: bool = True) -> bool:
        global _CAPTION_BORDER_SUPPORTED

        try:
            win_id = widget.winId()
            hwnd = _get_top_hwnd_from_winid(int(win_id))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

        if not hwnd:
            return False

        if disable_backdrop:
            v = wintypes.DWORD(DWMSBT_NONE)
            _dwm_set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(v), ctypes.sizeof(v))
            v2 = wintypes.BOOL(0)
            _dwm_set_attr(hwnd, DWMWA_MICA_EFFECT, ctypes.byref(v2), ctypes.sizeof(v2))

        val = wintypes.BOOL(1 if dark else 0)
        for attr in _DARK_ATTR_CANDIDATES:
            if _dwm_set_attr(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)):
                break

        if text:
            c = wintypes.DWORD(_hex_to_dwmargb(text, a=255))
            _dwm_set_attr(hwnd, DWMWA_TEXT_COLOR, ctypes.byref(c), ctypes.sizeof(c))

        if _CAPTION_BORDER_SUPPORTED is None:
            test = wintypes.DWORD(_hex_to_dwmargb("#000000", a=255))
            ok_c = _dwm_set_attr(hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(test), ctypes.sizeof(test))
            ok_b = _dwm_set_attr(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(test), ctypes.sizeof(test))
            _CAPTION_BORDER_SUPPORTED = bool(ok_c and ok_b)

        if _CAPTION_BORDER_SUPPORTED:
            if border:
                cb = wintypes.DWORD(_hex_to_dwmargb(border, a=255))
                _dwm_set_attr(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(cb), ctypes.sizeof(cb))
            if caption:
                cc = wintypes.DWORD(_hex_to_dwmargb(caption, a=255))
                _dwm_set_attr(hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(cc), ctypes.sizeof(cc))

        _force_nc_redraw(hwnd)
        return True

else:
    def set_titlebar_colors_qt(*_a, **_kw) -> bool:
        return False

# ===================== PATHS =====================
RES_DIR = get_res_dir()                     # ресурсы (onefile -> _MEIPASS)
EXE_DIR = get_exe_dir()                     # где лежит exe
DATA_DIR = get_user_data_dir("XyesosBeta")  # куда можно писать без прав админа

# основной cfg (без расширения) + старый (на всякий случай)
CFG_PATH = os.path.join(DATA_DIR, "cfg")
LEGACY_CFG_JSON = os.path.join(DATA_DIR, "cfg.json")

def ensure_cfg_exists() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)

        cfg_path = CFG_PATH
        legacy_json = LEGACY_CFG_JSON

        def _read_json(p: str) -> dict:
            try:
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        j = json.load(f)
                        return j if isinstance(j, dict) else {}
            except Exception:
                pass
            return {}

        default_cfg = {
            "version": 1,
            "hotkeys": {
                "orange_hotkey": "F7",
                "afk_hotkey": "F10",
                "fish_hotkey": "F5",
                "sila_hotkey": "F9"},
            "monitor": 1}

        # 1) если нового cfg нет — создаём (с миграцией)
        if not os.path.exists(cfg_path):
            cfg = _read_json(legacy_json) if os.path.exists(legacy_json) else {}
            if not cfg:
                cfg = dict(default_cfg)

            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return

        # 2) cfg есть — валидируем и дотягиваем поля
        cfg = _read_json(cfg_path)
        if not cfg:
            cfg = dict(default_cfg)

        changed = False

        hk = cfg.get("hotkeys")
        if not isinstance(hk, dict):
            cfg["hotkeys"] = dict(default_cfg["hotkeys"])
            hk = cfg["hotkeys"]
            changed = True
        else:
            for k, v in default_cfg["hotkeys"].items():
                if not isinstance(hk.get(k), str) or not hk.get(k).strip():
                    hk[k] = v
                    changed = True

        mon = cfg.get("monitors")
        if not isinstance(mon, dict):
            cfg["monitors"] = dict(default_cfg["monitors"])
            changed = True
        else:
            for k, v in default_cfg["monitors"].items():
                if not isinstance(mon.get(k), int):
                    mon[k] = v
                    changed = True

        if changed:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)

    except Exception:
        pass

# ===================== FONTS =====================
DEV_FONTS_DIR = r"E:\Python Project's\GTA_RP_CAR\fonts"

def _fonts_dir() -> str:
    # dev: твой путь
    if (not getattr(sys, "frozen", False)) and os.path.isdir(DEV_FONTS_DIR):
        return DEV_FONTS_DIR

    # exe: fonts рядом с exe (удобно обновлять)
    if getattr(sys, "frozen", False):
        d1 = os.path.join(os.path.dirname(sys.executable), "fonts")
        if os.path.isdir(d1):
            return d1

    # fallback: fonts рядом со скриптом/в _MEIPASS
    return os.path.join(RES_DIR, "fonts")

def _try_load_app_font() -> str:
    fonts_dir = _fonts_dir()

    candidates = [
        "Inter-Regular.ttf",
        "Inter-Medium.ttf",
        "Inter-SemiBold.ttf",
        "Inter-Bold.ttf"]

    loaded = []
    for fn in candidates:
        fp = os.path.join(fonts_dir, fn)
        if os.path.exists(fp):
            fid = QtGui.QFontDatabase.addApplicationFont(fp)
            if fid != -1:
                loaded += QtGui.QFontDatabase.applicationFontFamilies(fid)

    if loaded:
        return loaded[0]

    # system fallback
    fams = set(QtGui.QFontDatabase.families())
    for name in ("Segoe UI Variable Text", "Segoe UI", "Arial"):
        if name in fams:
            return name

    return QtWidgets.QApplication.font().family()

def apply_app_typography(app: QtWidgets.QApplication) -> str:
    family = _try_load_app_font()
    f = QtGui.QFont(family)
    f.setPointSize(10)
    f.setHintingPreference(QtGui.QFont.HintingPreference.PreferFullHinting)
    app.setFont(f)
    return family

# --- resources ---
ORANGE_ICON = os.path.join(RES_DIR, "apelsin", "assets", "fruikt.png")
INFO_ICON = os.path.join(RES_DIR, "apelsin", "assets", "Info.png")
AUTH_VIDEO = os.path.join(RES_DIR, "Auth.webp")
UNLOCK_WEBP = os.path.join(RES_DIR, "Unlock.webp")
WRITE_WEBP = os.path.join(RES_DIR, "Write.webp")
FISH_ICON = os.path.join(RES_DIR, "fish", "assets", "Fish.png")
tg_path = os.path.join(RES_DIR, "telegram.png")
MAIN_ICON = os.path.join(RES_DIR, "Main.png")
SETTINGS_ICON = r"E:\Python Project's\GTA_RP_CAR\Settings.png"

STATE_PATH = os.path.join(DATA_DIR, "auth_state.json")
CFG_PATH = os.path.join(DATA_DIR, "cfg")
log_path = os.path.join(DATA_DIR, "bot.log")

# --- import priority for onefile ---
# 1) сначала папка с exe (там config.json и можно положить cf_lock.py рядом для хотфикса)
if EXE_DIR and (EXE_DIR not in sys.path):
    sys.path.insert(0, EXE_DIR)

# 2) затем ресурсы _MEIPASS (если cf_lock лежит внутри onefile)
if RES_DIR and (RES_DIR not in sys.path):
    sys.path.insert(1, RES_DIR)

import cf_lock  # <-- импорт строго после sys.path
print("CF_LOCK IMPORTED FROM:", getattr(cf_lock, "__file__", "??"))


# ===================== THEME =====================
BG = "#10131b"
CARD = "#171a22"
TEXT = "#e6e6eb"
MUTED = "#a9acb6"

RED = "#e74c3c"
RED_HOVER = "#ff6b5a"
GREEN = "#2ecc71"
GREEN_HOVER = "#48e68b"

SW_OFF_TRACK = "#DC143C"
SW_ON_TRACK = "#228B22"
SW_KNOB = "#f1f2f6"

ENTRY_BG = "#10131b"

RIGHT_COL_W = 150
ROW_GAP_Y = 14

ensure_cfg_exists()

# ===================== VERSION / UPDATE =====================
APP_VERSION = "1.0.0"

GITHUB_OWNER = "Egorka47"
GITHUB_REPO = "Updater-AFKGame"

UPDATE_MANIFEST_URL = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/latest.json"

UPDATER_LOCAL_NAME = "updater.exe"      # как будет называться в %LOCALAPPDATA%/XyesosBeta
UPDATER_BUNDLED_NAME = "updater.exe"    # как лежит в ресурсах (_MEIPASS) через --add-data

# ===================== CORE OBJECTS =====================
services = ServiceManager(DATA_DIR)
settings = HotkeySettings(DATA_DIR)

# ===================== GLOBAL STATE =====================
_app_closing = False
bot_proc: Optional[subprocess.Popen] = None

_bot_start_lock = threading.Lock()
_bot_start_guard = None

_bot_dots = {"i": 0}
_auth_wait_lock = {"on": False, "owner": "", "pos": None, "job": None}
_preauth = {"active": False, "deadline": 0.0}
PREAUTH_SEC = 60

TG_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

# ===================== STATE IO =====================
def read_state() -> dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}

def write_state(data: dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        os.replace(tmp, STATE_PATH)
    except (OSError, TypeError):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

def normalize_username(s: str) -> str:
    s = (s or "").strip()
    low = s.lower()

    for pref in ("https://t.me/", "t.me/"):
        if pref in low:
            idx = low.rfind(pref)
            s = s[idx + len(pref):]
            break
    else:
        needle = "t.me/"
        if needle in low:
            idx = low.rfind(needle)
            s = s[idx + len(needle):]

    # отрезаем хвосты/параметры
    for sep in ("?", "/", "#", " "):
        if sep in s:
            s = s.split(sep, 1)[0]

    s = s.strip()
    if s.startswith("@"):
        s = s[1:]
    return s.lower().strip()

def looks_like_tg_username(norm_un: str) -> bool:
    if not norm_un:
        return False
    return bool(TG_USERNAME_RE.fullmatch(norm_un))

st0 = read_state()
if (st0.get("bot_token") or "").strip() != BOT_TOKEN:
    st0["bot_token"] = BOT_TOKEN
    write_state(st0)

def ensure_data_config_before_bot() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        dst = os.path.join(DATA_DIR, "config.json")

        def _read_json(p: str) -> dict:
            try:
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        j = json.load(f)
                        return j if isinstance(j, dict) else {}
            except Exception:
                pass
            return {}

        # 1) пробуем взять из cf_lock (если уже что-то поднялось)
        url = str(getattr(cf_lock, "LOCK_URL", "") or "").strip()
        sec = str(getattr(cf_lock, "LOCK_SECRET", "") or "").strip()
        cid = str(getattr(cf_lock, "CLIENT_ID", "") or "").strip()

        # 2) если пусто — пробуем подхватить из config рядом с exe или из ресурсов
        if not (url and sec):
            for p in (
                os.path.join(EXE_DIR, "config.json"),
                os.path.join(RES_DIR, "config.json"),
            ):
                j = _read_json(p)
                u = str(j.get("cf_lock_url", "") or "").strip()
                s = str(j.get("cf_lock_secret", "") or "").strip()
                c = str(j.get("client_id", "") or "").strip()
                if u and s:
                    url, sec = u, s
                    if c:
                        cid = c
                    break

        # 3) пишем в DATA_DIR только если есть url+secret
        if url and sec:
            existing = _read_json(dst)
            merged = dict(existing)
            merged["cf_lock_url"] = url
            merged["cf_lock_secret"] = sec
            if cid:
                merged["client_id"] = cid

            with open(dst, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)

    except Exception:
        pass

# ===================== BOT / LOCK =====================
def _start_local_bot() -> None:
    global bot_proc, _bot_start_guard

    lock = globals().get("_bot_start_lock")
    if lock is None:
        lock = threading.Lock()
        globals()["_bot_start_lock"] = lock

    if not lock.acquire(blocking=False):
        return

    try:
        # уже запущен
        try:
            p = bot_proc
            if p is not None and p.poll() is None:
                return
        except Exception:
            bot_proc = None

        # 1) init cf_lock (прочитает url/token из config/env)
        try:
            cf_lock.init_lock_settings()
        except Exception:
            pass

        # ---- helpers: очередь ----
        def _queue_on(owner_id: str) -> None:
            try:
                w = globals().get("_auth_wait_lock")
                if not isinstance(w, dict):
                    return
                w["on"] = True
                w["owner"] = owner_id or ""
                w["pos"] = None

                job = w.get("job")
                if isinstance(job, threading.Thread) and job.is_alive():
                    return

                def _queue_worker():
                    while True:
                        if globals().get("_app_closing"):
                            break
                        ww = globals().get("_auth_wait_lock")
                        if not isinstance(ww, dict) or not ww.get("on"):
                            break
                        try:
                            pos = cf_lock.get_queue_position()
                        except Exception:
                            pos = None
                        try:
                            ww["pos"] = pos
                        except Exception:
                            pass
                        time.sleep(2.0)

                t = threading.Thread(target=_queue_worker, daemon=True)
                w["job"] = t
                t.start()
            except Exception:
                pass

        def _queue_off() -> None:
            try:
                w = globals().get("_auth_wait_lock")
                if not isinstance(w, dict):
                    return
                w["on"] = False
                w["pos"] = None
                w["owner"] = ""
                w["job"] = None
            except Exception:
                pass

        # 2) гарантируем DATA_DIR и bot.log (чтобы НЕ было “тишины”)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except Exception:
            pass

        bot_log_path = os.path.join(DATA_DIR, "bot.log")
        _bot_start_guard = object()

        def _read_json_file(p: str) -> dict:
            try:
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        j = json.load(f)
                        return j if isinstance(j, dict) else {}
            except Exception:
                pass
            return {}

        # 3) подготовим DATA_DIR/config.json (PROXY: url + token)
        have_url = False
        have_token = False
        lu = str(getattr(cf_lock, "LOCK_URL", "") or "").strip()
        at = str(getattr(cf_lock, "AUTH_TOKEN", "") or "").strip()

        # если токен проброшен через env — используем его
        env_token = (os.environ.get("CF_AUTH_TOKEN") or "").strip()
        if env_token and not at:
            at = env_token

        # если url/token пустые — пробуем вытащить из config.json (DATA_DIR, EXE_DIR, RES_DIR)
        if not lu or not at:
            for src in (
                os.path.join(DATA_DIR, "config.json"),
                os.path.join(EXE_DIR, "config.json"),
                os.path.join(RES_DIR, "config.json"),
            ):
                j = _read_json_file(src)
                if not lu:
                    u = str(j.get("cf_lock_url", "") or "").strip()
                    if u:
                        lu = u
                if not at:
                    t = str(j.get("cf_auth_token", "") or "").strip()
                    if t:
                        at = t
                if lu and at:
                    break

        have_url = bool(lu)
        have_token = bool(at)

        # пишем в DATA_DIR/config.json (не затираем пустыми)
        try:
            p_cfg = os.path.join(DATA_DIR, "config.json")
            old_cfg = _read_json_file(p_cfg)
            merged = dict(old_cfg)
            if lu:
                merged["cf_lock_url"] = lu
            if at:
                merged["cf_auth_token"] = at
            if merged.get("cf_lock_url"):
                with open(p_cfg, "w", encoding="utf-8") as f:
                    json.dump(merged, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        # синхронизируем cf_lock (чтобы try_acquire видел значения)
        try:
            if lu:
                cf_lock.LOCK_URL = lu.rstrip("/")
            if at:
                cf_lock.AUTH_TOKEN = at
        except Exception:
            pass

        # если не хватает настроек — пишем в bot.log и выходим
        if not have_url or not have_token:
            try:
                with open(bot_log_path, "a", encoding="utf-8", errors="ignore", buffering=1) as lf:
                    lf.write(f"[gui] missing settings: have_url={have_url} have_token={have_token}\n")
                    lf.write(f"[gui] DATA_DIR={DATA_DIR}\n")
                    lf.write(f"[gui] LOCK_URL={lu}\n")
                    lf.write(f"[gui] AUTH_TOKEN_LEN={len(at or '')}\n")
            except Exception:
                pass
            bot_proc = None
            _queue_off()
            return

        # 4) пробуем взять лок
        try:
            can_run, owner = cf_lock.try_acquire_lock()
        except Exception as e:
            try:
                with open(bot_log_path, "a", encoding="utf-8", errors="ignore", buffering=1) as lf:
                    lf.write(f"[gui] try_acquire_lock exception: {repr(e)}\n")
            except Exception:
                pass
            bot_proc = None
            _queue_off()
            return

        if not can_run:
            bot_proc = None
            _queue_on(owner_id=str(owner or ""))
            # полезно залогировать владельца/очередь
            try:
                with open(bot_log_path, "a", encoding="utf-8", errors="ignore", buffering=1) as lf:
                    lf.write(f"[gui] lock busy. owner={owner!r}\n")
            except Exception:
                pass
            return

        _queue_off()

        # 5) renew-поток
        try:
            cf_lock.start_renew_thread()
        except Exception as e:
            try:
                with open(bot_log_path, "a", encoding="utf-8", errors="ignore", buffering=1) as lf:
                    lf.write(f"[gui] start_renew_thread failed: {repr(e)}\n")
            except Exception:
                pass
            try:
                cf_lock.release_lock()
            except Exception:
                pass
            bot_proc = None
            return

        # 6) стартуем bot как subprocess и пишем stdout/err в bot.log
        logf = None
        try:
            logf = open(bot_log_path, "a", encoding="utf-8", errors="ignore", buffering=1)
            logf.write("[gui] starting bot process...\n")
            logf.flush()

            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--service", "bot"]
                cwd = EXE_DIR
            else:
                this_py = os.path.abspath(__file__)
                cmd = [sys.executable, this_py, "--service", "bot"]
                cwd = os.path.dirname(this_py)

            env = os.environ.copy()
            env["CF_LOCK_URL"] = str(getattr(cf_lock, "LOCK_URL", "") or "").strip()
            env["CF_AUTH_TOKEN"] = str(getattr(cf_lock, "AUTH_TOKEN", "") or "").strip()

            bot_proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=logf,
                stderr=logf,
                stdin=subprocess.DEVNULL,
                env=env,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )

            # Job Object
            try:
                from core_app import assign_to_app_job
                assign_to_app_job(bot_proc)
            except Exception:
                pass

        except Exception as e:
            bot_proc = None
            try:
                if logf is not None:
                    logf.write(f"[gui] bot start failed: {repr(e)}\n")
                    logf.flush()
            except Exception:
                pass
            try:
                cf_lock.stop_renew_thread()
            except Exception:
                pass
            try:
                cf_lock.release_lock()
            except Exception:
                pass

        finally:
            if logf is not None:
                try:
                    logf.close()
                except Exception:
                    pass

    finally:
        try:
            lock.release()
        except Exception:
            pass

def _stop_local_bot_and_release_lock() -> None:
    global bot_proc

    try:
        from cf_lock import shutdown_lock
        shutdown_lock()
    except (ImportError, OSError, RuntimeError, AttributeError):
        pass

    proc = bot_proc
    bot_proc = None
    if proc is None:
        return

    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except (OSError, subprocess.SubprocessError):
                    pass
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass

# ===================== BOT STATUS TEXT =====================
def bot_ui_status() -> tuple[str, str]:
    # очередь/лок
    try:
        if _auth_wait_lock.get("on"):
            pos = _auth_wait_lock.get("pos")
            if isinstance(pos, int) and pos > 0:
                return f"Подключение очередь ({pos})", MUTED
            return "Подключение очередь (?)", MUTED
    except (AttributeError, TypeError):
        pass

    # жив ли процесс бота
    alive = False
    try:
        p = bot_proc
        alive = (p is not None and p.poll() is None)
    except (AttributeError, OSError, subprocess.SubprocessError):
        alive = False

    if not alive:
        return "Бот выключен", RED_HOVER

    st = read_state()
    status = (st.get("bot_status") or "").strip().lower()

    try:
        last_ts = int(st.get("bot_last_ts") or 0)
    except (TypeError, ValueError):
        last_ts = 0

    now = int(time.time())

    if status == "connected" and (now - last_ts) <= 20:
        return "Подключен", GREEN_HOVER

    # ВАЖНО: без "…"
    return "Подключение", MUTED

def should_skip_auth() -> bool:
    st = read_state()
    key = (st.get("key") or "").strip()
    key_for = (st.get("key_for") or "").strip()
    authorized = bool(st.get("authorized"))

    if not authorized:
        return False
    if not key or not key_for:
        return False
    if not key.startswith("XY-") or len(key) < 20:
        return False
    return True

# ===================== UI HELPERS =====================
def qcolor(hex_: str) -> QtGui.QColor:
    return QtGui.QColor(hex_)

class TelegramIcon(QtWidgets.QLabel):
    """Clickable TG icon with smooth hover scale."""
    clicked = QtCore.Signal()

    def __init__(self, pix: QtGui.QPixmap, base: int = 44, hover: int = 50, parent=None):
        super().__init__(parent)

        # ---- важно: фон самого QLabel делаем полностью прозрачным ----
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent;")
        self.setAlignment(QtCore.Qt.AlignCenter)

        self._base = int(base)
        self._hover = int(hover)
        self._pix = pix

        self.setFixedSize(self._base, self._base)
        self.setPixmap(self._pix.scaled(self._base, self._base, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.setCursor(QtGui.QCursor(Qt.PointingHandCursor))

        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(180)
        self._anim.valueChanged.connect(self._apply_size)

    def _apply_size(self, v):
        s = int(v)

        # держим прозрачность даже при ресайзе
        self.setStyleSheet("background: transparent;")

        self.setFixedSize(s, s)
        self.setPixmap(self._pix.scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def enterEvent(self, e):
        self._anim.stop()
        self._anim.setStartValue(self.width())
        self._anim.setEndValue(self._hover)
        self._anim.start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._anim.stop()
        self._anim.setStartValue(self.width())
        self._anim.setEndValue(self._base)
        self._anim.start()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)

class GlassPillButton(QtWidgets.QPushButton):
    def __init__(self, text: str, *, w: int = 200, h: int = 30, parent=None):
        super().__init__(text, parent)
        self.setFixedSize(w, h)
        self.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        self.setFocusPolicy(Qt.NoFocus)

        self._on = False
        self._hover = False
        self._pressed = False

        # ✅ фиксируем "основной" текст (его и будем рисовать всегда)
        self._fixed_text = str(text)

        # чтобы QSS не вмешивался
        self.setStyleSheet("background: transparent; border: none;")

        f = self.font()
        f.setPointSize(11 if h >= 34 else 10)
        f.setWeight(QtGui.QFont.Weight.Bold)
        self.setFont(f)

    # ✅ менять надпись правильно — через этот метод
    def set_fixed_text(self, text: str) -> None:
        self._fixed_text = str(text)
        # оставим и реальный текст QPushButton, чтобы tooltip/acc/sizeHint были адекватны
        super().setText(self._fixed_text)
        self.update()

    # ⚠️ если где-то в коде будет btn.setText("STOP") — на экране НЕ появится
    # (мы рисуем fixed_text), но "внутренний" текст Qt обновится — это нормально.
    def setText(self, text: str) -> None:
        super().setText(text)
        self.update()

    def set_on(self, on: bool):
        self._on = bool(on)
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        rect = self.rect()
        rr = QtCore.QRectF(rect).adjusted(0.9, 0.9, -0.9, -0.9)
        radius = rr.height() / 2.0

        path = QtGui.QPainterPath()
        path.addRoundedRect(rr, radius, radius)

        # Accent color
        if self._on:
            accent = QtGui.QColor(72, 230, 139)  # green
        else:
            accent = QtGui.QColor(255, 107, 90)  # red

        # tuning
        edge_alpha = 95 if self._hover else 78
        center_alpha = 10
        if self._pressed:
            edge_alpha = min(125, edge_alpha + 18)

        # 1) Base glass
        base = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        base.setColorAt(0.00, QtGui.QColor(255, 255, 255, 28))
        base.setColorAt(0.40, QtGui.QColor(24, 28, 40, 140))
        base.setColorAt(1.00, QtGui.QColor(16, 19, 27, 175))

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(base)
        p.drawPath(path)

        # 2) Edge ring inside
        p.save()
        p.setClipPath(path)

        cx, cy = rr.center().x(), rr.center().y()
        rad = max(rr.width(), rr.height()) * 0.62

        ring = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), rad)
        ring.setColorAt(0.00, QtGui.QColor(accent.red(), accent.green(), accent.blue(), center_alpha))
        ring.setColorAt(0.70, QtGui.QColor(accent.red(), accent.green(), accent.blue(), 18))
        ring.setColorAt(0.90, QtGui.QColor(accent.red(), accent.green(), accent.blue(), edge_alpha))
        ring.setColorAt(1.00, QtGui.QColor(accent.red(), accent.green(), accent.blue(), edge_alpha))

        p.setBrush(ring)
        p.drawPath(path)

        # 3) Soft top sheen
        sheen = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        sheen.setColorAt(0.00, QtGui.QColor(255, 255, 255, 26))
        sheen.setColorAt(0.22, QtGui.QColor(255, 255, 255, 10))
        sheen.setColorAt(0.55, QtGui.QColor(255, 255, 255, 0))
        p.setBrush(sheen)
        p.drawRoundedRect(QtCore.QRectF(rr.left(), rr.top(), rr.width(), rr.height() * 0.52), radius, radius)

        # 4) Inner depth bottom
        depth = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        depth.setColorAt(0.55, QtGui.QColor(0, 0, 0, 0))
        depth.setColorAt(1.00, QtGui.QColor(0, 0, 0, 55))
        p.setBrush(depth)
        p.drawPath(path)

        p.restore()

        # 5) Thin white rim
        white_rim = QtGui.QColor(255, 255, 255, 70 if self._hover else 55)
        p.setPen(QtGui.QPen(white_rim, 1.0))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawPath(path)

        # 6) Text (✅ рисуем фиксированный текст, а не self.text())
        p.setPen(QtGui.QColor(255, 255, 255, 238))
        p.drawText(rect, QtCore.Qt.AlignCenter, self._fixed_text)

        p.end()

class ToggleSwitch(QtWidgets.QWidget):
    """Glassy toggle switch (matches Card / Glass buttons)."""
    toggled = QtCore.Signal(bool)

    def __init__(self, initial: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedSize(64, 34)

        self._state = bool(initial)
        self._knob_x = 0.0
        self._vx = 0.0
        self._target = self._state
        self._silent = False

        self._anim_timer = QtCore.QTimer(self)
        self._anim_timer.timeout.connect(self._anim_step)
        self.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        self._update_positions(initial=True)

    @property
    def state(self) -> bool:
        return self._state

    def set_state(self, on: bool, silent: bool = False):
        self._target = bool(on)
        if self._target == self._state:
            return
        self._start_anim(silent=silent)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._target = not self._target
            self._start_anim(silent=False)
        super().mousePressEvent(e)

    def _update_positions(self, initial=False):
        left = 18.0
        right = self.width() - 18.0
        self._knob_x = right if self._state else left
        if initial:
            self._vx = 0.0
        self.update()

    def _start_anim(self, silent: bool):
        self._silent = bool(silent)
        self._vx = 0.0
        if not self._anim_timer.isActive():
            self._anim_timer.start(12)

    def _anim_step(self):
        left = 18.0
        right = self.width() - 18.0
        target_x = right if self._target else left

        k = 0.22
        damping = 0.72
        eps = 0.25

        dx = target_x - self._knob_x
        self._vx += dx * k
        self._vx *= damping
        self._knob_x += self._vx

        lo, hi = left, right
        if self._knob_x < lo - 6:
            self._knob_x = lo - 6
            self._vx *= 0.5
        elif self._knob_x > hi + 6:
            self._knob_x = hi + 6
            self._vx *= 0.5

        self.update()

        done = (abs(dx) <= eps and abs(self._vx) <= 0.20)
        if done:
            self._anim_timer.stop()
            self._state = bool(self._target)
            self._update_positions(initial=False)
            if not getattr(self, "_silent", False):
                self.toggled.emit(self._state)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        w, h = self.width(), self.height()
        rr = QtCore.QRectF(0.8, 0.8, w - 1.6, h - 1.6)
        radius = h / 2.0

        track_path = QtGui.QPainterPath()
        track_path.addRoundedRect(rr, radius, radius)

        # Accent color
        if self._target:
            accent = QtGui.QColor(72, 230, 139)  # green
        else:
            accent = QtGui.QColor(255, 107, 90)  # red

        hover = self.underMouse()

        # ✅ заметнее акцент (но всё ещё "glass")
        edge_alpha = 115 if hover else 95

        # ===== Track: base glass =====
        base = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        base.setColorAt(0.00, QtGui.QColor(255, 255, 255, 22))
        base.setColorAt(0.40, QtGui.QColor(24, 28, 40, 150))
        base.setColorAt(1.00, QtGui.QColor(16, 19, 27, 185))

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(base)
        p.drawPath(track_path)

        # ===== Track: accent ring INSIDE + mild center tint =====
        p.save()
        p.setClipPath(track_path)

        cx, cy = rr.center().x(), rr.center().y()
        rad = max(rr.width(), rr.height()) * 0.70

        ring = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), rad)
        # центр — чуть заметнее, чтобы не сливалось
        ring.setColorAt(0.00, QtGui.QColor(accent.red(), accent.green(), accent.blue(), 16))
        ring.setColorAt(0.60, QtGui.QColor(accent.red(), accent.green(), accent.blue(), 24))
        # край — основной акцент (чуть шире)
        ring.setColorAt(0.88, QtGui.QColor(accent.red(), accent.green(), accent.blue(), edge_alpha))
        ring.setColorAt(1.00, QtGui.QColor(accent.red(), accent.green(), accent.blue(), edge_alpha))

        p.setBrush(ring)
        p.drawPath(track_path)

        # Top sheen
        sheen = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        sheen.setColorAt(0.00, QtGui.QColor(255, 255, 255, 20))
        sheen.setColorAt(0.18, QtGui.QColor(255, 255, 255, 8))
        sheen.setColorAt(0.55, QtGui.QColor(255, 255, 255, 0))
        p.setBrush(sheen)
        p.drawRoundedRect(QtCore.QRectF(rr.left(), rr.top(), rr.width(), rr.height() * 0.55), radius, radius)

        # Bottom depth
        depth = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        depth.setColorAt(0.55, QtGui.QColor(0, 0, 0, 0))
        depth.setColorAt(1.00, QtGui.QColor(0, 0, 0, 60))
        p.setBrush(depth)
        p.drawPath(track_path)

        p.restore()

        # Thin neutral rim
        rim = QtGui.QColor(210, 215, 225, 55 if hover else 45)
        p.setPen(QtGui.QPen(rim, 1.0))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawPath(track_path)

        # ===== subtle color under knob (helps readability) =====
        under = QtGui.QRadialGradient(QtCore.QPointF(self._knob_x, h / 2), 18)
        under.setColorAt(0.00, QtGui.QColor(accent.red(), accent.green(), accent.blue(), 45))
        under.setColorAt(1.00, QtGui.QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.setBrush(under)
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(QtCore.QRectF(self._knob_x - 14, h/2 - 14, 28, 28))

        # ===== Knob: glass sphere =====
        knob_size = 26.0
        kcx = float(self._knob_x)
        kcy = h / 2.0
        krect = QtCore.QRectF(kcx - knob_size/2, kcy - knob_size/2, knob_size, knob_size)

        knob_path = QtGui.QPainterPath()
        knob_path.addEllipse(krect)

        # knob base
        kbase = QtGui.QLinearGradient(krect.topLeft(), krect.bottomLeft())
        kbase.setColorAt(0.00, QtGui.QColor(255, 255, 255, 235))
        kbase.setColorAt(0.55, QtGui.QColor(235, 238, 245, 235))
        kbase.setColorAt(1.00, QtGui.QColor(190, 195, 210, 235))

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(kbase)
        p.drawPath(knob_path)

        # knob highlight
        p.save()
        p.setClipPath(knob_path)
        kh = QtGui.QLinearGradient(krect.topLeft(), krect.bottomLeft())
        kh.setColorAt(0.00, QtGui.QColor(255, 255, 255, 120))
        kh.setColorAt(0.25, QtGui.QColor(255, 255, 255, 35))
        kh.setColorAt(0.55, QtGui.QColor(255, 255, 255, 0))
        p.setBrush(kh)
        p.drawEllipse(QtCore.QRectF(krect.left(), krect.top(), krect.width(), krect.height() * 0.70))
        p.restore()

        # knob rim
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 55), 1.0))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawEllipse(krect)

        p.end()

class Card(QtWidgets.QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(300, 190)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

        self._hover = False

        # тень (можно чуть мягче)
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 12)
        shadow.setColor(QtGui.QColor(0, 0, 0, 140))
        self.setGraphicsEffect(shadow)

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        r = self.rect()
        rr = QtCore.QRectF(r).adjusted(1.0, 1.0, -1.0, -1.0)
        radius = 18.0

        path = QtGui.QPainterPath()
        path.addRoundedRect(rr, radius, radius)

        # ===== 1) base glass (FLAT, NO GRADIENT) =====
        base_color = QtGui.QColor(20, 24, 34, 165)  # ровное матовое стекло
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(base_color)
        p.drawPath(path)

        p.save()
        p.setClipPath(path)

        # ===== 2) edge thickness =====
        cx, cy = rr.center().x(), rr.center().y()
        rad = max(rr.width(), rr.height()) * 0.74

        edge_dark_a = 70 if not self._hover else 85

        ring = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), rad)
        ring.setColorAt(0.00, QtGui.QColor(0, 0, 0, 0))
        ring.setColorAt(0.80, QtGui.QColor(0, 0, 0, 0))
        ring.setColorAt(0.92, QtGui.QColor(10, 12, 18, edge_dark_a))
        ring.setColorAt(1.00, QtGui.QColor(10, 12, 18, edge_dark_a))

        p.setBrush(ring)
        p.drawPath(path)

        # ===== 3) soft top highlight (очень слабый) =====
        spec = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        spec.setColorAt(0.00, QtGui.QColor(255, 255, 255, 22))
        spec.setColorAt(0.20, QtGui.QColor(255, 255, 255, 0))

        p.setBrush(spec)
        p.drawRoundedRect(
            QtCore.QRectF(rr.left(), rr.top(), rr.width(), rr.height() * 0.45),
            radius, radius
        )

        # ===== 4) inner depth bottom =====
        depth = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        depth.setColorAt(0.65, QtGui.QColor(0, 0, 0, 0))
        depth.setColorAt(1.00, QtGui.QColor(0, 0, 0, 65))
        p.setBrush(depth)
        p.drawPath(path)

        p.restore()

        # ===== 5) rim =====
        rim_a = 40 if not self._hover else 55
        p.setPen(QtGui.QPen(QtGui.QColor(200, 205, 215, rim_a), 1.0))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawPath(path)

        p.end()

class GlassCard(QtWidgets.QFrame):
    def __init__(self, w: int, h: int, parent=None):
        super().__init__(parent)
        self.setFixedSize(int(w), int(h))
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

        self._hover = False

        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 12)
        shadow.setColor(QtGui.QColor(0, 0, 0, 140))
        self.setGraphicsEffect(shadow)

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        r = self.rect()
        rr = QtCore.QRectF(r).adjusted(1.0, 1.0, -1.0, -1.0)
        radius = 18.0

        path = QtGui.QPainterPath()
        path.addRoundedRect(rr, radius, radius)

        # 1) base glass
        base_color = QtGui.QColor(20, 24, 34, 165)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(base_color)
        p.drawPath(path)

        p.save()
        p.setClipPath(path)

        # 2) edge thickness
        cx, cy = rr.center().x(), rr.center().y()
        rad = max(rr.width(), rr.height()) * 0.74

        edge_dark_a = 70 if not self._hover else 85

        ring = QtGui.QRadialGradient(QtCore.QPointF(cx, cy), rad)
        ring.setColorAt(0.00, QtGui.QColor(0, 0, 0, 0))
        ring.setColorAt(0.80, QtGui.QColor(0, 0, 0, 0))
        ring.setColorAt(0.92, QtGui.QColor(10, 12, 18, edge_dark_a))
        ring.setColorAt(1.00, QtGui.QColor(10, 12, 18, edge_dark_a))

        p.setBrush(ring)
        p.drawPath(path)

        # 3) soft top highlight
        spec = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        spec.setColorAt(0.00, QtGui.QColor(255, 255, 255, 22))
        spec.setColorAt(0.20, QtGui.QColor(255, 255, 255, 0))

        p.setBrush(spec)
        p.drawRoundedRect(
            QtCore.QRectF(rr.left(), rr.top(), rr.width(), rr.height() * 0.45),
            radius, radius
        )

        # 4) inner depth bottom
        depth = QtGui.QLinearGradient(rr.topLeft(), rr.bottomLeft())
        depth.setColorAt(0.65, QtGui.QColor(0, 0, 0, 0))
        depth.setColorAt(1.00, QtGui.QColor(0, 0, 0, 65))
        p.setBrush(depth)
        p.drawPath(path)

        p.restore()

        # 5) rim
        rim_a = 40 if not self._hover else 55
        p.setPen(QtGui.QPen(QtGui.QColor(200, 205, 215, rim_a), 1.0))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawPath(path)

        p.end()

class RotatingIconButton(QtWidgets.QAbstractButton):
    def __init__(self, icon_path: str, *, size: int = 46, parent=None):
        super().__init__(parent)
        self.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

        self._size = int(size)
        self.setFixedSize(self._size, self._size)

        self._pix = QtGui.QPixmap(icon_path) if (icon_path and os.path.exists(icon_path)) else QtGui.QPixmap()
        if not self._pix.isNull():
            self._pix = self._pix.scaled(
                self._size, self._size,
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )

        self._angle = 0.0

        # ✅ РОВНО 1 ОБОРОТ
        self._max_angle = 360.0
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(900)  # плавно
        self._anim.setEasingCurve(QtCore.QEasingCurve.InOutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)

    def _on_anim_value(self, v):
        try:
            self._angle = float(v)
        except (TypeError, ValueError):
            return
        self.update()

    def _start_anim(self, target: float):
        if self._anim.state() == QtCore.QAbstractAnimation.Running:
            self._anim.stop()
        self._anim.setStartValue(self._angle)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, e):
        # 🔄 0 → 360°
        self._start_anim(self._max_angle)
        super().enterEvent(e)

    def leaveEvent(self, e):
        # 🔄 360° → 0°
        self._start_anim(0.0)
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)

        if self._pix.isNull():
            p.setPen(QtGui.QColor(255, 255, 255, 220))
            p.drawText(self.rect(), Qt.AlignCenter, "⚙")
            return

        cx = self.width() / 2.0
        cy = self.height() / 2.0

        p.translate(cx, cy)
        p.rotate(self._angle)
        p.translate(-cx, -cy)

        x = (self.width() - self._pix.width()) // 2
        y = (self.height() - self._pix.height()) // 2
        p.drawPixmap(x, y, self._pix)

class NoCurrentInPopupComboBox(QtWidgets.QComboBox):
    """(1) скрывает текущий элемент в списке (2) делает popup непрозрачным, чтобы не просвечивал текст."""
    def showPopup(self):
        # 1) скрываем текущий выбранный ряд в списке
        v = self.view()
        idx = self.currentIndex()
        if idx >= 0:
            v.setRowHidden(idx, True)

        # 2) делаем окно popup непрозрачным (иначе видно текст комбобокса "под ним")
        try:
            w = v.window()  # это окно выпадающего списка
            w.setWindowOpacity(1.0)
            w.setAttribute(QtCore.Qt.WA_TranslucentBackground, False)

            # непрозрачный фон списка
            v.setStyleSheet(
                f"QListView{{background:{CARD}; color:{TEXT}; border:1px solid #2a2f3a;}}"
                f"QListView::item{{padding:6px 10px;}}"
                f"QListView::item:selected{{background:#202432;}}"
            )
        except Exception:
            pass

        super().showPopup()

    def hidePopup(self):
        # возвращаем скрытый ряд
        v = self.view()
        idx = self.currentIndex()
        if idx >= 0:
            v.setRowHidden(idx, False)
        super().hidePopup()

# ===================== MONITORS (Windows: model + res + Hz) =====================
def list_monitors_detailed() -> list[dict]:
    out: list[dict] = []

    # fallback (Qt only) — если не Windows/ctypes не смог
    def _qt_fallback() -> list[dict]:
        res = []
        try:
            screens = QtGui.QGuiApplication.screens()
            for i, s in enumerate(screens, start=1):
                g = s.geometry()
                hz = int(round(float(getattr(s, "refreshRate", lambda: 0.0)() or 0.0)))
                name = str(getattr(s, "name", lambda: "")() or "")
                res.append({
                    "index": i,
                    "device_name": name or f"SCREEN{i}",
                    "model": name or f"Monitor {i}",
                    "w": int(g.width()),
                    "h": int(g.height()),
                    "hz": hz or 0,
                })
        except Exception:
            pass
        return res

    if os.name != "nt":
        return _qt_fallback()

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", wintypes.WCHAR * 32),
                ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD),

                ("dmPositionX", wintypes.LONG),
                ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD),
                ("dmDisplayFixedOutput", wintypes.DWORD),

                ("dmColor", wintypes.SHORT),
                ("dmDuplex", wintypes.SHORT),
                ("dmYResolution", wintypes.SHORT),
                ("dmTTOption", wintypes.SHORT),
                ("dmCollate", wintypes.SHORT),
                ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD),

                ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD),
                ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD),
                ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD),
                ("dmPanningHeight", wintypes.DWORD),
            ]

        user32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wintypes.DWORD]
        user32.EnumDisplayDevicesW.restype = wintypes.BOOL

        user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
        user32.EnumDisplaySettingsW.restype = wintypes.BOOL

        EDS_CURRENT_SETTINGS = -1
        DISPLAY_DEVICE_ACTIVE = 0x00000001

        i = 0
        idx = 0
        while True:
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            ok = user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0)
            if not ok:
                break

            i += 1

            # только активные дисплеи
            if not (dd.StateFlags & DISPLAY_DEVICE_ACTIVE):
                continue

            idx += 1
            dev_name = str(dd.DeviceName)
            model = str(dd.DeviceString).strip() or dev_name

            dm = DEVMODEW()
            dm.dmSize = ctypes.sizeof(DEVMODEW)
            if user32.EnumDisplaySettingsW(dev_name, EDS_CURRENT_SETTINGS, ctypes.byref(dm)):
                w = int(dm.dmPelsWidth or 0)
                h = int(dm.dmPelsHeight or 0)
                hz = int(dm.dmDisplayFrequency or 0)
            else:
                w = h = hz = 0

            out.append({
                "index": idx,
                "device_name": dev_name,
                "model": model,
                "w": w,
                "h": h,
                "hz": hz,
            })

        if out:
            return out

    except Exception:
        pass

    return _qt_fallback()

def _cfg_read() -> dict:
    try:
        if os.path.exists(CFG_PATH):
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                j = json.load(f)
                return j if isinstance(j, dict) else {}
    except Exception:
        pass
    return {}

def _cfg_write(cfg: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setModal(True)
        self.setFixedSize(520, 230)
        self.setStyleSheet(f"background:{BG}; color:{TEXT};")

        self.monitors = list_monitors_detailed()

        cfg = _cfg_read()
        try:
            current_mon = int(cfg.get("monitor", 1) or 1)
        except Exception:
            current_mon = 1

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)

        title = QtWidgets.QLabel("Настройки")
        title.setStyleSheet(f"color:{TEXT}; font-weight:800; font-size:16px;")
        root.addWidget(title)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self.cb_all = NoCurrentInPopupComboBox()
        self.cb_all.setStyleSheet(
            f"QComboBox{{background:{CARD}; padding:8px; border-radius:10px;}}"
            f"QComboBox::drop-down{{border:none;}}"
        )
        self.cb_all.setCursor(QtGui.QCursor(Qt.PointingHandCursor))

        for m in self.monitors:
            hz_txt = f"{m['hz']} Hz" if int(m.get("hz") or 0) > 0 else "? Hz"
            text = f"#{m['index']}  {m['model']} — {m['w']}x{m['h']} @ {hz_txt}"
            self.cb_all.addItem(text, int(m["index"]))

        found = False
        for i in range(self.cb_all.count()):
            if int(self.cb_all.itemData(i) or 0) == current_mon:
                self.cb_all.setCurrentIndex(i)
                found = True
                break
        if not found:
            self.cb_all.setCurrentIndex(0)

        form.addRow("Монитор:", self.cb_all)
        root.addLayout(form)

        root.addStretch(1)

        # buttons
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)

        b_cancel = QtWidgets.QPushButton("Отмена")
        b_ok = QtWidgets.QPushButton("Сохранить")

        for b in (b_cancel, b_ok):
            b.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
            b.setFixedHeight(34)
            b.setStyleSheet(
                f"QPushButton{{background:{CARD}; border-radius:10px; padding:0 14px;}}"
                f"QPushButton:hover{{background:#202432;}}"
            )

        b_ok.setStyleSheet(
            f"QPushButton{{background:#1f3b2a; border-radius:10px; padding:0 14px;}}"
            f"QPushButton:hover{{background:#255033;}}"
        )

        b_cancel.clicked.connect(self.reject)
        b_ok.clicked.connect(self._on_save)

        btns.addWidget(b_cancel)
        btns.addWidget(b_ok)
        root.addLayout(btns)

    def _on_save(self):
        cfg = _cfg_read()
        if not isinstance(cfg, dict):
            cfg = {}

        cfg["monitor"] = int(self.cb_all.currentData() or 1)

        # (опционально) если раньше писали отдельные monitors — можно убрать, чтобы не путалось
        # cfg.pop("monitors", None)

        _cfg_write(cfg)
        self.accept()

class SettingsOverlay(QtWidgets.QFrame):
    """Оверлей настроек поверх MainWindow (без отдельного окна)."""
    def __init__(self, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: rgba(0,0,0,140);")
        self.hide()

        # ловим клики по фону (чтобы закрывать)
        self.setMouseTracking(True)

        # карточка
        self.card = GlassCard(520, 230, self)
        self.card.setCursor(QtGui.QCursor(Qt.ArrowCursor))

        root = QtWidgets.QVBoxLayout(self.card)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)

        title = QtWidgets.QLabel("Настройки")
        title.setStyleSheet(f"color:{TEXT}; font-weight:800; font-size:16px;")
        root.addWidget(title)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self.cb_all = NoCurrentInPopupComboBox()
        self.cb_all.setStyleSheet(
            f"QComboBox{{background:{CARD}; padding:8px; border-radius:10px;}}"
            f"QComboBox::drop-down{{border:none;}}"
        )
        self.cb_all.setCursor(QtGui.QCursor(Qt.PointingHandCursor))

        form.addRow("Монитор:", self.cb_all)
        root.addLayout(form)

        root.addStretch(1)

        # кнопки
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)

        self.b_cancel = QtWidgets.QPushButton("Отмена")
        self.b_ok = QtWidgets.QPushButton("Сохранить")

        # ✅ фиксируем одинаковые размеры, чтобы кнопка не "прыгала"
        for b in (self.b_cancel, self.b_ok):
            b.setFixedHeight(34)
            b.setMinimumWidth(120)
            b.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)

        self._initial_monitor = None

        self.b_cancel.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        self.b_ok.setCursor(QtGui.QCursor(Qt.PointingHandCursor))

        self.b_cancel.clicked.connect(self.hide_overlay)
        self.b_ok.clicked.connect(self.save_and_close)

        self.b_cancel.setStyleSheet(
            f"QPushButton{{background:{CARD}; color:{TEXT}; padding:0 14px; border-radius:10px; border:none; font-weight:700;}}"
            f"QPushButton:hover{{background:#202534;}}"
        )

        # ✅ единый каркас стилей для OK будет выставляться в _update_save_state()
        self.cb_all.currentIndexChanged.connect(self._update_save_state)

        # стартовое состояние (серая)
        self.b_ok.setEnabled(False)
        self._update_save_state()

        btns.addWidget(self.b_cancel)
        btns.addSpacing(10)
        btns.addWidget(self.b_ok)

        root.addLayout(btns)

        self.monitors = []

    def refresh(self):
        self.monitors = list_monitors_detailed()

        cfg = _cfg_read()
        try:
            current_mon = int(cfg.get("monitor", 1) or 1)
        except Exception:
            current_mon = 1

        self._initial_monitor = current_mon

        self.cb_all.blockSignals(True)
        try:
            self.cb_all.clear()
            for m in self.monitors:
                hz_txt = f"{m['hz']} Hz" if int(m.get("hz") or 0) > 0 else "? Hz"
                text = f"#{m['index']}  {m['model']} — {m['w']}x{m['h']} @ {hz_txt}"
                self.cb_all.addItem(text, int(m["index"]))

            for i in range(self.cb_all.count()):
                if int(self.cb_all.itemData(i) or 0) == current_mon:
                    self.cb_all.setCurrentIndex(i)
                    break
        finally:
            self.cb_all.blockSignals(False)

        # ✅ обновляем состояние кнопки "Сохранить"
        self._update_save_state()

    def show_overlay(self):
        # ✅ blur основного меню
        try:
            mw = self.window()
            if hasattr(mw, "set_menu_blur"):
                mw.set_menu_blur(True)
        except Exception:
            pass

        self.refresh()
        self.setGeometry(0, 0, self.parent().width(), self.parent().height())
        self._center_card()
        self.raise_()
        self.show()

    def _update_save_state(self):
        try:
            cur = int(self.cb_all.currentData() or 0)
            changed = (self._initial_monitor is not None and cur != self._initial_monitor)
        except Exception:
            changed = False

        self.b_ok.setEnabled(changed)

        # общий каркас (одинаковый размер/паддинги/бордер для обоих состояний)
        base = "padding:0 14px; border-radius:10px; border:none; font-weight:700;"

        if changed:
            self.b_ok.setStyleSheet(
                f"QPushButton{{{base} background:{GREEN}; color:#0b0e14;}}"
                f"QPushButton:hover{{background:{GREEN_HOVER};}}"
                f"QPushButton:disabled{{{base} background:#2a2f3a; color:#777;}}")
        else:
            self.b_ok.setStyleSheet(
                f"QPushButton{{{base} background:#2a2f3a; color:#777;}}"
                f"QPushButton:hover{{background:#2a2f3a;}}"
                f"QPushButton:disabled{{{base} background:#2a2f3a; color:#777;}}")

    def hide_overlay(self):
        # ✅ unblur основного меню
        try:
            mw = self.window()
            if hasattr(mw, "set_menu_blur"):
                mw.set_menu_blur(False)
        except Exception:
            pass

        self.hide()

    def _center_card(self):
        x = self.width() // 2 - self.card.width() // 2
        y = self.height() // 2 - self.card.height() // 2
        self.card.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_card()

    def mousePressEvent(self, e):
        # клик по затемнению закрывает, клик по карточке — нет
        if not self.card.geometry().contains(e.position().toPoint()):
            self.hide_overlay()
        super().mousePressEvent(e)

    def save_and_close(self):
        # ✅ если ничего не меняли — не сохраняем
        try:
            if not self.b_ok.isEnabled():
                return
        except Exception:
            pass

        try:
            mon = int(self.cb_all.currentData() or 1)
        except Exception:
            mon = 1

        cfg = _cfg_read()
        cfg["monitor"] = int(mon)
        _cfg_write(cfg)

        # ✅ после сохранения считаем это новым "текущим"
        self._initial_monitor = int(mon)
        self._update_save_state()

        self.hide_overlay()

# ===================== AUTH DIALOG =====================
class AuthDialog(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(" ")
        self.setFixedSize(800, 450)
        self.setModal(True)
        self.setStyleSheet(f"background:{BG};")

        # важно: гарантируем, что все виджеты наследуют шрифт приложения
        try:
            self.setFont(QtWidgets.QApplication.font())
        except (AttributeError, RuntimeError, TypeError):
            pass

        # background movie
        self.bg = QtWidgets.QLabel(self)
        self.bg.setGeometry(0, 0, 800, 450)
        self.bg.setScaledContents(True)

        self._bg_movie: Optional[QtGui.QMovie] = None
        if os.path.exists(AUTH_VIDEO):
            mv = QtGui.QMovie(AUTH_VIDEO)
            mv.setCacheMode(QtGui.QMovie.CacheAll)
            mv.setScaledSize(QtCore.QSize(800, 450))
            self._bg_movie = mv
            self.bg.setMovie(mv)
            mv.start()
        else:
            self.bg.setStyleSheet(f"background:{BG};")

        # veil
        self.veil = QtWidgets.QFrame(self)
        self.veil.setGeometry(0, 0, 800, 450)
        self.veil.setStyleSheet("background: rgba(0,0,0,110);")

        # beta label
        self.beta = QtWidgets.QLabel("Beta Version", self)
        self.beta.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.beta.setStyleSheet(
            "background: transparent; color:#ff9f1a; font-weight:700; font-size:12px;")
        self.beta.move(5, -5)
        self.beta.raise_()

        # --------- TITLE (без RichText, чтобы шрифт точно применялся) ---------
        self.title_wrap = QtWidgets.QWidget(self)
        self.title_wrap.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.title_wrap.setStyleSheet("background: transparent;")

        title_row = QtWidgets.QHBoxLayout(self.title_wrap)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(0)

        self.title_left = QtWidgets.QLabel("Введите свой @username ", self.title_wrap)
        self.title_left.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.title_left.setStyleSheet(
            f"background: transparent; color:{TEXT}; font-size:16px; font-weight:700;")

        self.title_right = QtWidgets.QLabel("telegram", self.title_wrap)
        self.title_right.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.title_right.setStyleSheet(
            "background: transparent; color:#36a1d4; font-size:16px; font-weight:700;")

        title_row.addWidget(self.title_left)
        title_row.addWidget(self.title_right)

        self.title_wrap.adjustSize()
        self.title_wrap.move(self.width() // 2 - self.title_wrap.width() // 2, 48)
        self.title_wrap.raise_()
        # ---------------------------------------------------------------------

        # animations (write/unlock)
        self.anim = QtWidgets.QLabel(self)
        self.anim.setFixedSize(80, 80)
        self.anim.move(self.width() // 2 - 40, 125 - 40)
        self.anim.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.anim.setStyleSheet("background: transparent;")

        self._write_movie: Optional[QtGui.QMovie] = None
        self._unlock_movie: Optional[QtGui.QMovie] = None

        if os.path.exists(WRITE_WEBP):
            m = QtGui.QMovie(WRITE_WEBP)
            m.setCacheMode(QtGui.QMovie.CacheAll)
            m.setScaledSize(QtCore.QSize(80, 80))
            m.setSpeed(100)
            self._write_movie = m

        if os.path.exists(UNLOCK_WEBP):
            m = QtGui.QMovie(UNLOCK_WEBP)
            m.setCacheMode(QtGui.QMovie.CacheAll)
            m.setScaledSize(QtCore.QSize(80, 80))
            m.setSpeed(100)
            self._unlock_movie = m

        if self._write_movie:
            self.anim.setMovie(self._write_movie)
            self._write_movie.start()

        # entry
        self.edit = QtWidgets.QLineEdit(self)
        self.edit.setPlaceholderText("@username")
        self.edit.setFixedSize(350, 46)
        self.edit.move(225, 205)
        self.edit.setStyleSheet(f"""
            QLineEdit {{
                background:{ENTRY_BG};
                color:{TEXT};
                border: none;
                border-radius: 14px;
                padding-left: 12px;
                padding-right: 12px;
                font-size: 14px;
                font-weight: 500;
            }}
            QLineEdit:focus {{
                outline: none;
            }}
            QLineEdit::placeholder {{
                color: rgba(230,230,235,120);
            }}
        """)
        self.edit.returnPressed.connect(self._on_enter)

        # ✅ Валидация username "на лету"
        self.edit.textChanged.connect(self._on_username_changed)

        # button
        self.btn = QtWidgets.QPushButton("Продолжить", self)
        self.btn.setFixedSize(260, 46)
        self.btn.move(270, 270)
        self.btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        self.btn.clicked.connect(self._on_button)

        # ---- smooth enable/disable for "Продолжить" ----
        self._btn_enabled = False
        self._btn_col_enabled = QtGui.QColor(GREEN)
        self._btn_col_disabled = QtGui.QColor("#2a3347")

        r = self.btn.height() // 2
        self._btn_style_tpl = f"""
            QPushButton {{
                background: {{bg}};
                color: white;
                border: none;
                border-radius: {r}px;
                font-weight: 700;
                font-size: 12px;
                letter-spacing: 0.2px;
            }}
            QPushButton:hover {{
                background:{GREEN_HOVER};
            }}
            QPushButton:pressed {{
                background:{GREEN_HOVER};
                padding-top: 1px;
            }}
            QPushButton:disabled {{
                background:#2a3347;
                color:#80838f;
            }}
            QPushButton:focus {{
                outline: none;
            }}
        """

        self._btn_anim = QtCore.QVariantAnimation(self)
        self._btn_anim.setDuration(160)
        self._btn_anim.valueChanged.connect(self._apply_btn_color)

        # ✅ ВАЖНО: finished подключаем ОДИН РАЗ, без disconnect() (чтобы не было RuntimeWarning)
        self._btn_disable_on_finish = False
        self._btn_anim.finished.connect(self._on_btn_anim_finished)

        # старт: выключена + серый фон
        self.btn.setEnabled(False)
        self._apply_btn_color(self._btn_col_disabled)

        # status text (под кнопкой)
        self.status = QtWidgets.QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.status.setGeometry(40, 325, 720, 70)
        self.status.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.status.setStyleSheet(
            "background: transparent; color:#a9acb6; font-size:12px; font-weight:600;"
        )
        self.status.raise_()

        # bot status (низ слева)  ✅ FIX: чтобы не обрезало ".." и "..."
        self.bot_status = QtWidgets.QLabel("", self)
        self.bot_status.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.bot_status.setStyleSheet(
            "background: transparent; color:#a9acb6; font-size:12px; font-weight:700;"
        )
        self.bot_status.setTextFormat(Qt.PlainText)
        self.bot_status.setWordWrap(False)
        self.bot_status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.bot_status.move(12, 425)

        # главное: дать ширину, иначе QLabel может быть слишком узким и обрежет текст
        self.bot_status.setFixedWidth(420)  # можно 360/420/500 — под твой текст с "очередь ..."

        self.bot_status.raise_()

        # tg icon
        if os.path.exists(tg_path):
            pix = QtGui.QPixmap(tg_path)
            self.tg = TelegramIcon(pix, base=44, hover=50, parent=self)
            self.tg.clicked.connect(self._open_tg)
            self._reposition_tg()
        else:
            self.tg = None

        # state
        self._step = "username"
        self._username = ""

        # timers
        self._timer_bot = QtCore.QTimer(self)
        self._timer_bot.timeout.connect(self._tick_bot_status)
        self._timer_bot.start(500)

        self._timer_queue: Optional[QtCore.QTimer] = None

        self._tick_bot_status()
        self._apply_titlebar()

        # ✅ На старте: кнопка выключена (пока не введут валидный username)
        self._on_username_changed(self.edit.text())

    # ---------- UI chrome ----------
    def _apply_titlebar(self):
        try:
            set_titlebar_colors_qt(
                self,
                caption="#0f1117",
                text="#ffffff",
                border="#0f1117",
                dark=True,
                disable_backdrop=True
            )
        except (OSError, AttributeError, RuntimeError, TypeError):
            pass

    def resizeEvent(self, e):
        try:
            self.bg.setGeometry(0, 0, self.width(), self.height())
            self.veil.setGeometry(0, 0, self.width(), self.height())
            if getattr(self, "_bg_movie", None) is not None:
                self._bg_movie.setScaledSize(QtCore.QSize(self.width(), self.height()))
        except Exception:
            pass

        # ⬇️ добавь это
        try:
            self._reposition_settings_btn()
        except Exception:
            pass

        super().resizeEvent(e)

    def _reposition_tg(self):
        if self.tg is None:
            return
        pad = 12
        self.tg.move(self.width() - self.tg.width() - pad, self.height() - self.tg.height() - pad)

    def _open_tg(self):
        import webbrowser
        webbrowser.open("https://t.me/AFK_Game_bot")

    # ---------- status helpers ----------
    def _set_status(self, msg: str):
        self.status.setText(msg)
        self.status.raise_()

    # ---------- smooth button helpers ----------
    def _apply_btn_color(self, color: QtGui.QColor | object) -> None:
        # QVariantAnimation иногда отдаёт не QColor — пробуем привести
        try:
            if not isinstance(color, QtGui.QColor):
                color = QtGui.QColor(color)
        except (TypeError, ValueError):
            return

        try:
            bg = color.name()
            self.btn.setStyleSheet(self._btn_style_tpl.format(bg=bg))
        except (AttributeError, RuntimeError, KeyError):
            pass

    def _on_btn_anim_finished(self) -> None:
        # Выключаем кнопку ТОЛЬКО если в этот момент нужно (disable-анимация завершилась)
        if not getattr(self, "_btn_disable_on_finish", False):
            return
        self._btn_disable_on_finish = False
        try:
            self.btn.setEnabled(False)
        except (AttributeError, RuntimeError):
            pass

    def _set_continue_enabled_smooth(self, enable: bool) -> None:
        enable = bool(enable)

        # на шаге key всегда активна
        if getattr(self, "_step", "") != "username":
            self._btn_enabled = True
            self._btn_disable_on_finish = False
            try:
                self.btn.setEnabled(True)
            except (AttributeError, RuntimeError):
                pass
            self._apply_btn_color(self._btn_col_enabled)
            return

        if enable == getattr(self, "_btn_enabled", False):
            return

        self._btn_enabled = enable

        try:
            self._btn_anim.stop()
        except (AttributeError, RuntimeError):
            pass

        if enable:
            # сначала включаем, чтобы :disabled не перебивал цвет
            self._btn_disable_on_finish = False
            try:
                self.btn.setEnabled(True)
            except (AttributeError, RuntimeError):
                pass

            try:
                self._btn_anim.setStartValue(self._btn_col_disabled)
                self._btn_anim.setEndValue(self._btn_col_enabled)
                self._btn_anim.start()
            except (AttributeError, RuntimeError, TypeError):
                self._apply_btn_color(self._btn_col_enabled)
            return

        # disable branch
        self._btn_disable_on_finish = True
        try:
            self._btn_anim.setStartValue(self._btn_col_enabled)
            self._btn_anim.setEndValue(self._btn_col_disabled)
            self._btn_anim.start()
        except (AttributeError, RuntimeError, TypeError):
            # fallback: сразу выключаем
            self._btn_disable_on_finish = False
            try:
                self.btn.setEnabled(False)
            except (AttributeError, RuntimeError):
                pass
            self._apply_btn_color(self._btn_col_disabled)

    # ---------- validation ----------
    def _on_username_changed(self, text: str) -> None:
        if getattr(self, "_step", "") != "username":
            self._set_continue_enabled_smooth(True)
            return

        raw = (text or "").strip()
        norm = normalize_username(raw)
        ok = looks_like_tg_username(norm)

        self._set_continue_enabled_smooth(bool(ok))

        if not raw:
            self._set_status("")
            return

        if not ok:
            self._set_status("Неверный username.")
        else:
            self._set_status("")

    def _tick_bot_status(self):
        txt, color = bot_ui_status()

        # крутилка "Подключение / Подключение. / Подключение.. / Подключение..."
        if isinstance(txt, str) and txt.startswith("Подключение"):
            _bot_dots["i"] = (_bot_dots["i"] + 1) % 4
            dots = ("", ".", "..", "...")[_bot_dots["i"]]

            # если есть "очередь (...)" — сохраняем хвост
            tail = ""
            idx = txt.find("очередь")
            if idx != -1:
                tail = " " + txt[idx:]

            txt = "Подключение" + dots + tail
        else:
            _bot_dots["i"] = 0

        # preauth countdown
        try:
            active = bool(_preauth.get("active"))
        except (AttributeError, TypeError):
            active = False

        if active:
            try:
                deadline = float(_preauth.get("deadline", 0.0))
            except (TypeError, ValueError):
                deadline = 0.0

            remain = int(deadline - time.time())
            if 0 < remain <= 10:
                color = "#ffb84d"
                txt = f"{txt}  (осталось {remain}с)"
            elif remain <= 0:
                _preauth["active"] = False
                _preauth["deadline"] = 0.0

        try:
            self.bot_status.setText(txt)
            self.bot_status.setStyleSheet(
                f"background: transparent; color:{color}; font-size:12px; font-weight:700;"
            )
            self.bot_status.raise_()
        except (AttributeError, RuntimeError):
            pass

    # ---------- flow ----------
    def _on_enter(self):
        if self._step == "username":
            self._submit_username()
        else:
            self._verify_key()

    def _on_button(self):
        self._on_enter()

    def _set_step_username(self):
        self._step = "username"

        self.title_left.setText("Введите свой @username ")
        self.title_right.setText("telegram")
        self.title_wrap.adjustSize()
        self.title_wrap.move(self.width() // 2 - self.title_wrap.width() // 2, 48)

        self.edit.clear()
        self.edit.setPlaceholderText("@username")
        self.btn.setText("Продолжить")

        # вернуть write-анимацию: стопнуть unlock
        try:
            m = getattr(self, "_unlock_movie", None)
            if m is not None:
                m.stop()
        except (AttributeError, RuntimeError):
            pass

        # включить write
        try:
            m2 = getattr(self, "_write_movie", None)
            if m2 is not None:
                self.anim.setMovie(m2)
                m2.start()
        except (AttributeError, RuntimeError):
            pass

        # остановить polling ключа
        try:
            t = getattr(self, "_timer_queue", None)
            if t is not None:
                t.stop()
        except (AttributeError, RuntimeError):
            pass

        # ✅ заново применить валидацию
        self._on_username_changed(self.edit.text())

    def _set_step_key(self):
        self._step = "key"

        self.title_left.setText("Введите ключ доступа")
        self.title_right.setText("")
        self.title_wrap.adjustSize()
        self.title_wrap.move(self.width() // 2 - self.title_wrap.width() // 2, 48)

        self.edit.clear()
        self.edit.setPlaceholderText("ключ доступа")
        self.btn.setText("Проверить ключ")

        # stop write animation
        try:
            m = getattr(self, "_write_movie", None)
            if m is not None:
                m.stop()
        except (AttributeError, RuntimeError):
            pass

        # start unlock animation
        try:
            m2 = getattr(self, "_unlock_movie", None)
            if m2 is not None:
                self.anim.setMovie(m2)
                m2.start()
        except (AttributeError, RuntimeError):
            pass

        # ✅ на шаге ключа кнопка всегда активна
        self._set_continue_enabled_smooth(True)

    def _close_success(self) -> None:
        # stop key poll timer (if any)
        try:
            t = getattr(self, "_timer_queue", None)
            if t is not None:
                t.stop()
        except (AttributeError, RuntimeError):
            pass

        _preauth["active"] = False
        _preauth["deadline"] = 0.0

        self.accept()

    def _poll_for_key_from_bot(self) -> None:
        if _app_closing:
            return

        expected = normalize_username(getattr(self, "_username", "") or "")
        st = read_state()

        if not expected:
            expected = normalize_username(st.get("expected_username", ""))

        key = (st.get("key") or "").strip()
        key_for = normalize_username(st.get("key_for") or "")

        pending = st.get("pending") if isinstance(st.get("pending"), dict) else {}
        p_status = (pending.get("status") or "").strip().lower()
        p_user = normalize_username(pending.get("username") or "")

        if key and key_for and key_for == expected:
            self._set_status("✅ Ключ выдан. Вставьте его в поле и нажмите «Проверить ключ».")
            try:
                t = getattr(self, "_timer_queue", None)
                if t is not None:
                    t.stop()
            except (AttributeError, RuntimeError):
                pass
            return

        if pending and (not p_user or p_user == expected):
            if p_status == "pending":
                self._set_status("⏳ Запрос отправлен администратору. Ожидайте выдачи ключа…")
                return
            if p_status == "denied":
                self._set_status("❌ Администратор отказал. Вы можете повторить запрос в боте командой /key.")
                return

        self._set_status("@AFK_Game_bot → отправьте /start")

    def _verify_key(self) -> None:
        entered = (self.edit.text() or "").strip()
        if not entered:
            self._set_status("Вставьте ключ доступа.")
            return

        st = read_state()
        expected = normalize_username(getattr(self, "_username", "") or st.get("expected_username", ""))
        key = (st.get("key") or "").strip()
        key_for = normalize_username(st.get("key_for") or "")

        if not expected:
            self._set_status("Сначала введите @username.")
            self._set_step_username()
            return

        if not key or not key_for or key_for != expected:
            self._set_status("Ключ для этого пользователя ещё не выдан. Запросите его в боте: /key.")
            return

        if entered != key:
            self._set_status("❌ Неверный ключ. Проверьте и попробуйте снова.")
            return

        st["authorized"] = True
        st["authorized_for"] = expected
        st["authorized_at"] = int(time.time())
        write_state(st)

        self._set_status("✅ Успешно! Открываю приложение…")
        QtCore.QTimer.singleShot(150, self._close_success)

    def _submit_username(self) -> None:
        raw = (self.edit.text() or "").strip()
        un = normalize_username(raw)

        if not un:
            self._set_status("Введите ваш Telegram @username.")
            self._set_continue_enabled_smooth(False)
            return

        if not looks_like_tg_username(un):
            self._set_status("Неверный username.")
            self._set_continue_enabled_smooth(False)
            return

        self._username = un

        st = read_state()
        prev_expected = normalize_username(st.get("expected_username") or "")
        st["expected_username"] = un

        # reset authorization if username changed
        if prev_expected != un:
            st.pop("key", None)
            st.pop("key_for", None)
            st.pop("pending", None)
            st["authorized"] = False

        write_state(st)

        # ensure local bot running
        try:
            threading.Thread(target=_start_local_bot, daemon=True).start()
        except Exception:
            pass

        self._set_step_key()
        self._set_status("⏳ Ожидаю ключ…")

        # timer for polling state
        try:
            if getattr(self, "_timer_queue", None) is None:
                self._timer_queue = QtCore.QTimer(self)
                self._timer_queue.timeout.connect(self._poll_for_key_from_bot)
            self._timer_queue.start(800)
        except (AttributeError, RuntimeError, TypeError):
            pass

# ===================== MAIN WINDOW =====================
def _http_get_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "XyesosBeta",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _parse_ver(v: str):
    # "1.2.3" -> (1,2,3)
    parts = []
    for x in (v or "").strip().split("."):
        try:
            parts.append(int(x))
        except Exception:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])

def is_newer(remote: str, local: str) -> bool:
    return _parse_ver(remote) > _parse_ver(local)

def ensure_updater_exists() -> str:
    # %LOCALAPPDATA%\XyesosBeta\updater.exe
    app_dir = get_app_data_dir("XyesosBeta")
    dst = os.path.join(app_dir, UPDATER_LOCAL_NAME)
    if os.path.exists(dst) and os.path.getsize(dst) > 50_000:
        return dst

    # берём из ресурсов (_MEIPASS) или рядом с .py
    src = os.path.join(res_dir(), UPDATER_BUNDLED_NAME)
    if not os.path.exists(src):
        # на dev-режиме можно разрешить updater рядом с проектом
        src2 = os.path.join(exe_dir(), UPDATER_BUNDLED_NAME)
        if os.path.exists(src2):
            src = src2

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return dst

def run_updater_and_exit(download_url: str):
    upd = ensure_updater_exists()

    target = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
    pid = os.getpid()

    # запускаем updater -> он подождёт пока GUI умрёт, заменит exe и запустит новый
    subprocess.Popen(
        [upd, "--pid", str(pid), "--target", target, "--url", download_url],
        close_fds=True,
        cwd=os.path.dirname(os.path.abspath(target)),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # закрываем текущее приложение
    try:
        QtWidgets.QApplication.quit()
    except Exception:
        os._exit(0)

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        # ✅ Вариант 1: убираем системное меню (и место под иконку слева)
        flags = self.windowFlags()
        flags &= ~Qt.WindowSystemMenuHint
        flags |= (Qt.WindowTitleHint |
                  Qt.WindowCloseButtonHint |
                  Qt.WindowMinimizeButtonHint)
        self.setWindowFlags(flags)

        self.setWindowTitle(" ")
        self.setFixedSize(800, 450)
        self.setStyleSheet(f"background:{BG}; color:{TEXT};")

        # ✅ Ставим иконку окна (твоя прозрачная Main.png)
        try:
            if os.path.exists(MAIN_ICON):
                self.setWindowIcon(QtGui.QIcon(MAIN_ICON))
            elif os.path.exists(ORANGE_ICON):
                self.setWindowIcon(QtGui.QIcon(ORANGE_ICON))
        except Exception:
            pass

        # capture mode
        self.capture_mode: Optional[str] = None

        # ---------- animated background (same as AuthDialog) ----------
        self.bg = QtWidgets.QLabel(self)
        self.bg.setGeometry(0, 0, 800, 450)
        self.bg.setScaledContents(True)

        self._bg_movie: Optional[QtGui.QMovie] = None
        if os.path.exists(AUTH_VIDEO):
            mv = QtGui.QMovie(AUTH_VIDEO)
            mv.setCacheMode(QtGui.QMovie.CacheAll)
            mv.setScaledSize(QtCore.QSize(800, 450))
            self._bg_movie = mv
            self.bg.setMovie(mv)
            mv.start()
        else:
            self.bg.setStyleSheet(f"background:{BG};")

        self.veil = QtWidgets.QFrame(self)
        self.veil.setGeometry(0, 0, 800, 450)
        self.veil.setStyleSheet("background: rgba(0,0,0,110);")

        self.bg.lower()
        self.veil.raise_()
        # ------------------------------------------------------------

        # beta label (top-left)
        self.beta = QtWidgets.QLabel("Beta Version", self)
        self.beta.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.beta.setStyleSheet("background: transparent; color:#ff9f1a; font-weight:700; font-size:12px;")
        self.beta.move(10, 0)
        self.beta.raise_()

        # ✅ КНОПКА ОБНОВЛЕНИЯ (по умолчанию скрыта)
        self._update_info: dict | None = None
        self.update_btn = GlassPillButton("Обновить", w=170, h=30, parent=self)
        self.update_btn.hide()
        self.update_btn.clicked.connect(self._on_update_clicked)
        self.update_btn.raise_()
        # ------------------------------------------------------------

        # central widget (transparent so video is visible)
        cw = QtWidgets.QWidget(self)
        cw.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        cw.setStyleSheet("background: transparent;")
        self.setCentralWidget(cw)

        # ✅ BLUR прослойка (блюрим СКРИН ВСЕГО ОКНА self.grab())
        self._blur_layer = QtWidgets.QLabel(self)
        self._blur_layer.setGeometry(self.rect())  # весь экран окна
        self._blur_layer.setScaledContents(False)
        self._blur_layer.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._blur_layer.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._blur_layer.hide()
        self._blur_layer.raise_()

        self._blur_opacity = QtWidgets.QGraphicsOpacityEffect(self._blur_layer)
        self._blur_opacity.setOpacity(0.0)
        self._blur_layer.setGraphicsEffect(self._blur_opacity)

        self._blur_fade = QtCore.QPropertyAnimation(self._blur_opacity, b"opacity", self)
        self._blur_fade.setDuration(140)
        self._blur_fade.setEasingCurve(QtCore.QEasingCurve.OutCubic)

        # ------------------------------------------------------------

        # absolute layout via QGrid for similar look
        grid = QtWidgets.QGridLayout(cw)
        grid.setContentsMargins(18, 18, 18, 18)
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(28)

        # cards
        self.card_orange = Card()
        self.card_right = Card()
        self.card_fish = Card()

        grid.addWidget(self.card_orange, 0, 0, Qt.AlignTop)
        grid.addWidget(self.card_right, 1, 0, Qt.AlignTop)
        grid.addWidget(self.card_fish, 0, 1, Qt.AlignTop)

        # Orange content
        self._build_orange_card()

        # Right content (belt + afk + sila)
        self._build_right_card()

        # Fish content
        self._build_fish_card()

        # ---------- settings icon (bottom-right overlay) ----------
        self.SETTINGS_ICON = r"E:\Python Project's\GTA_RP_CAR\Settings.png"
        self._settings_size = 46

        self.settings_btn = RotatingIconButton(self.SETTINGS_ICON, size=self._settings_size, parent=self)
        self.settings_btn.clicked.connect(self.open_settings)
        self.settings_btn.raise_()
        self._reposition_settings_btn()
        # ---------------------------------------------------------

        # ✅ ОВЕРЛЕЙ НАСТРОЕК (поверх главного окна, без отдельного окна)
        self.settings_overlay = SettingsOverlay(self)
        self.settings_overlay.setGeometry(0, 0, self.width(), self.height())
        self.settings_overlay.hide()
        self.settings_overlay.raise_()
        # ---------------------------------------------------------

        # Poll timer
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start(250)

        # apply titlebar colors (best effort)
        QtCore.QTimer.singleShot(0, self._apply_titlebar)
        QtCore.QTimer.singleShot(200, self._apply_titlebar)

        self._last_belt_toggle = 0.0

        # ✅ ПРОВЕРКА ОБНОВЛЕНИЙ (не блокирует UI)
        QtCore.QTimer.singleShot(450, self._check_updates_async)

    # -------------------- UPDATE HELPERS --------------------
    def _parse_ver(self, v: str) -> tuple[int, int, int]:
        s = (v or "").strip()
        parts = s.split(".")
        out = []
        for x in parts[:3]:
            try:
                out.append(int(x))
            except Exception:
                out.append(0)
        while len(out) < 3:
            out.append(0)
        return (out[0], out[1], out[2])

    def _is_newer(self, remote: str, local: str) -> bool:
        return self._parse_ver(remote) > self._parse_ver(local)

    def _http_get_json(self, url: str) -> dict:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "XyesosBeta",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=7) as r:
            raw = r.read().decode("utf-8", errors="replace")
        j = json.loads(raw)
        return j if isinstance(j, dict) else {}

    def _ensure_updater_exists(self) -> str:
        """
        updater.exe должен жить в %LOCALAPPDATA%\\XyesosBeta\\updater.exe (DATA_DIR).
        Если его там нет — копируем из ресурсов (RES_DIR, т.е. _MEIPASS).
        """
        os.makedirs(DATA_DIR, exist_ok=True)
        dst = os.path.join(DATA_DIR, "updater.exe")

        if os.path.exists(dst) and os.path.getsize(dst) > 50_000:
            return dst

        src = os.path.join(RES_DIR, "updater.exe")
        if not os.path.exists(src):
            # fallback (если в dev рядом)
            src2 = os.path.join(EXE_DIR, "updater.exe")
            if os.path.exists(src2):
                src = src2

        if not os.path.exists(src):
            raise FileNotFoundError("updater.exe not found (не добавлен в сборку через --add-data/--add-binary)")

        shutil.copy2(src, dst)
        return dst

    def _run_updater_and_quit(self, download_url: str) -> None:
        upd = self._ensure_updater_exists()
        target_exe = sys.executable  # текущий XyesosBeta.exe
        pid = os.getpid()
        cwd = os.path.dirname(os.path.abspath(target_exe))

        subprocess.Popen(
            [upd, "--pid", str(pid), "--target", target_exe, "--url", str(download_url)],
            cwd=cwd,
            close_fds=True,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        QtWidgets.QApplication.quit()

    def _set_update_available(self, info: dict | None) -> None:
        self._update_info = info
        if not info:
            self.update_btn.hide()
            return

        ver = str(info.get("version") or "").strip()
        self.update_btn.setText(f"Обновить {ver}" if ver else "Обновить")
        self.update_btn.show()

    def _check_updates_async(self) -> None:
        def worker():
            try:
                m = self._http_get_json(UPDATE_MANIFEST_URL)
                remote_ver = str(m.get("version") or "").strip()
                dl_url = str(m.get("url") or "").strip()

                ok = bool(remote_ver and dl_url and self._is_newer(remote_ver, APP_VERSION))
                QtCore.QTimer.singleShot(0, lambda: self._set_update_available(m if ok else None))
            except Exception:
                QtCore.QTimer.singleShot(0, lambda: self._set_update_available(None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_clicked(self) -> None:
        info = self._update_info or {}
        url = str(info.get("url") or "").strip()
        if not url:
            QtWidgets.QMessageBox.warning(self, "Обновление", "Не найдена ссылка на обновление (url).")
            return
        self._run_updater_and_quit(url)

    # ✅ управление блюром (вызывай из SettingsOverlay.show_overlay()/hide_overlay())
    def set_menu_blur(self, enabled: bool):
        try:
            layer = getattr(self, "_blur_layer", None)
            fade = getattr(self, "_blur_fade", None)
            op = getattr(self, "_blur_opacity", None)

            if layer is None:
                return

            # стопаем/отцепляем прошлые finished, чтобы ничего не "отваливалось"
            if fade is not None:
                try:
                    fade.finished.disconnect()
                except (TypeError, RuntimeError):
                    pass
                fade.stop()

            if enabled:
                import numpy as np
                import cv2

                pm = self.grab()
                dpr = float(pm.devicePixelRatio() or 1.0)

                img = pm.toImage().convertToFormat(QtGui.QImage.Format_ARGB32)
                w = img.width()
                h = img.height()

                ptr = img.bits()  # PySide6: memoryview
                buf = np.frombuffer(ptr, dtype=np.uint8, count=img.sizeInBytes())
                arr = buf.reshape((h, w, 4))

                k = 41
                blurred = cv2.GaussianBlur(arr, (k, k), 0)

                out = QtGui.QImage(
                    blurred.data, w, h,
                    img.bytesPerLine(),
                    QtGui.QImage.Format_ARGB32
                )
                pm_blur = QtGui.QPixmap.fromImage(out.copy())
                pm_blur.setDevicePixelRatio(dpr)

                layer.setGeometry(self.rect())
                layer.setPixmap(pm_blur)
                layer.raise_()
                layer.show()

                # 6) плавно проявляем
                if op is not None:
                    op.setOpacity(0.0)
                if fade is not None and op is not None:
                    fade.setStartValue(float(op.opacity()))
                    fade.setEndValue(1.0)
                    fade.start()
                elif op is not None:
                    op.setOpacity(1.0)

            else:
                # плавно скрываем
                if fade is not None and op is not None:
                    fade.setStartValue(float(op.opacity()))
                    fade.setEndValue(0.0)

                    def _done():
                        try:
                            layer.hide()
                        except Exception:
                            pass

                    fade.finished.connect(_done)
                    fade.start()
                else:
                    layer.hide()

        except Exception:
            import traceback
            traceback.print_exc()  # ← покажет настоящую причину в консоли
            try:
                getattr(self, "_blur_layer", None).hide()
            except Exception:
                pass

    def _reposition_settings_btn(self) -> None:
        """Держим settings.png в правом нижнем углу окна + кнопку обновления сверху справа."""
        try:
            btn = getattr(self, "settings_btn", None)
            if btn is not None:
                pad = 18
                x = self.width() - btn.width() - pad
                y = self.height() - btn.height() - pad
                btn.move(x, y)
        except Exception:
            pass

        # ✅ позиция кнопки обновления (правый верх)
        try:
            ub = getattr(self, "update_btn", None)
            if ub is not None:
                pad = 18
                ub.move(self.width() - ub.width() - pad, 12)
        except Exception:
            pass

    def open_settings(self) -> None:
        # toggle overlay
        try:
            ov = getattr(self, "settings_overlay", None)
            if ov is not None and ov.isVisible():
                ov.hide_overlay()
                return
        except Exception:
            pass

        mons = list_monitors_detailed()
        if not mons or len(mons) <= 1:
            QtWidgets.QMessageBox.information(self, "Настройки", "Найден только 1 монитор — выбирать нечего.")
            return

        ov = getattr(self, "settings_overlay", None)
        if ov is None:
            self.settings_overlay = SettingsOverlay(self)
            ov = self.settings_overlay

        ov.setGeometry(0, 0, self.width(), self.height())
        ov.show_overlay()

    def resizeEvent(self, e):
        try:
            self.bg.setGeometry(0, 0, self.width(), self.height())
            self.veil.setGeometry(0, 0, self.width(), self.height())
            if getattr(self, "_bg_movie", None) is not None:
                self._bg_movie.setScaledSize(QtCore.QSize(self.width(), self.height()))
        except Exception:
            pass

        self._reposition_settings_btn()

        # blur-layer всегда на весь экран окна
        try:
            layer = getattr(self, "_blur_layer", None)
            if layer is not None:
                layer.setGeometry(self.rect())
        except Exception:
            pass

        try:
            ov = getattr(self, "settings_overlay", None)
            if ov is not None:
                ov.setGeometry(0, 0, self.width(), self.height())
        except Exception:
            pass

        super().resizeEvent(e)

    def _apply_titlebar(self):
        try:
            set_titlebar_colors_qt(
                self,
                caption="#0f1117",
                text="#ffffff",
                border="#0f1117",
                dark=True,
                disable_backdrop=True
            )
        except (OSError, AttributeError, RuntimeError, TypeError):
            pass

    # ------------- cards -------------
    def _build_orange_card(self):
        lay = QtWidgets.QVBoxLayout(self.card_orange)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        icon_lbl = QtWidgets.QLabel()
        if os.path.exists(ORANGE_ICON):
            pix = QtGui.QPixmap(ORANGE_ICON).scaled(78, 78, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_lbl.setPixmap(pix)
        icon_lbl.setAlignment(Qt.AlignHCenter)
        lay.addWidget(icon_lbl)

        lay.addSpacing(25)

        self.orange_bind_text = settings.orange_hotkey
        self.orange_btn = GlassPillButton(self.orange_bind_text, w=200, h=30)
        self.orange_btn.clicked.connect(self.begin_capture_orange)
        self.orange_btn.set_on(False)
        lay.addWidget(self.orange_btn, alignment=Qt.AlignHCenter)

        lay.addStretch(1)

    def _build_fish_card(self):
        lay = QtWidgets.QVBoxLayout(self.card_fish)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        icon_lbl = QtWidgets.QLabel()
        if os.path.exists(FISH_ICON):
            pix = QtGui.QPixmap(FISH_ICON).scaled(78, 78, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_lbl.setPixmap(pix)
        icon_lbl.setAlignment(Qt.AlignHCenter)
        lay.addWidget(icon_lbl)

        lay.addSpacing(25)

        self.fish_bind_text = settings.fish_hotkey
        self.fish_btn = GlassPillButton(self.fish_bind_text, w=200, h=30)
        self.fish_btn.clicked.connect(self.begin_capture_fish)
        self.fish_btn.set_on(False)
        lay.addWidget(self.fish_btn, alignment=Qt.AlignHCenter)
        lay.addStretch(1)

    def _build_right_card(self):
        outer = QtWidgets.QVBoxLayout(self.card_right)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(14)

        belt_row = QtWidgets.QHBoxLayout()
        belt_row.setSpacing(8)

        belt_text = QtWidgets.QVBoxLayout()
        belt_text.setSpacing(2)

        t1 = QtWidgets.QLabel("Авто-ремень")
        t1.setStyleSheet(f"color:{TEXT}; font-weight:700; font-size:13px;")
        t2 = QtWidgets.QLabel("водитель")
        t2.setStyleSheet(f"color:{MUTED}; font-size:11px;")

        belt_text.addWidget(t1)
        belt_text.addWidget(t2)
        belt_row.addLayout(belt_text)

        belt_row.addStretch(1)

        self.belt_switch = ToggleSwitch(False)
        self.belt_switch.toggled.connect(self.on_belt_toggle)
        belt_row.addWidget(self.belt_switch, 0, Qt.AlignVCenter)

        belt_row.addSpacing(22)
        outer.addLayout(belt_row)

        afk_row = QtWidgets.QHBoxLayout()
        afk_label = QtWidgets.QLabel("Анти-АФК")
        afk_label.setStyleSheet(f"color:{TEXT}; font-weight:700; font-size:13px;")
        afk_row.addWidget(afk_label)
        afk_row.addStretch(1)

        self.afk_bind_text = settings.afk_hotkey
        self.afk_btn = GlassPillButton(self.afk_bind_text, w=110, h=30)
        self.afk_btn.clicked.connect(self.begin_capture_afk)
        self.afk_btn.set_on(False)
        afk_row.addWidget(self.afk_btn)
        outer.addLayout(afk_row)

        sila_row = QtWidgets.QHBoxLayout()
        sila_label = QtWidgets.QLabel("Качалка")
        sila_label.setStyleSheet(f"color:{TEXT}; font-weight:700; font-size:13px;")
        sila_row.addWidget(sila_label)
        sila_row.addStretch(1)

        self.sila_bind_text = settings.sila_hotkey
        self.sila_btn = GlassPillButton(self.sila_bind_text, w=110, h=30)
        self.sila_btn.clicked.connect(self.begin_capture_sila)
        self.sila_btn.set_on(False)
        sila_row.addWidget(self.sila_btn)
        outer.addLayout(sila_row)

        outer.addStretch(1)

    # ------------- hotkey capture -------------
    def begin_capture_orange(self):
        self.capture_mode = "orange"
        self.orange_btn.setText("PRESS")

    def begin_capture_afk(self):
        self.capture_mode = "afk"
        self.afk_btn.setText("PRESS")

    def begin_capture_sila(self):
        self.capture_mode = "sila"
        self.sila_btn.setText("PRESS")

    def begin_capture_fish(self):
        self.capture_mode = "fish"
        self.fish_btn.setText("PRESS")

    def is_capture_active(self) -> bool:
        return self.capture_mode is not None

    def keyPressEvent(self, e: QtGui.QKeyEvent):
        # ✅ если открыт оверлей — ESC закрывает
        try:
            ov = getattr(self, "settings_overlay", None)
            if ov is not None and ov.isVisible() and e.key() == Qt.Key_Escape:
                ov.hide_overlay()
                e.accept()
                return
        except Exception:
            pass

        if self.capture_mode is None:
            super().keyPressEvent(e)
            return

        key = e.key()
        if Qt.Key_F1 <= key <= Qt.Key_F35:
            hk = f"F{key - Qt.Key_F1 + 1}"
        else:
            txt = e.text()
            if not txt:
                return
            hk = txt.upper()
            if len(hk) != 1:
                return

        if self.capture_mode == "orange":
            settings.set_orange(hk)
            self.orange_bind_text = settings.orange_hotkey
            self.orange_btn.setText(self.orange_bind_text)
        elif self.capture_mode == "afk":
            settings.set_afk(hk)
            self.afk_bind_text = settings.afk_hotkey
            self.afk_btn.setText(self.afk_bind_text)
        elif self.capture_mode == "sila":
            settings.set_sila(hk)
            self.sila_bind_text = settings.sila_hotkey
            self.sila_btn.setText(self.sila_bind_text)
        elif self.capture_mode == "fish":
            settings.set_fish(hk)
            self.fish_bind_text = settings.fish_hotkey
            self.fish_btn.setText(self.fish_bind_text)

        self.capture_mode = None
        e.accept()

    # ------------- services callbacks -------------
    def on_belt_toggle(self, state: bool):
        now = time.time()
        if now - self._last_belt_toggle < 0.25:
            return
        self._last_belt_toggle = now
        if state:
            services.belt_start()
        else:
            services.belt_stop()

    def fish_toggle(self):
        services.fish_toggle()
        self.update_fish_state()

    # ------------- polling UI states -------------
    def update_orange_state(self):
        self.orange_btn.set_on(services.orange_is_running())
        if not self.is_capture_active() or self.capture_mode != "orange":
            self.orange_btn.setText(settings.orange_hotkey)

    def update_afk_state(self):
        self.afk_btn.set_on(services.afk_is_running())
        if not self.is_capture_active() or self.capture_mode != "afk":
            self.afk_btn.setText(settings.afk_hotkey)

    def update_sila_state(self):
        running = services.sila_is_running()
        self.sila_btn.set_on(running)
        if not self.is_capture_active() or self.capture_mode != "sila":
            self.sila_btn.setText("STOP" if running else settings.sila_hotkey)

    def update_fish_state(self):
        running = services.fish_is_running()
        self.fish_btn.set_on(running)
        if not self.is_capture_active() or self.capture_mode != "fish":
            self.fish_btn.setText("STOP" if running else settings.fish_hotkey)

    def poll(self):
        if _app_closing:
            return
        self.update_orange_state()
        self.update_afk_state()
        self.update_sila_state()
        self.update_fish_state()

        if self.belt_switch.state and (not services.belt_is_running()):
            self.belt_switch._state = False
            self.belt_switch._target = False
            self.belt_switch._update_positions(initial=False)

    def closeEvent(self, e: QtGui.QCloseEvent):
        global _app_closing
        if _app_closing:
            e.accept()
            return
        _app_closing = True

        _preauth["active"] = False
        _preauth["deadline"] = 0.0

        try:
            t = getattr(self, "_poll_timer", None)
            if t is not None:
                t.stop()
        except (RuntimeError, AttributeError):
            pass

        try:
            services.stop_all()
        except (RuntimeError, OSError):
            pass

        try:
            _stop_local_bot_and_release_lock()
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass

        e.accept()

# ===================== APP ENTRY =====================
def main() -> int:
    global _app_closing

    _app_closing = False  # на всякий случай

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("AFKGame.app")

    try:
        apply_app_typography(app)
    except Exception:
        pass

    if os.path.exists(ORANGE_ICON):
        try:
            app.setWindowIcon(QtGui.QIcon(ORANGE_ICON))
        except Exception:
            pass

    # сделаем переменную заранее, чтобы cleanup был безопасным
    hotkeys = None

    # ---------- unified cleanup ----------
    def _cleanup_all():
        global _app_closing
        if _app_closing:
            return
        _app_closing = True

        # 1) hotkeys
        try:
            if hotkeys is not None:
                hotkeys.stop()
        except Exception:
            pass

        # 2) services
        try:
            services.stop_all()
        except Exception:
            pass

        # 3) bot proc + lock
        try:
            _stop_local_bot_and_release_lock()
        except Exception:
            pass

    # ВАЖНО: сработает при любом выходе из Qt loop
    try:
        app.aboutToQuit.connect(_cleanup_all)
    except Exception:
        pass

    # ---------- hotkeys ----------
    def hk_orange(*_):
        services.orange_toggle()

    def hk_afk(*_):
        services.afk_toggle()

    def hk_sila(*_):
        services.sila_toggle()

    def hk_fish(*_):
        services.fish_toggle()

    # We will attach capture-mode query to the window later
    hotkeys_holder = {"win": None}

    def is_capture_active():
        w = hotkeys_holder.get("win")
        if w is None:
            return False
        try:
            return bool(getattr(w, "is_capture_active", lambda: False)())
        except Exception:
            return False

    try:
        hotkeys = GlobalHotkeys(
            settings,
            on_orange=hk_orange,
            on_afk=hk_afk,
            on_sila=hk_sila,        # ✅ SILA
            on_fish=hk_fish,
            is_capture_active=is_capture_active,
        )
        hotkeys.start()
    except Exception:
        hotkeys = None

    # ---------- auth ----------
    if not should_skip_auth():
        dlg = AuthDialog()
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            # если закрыли авторизацию крестиком — прибираем всё, включая bot_proc
            _cleanup_all()
            return 0

    # ---------- main window ----------
    win = MainWindow()
    hotkeys_holder["win"] = win

    # если главное окно закрыли — гарантированно чистим
    try:
        win.destroyed.connect(lambda *_: _cleanup_all())
    except Exception:
        pass

    win.show()

    # initial sync
    try:
        win.update_orange_state()
        win.update_afk_state()
        win.update_sila_state()     # ✅
        win.update_fish_state()
    except Exception:
        pass

    rc = app.exec()

    # На всякий случай: если aboutToQuit не сработал (редко), добьём здесь
    _cleanup_all()
    return rc

if __name__ == "__main__":
    raise SystemExit(main())