# core_app.py
from __future__ import annotations

import os
import sys
import runpy
import json
import subprocess
import threading
from typing import List, Optional

from pynput import keyboard

# ===================== WINDOWS JOB (kill children on parent kill) =====================
# Это решает кейс: "Завершить задачу" убивает GUI, но дети от Popen остаются жить.
# С JobObject + KILL_ON_JOB_CLOSE все процессы в job умирают при закрытии/убийстве родителя.
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", wintypes.ULARGE_INTEGER),
            ("WriteOperationCount", wintypes.ULARGE_INTEGER),
            ("OtherOperationCount", wintypes.ULARGE_INTEGER),
            ("ReadTransferCount", wintypes.ULARGE_INTEGER),
            ("WriteTransferCount", wintypes.ULARGE_INTEGER),
            ("OtherTransferCount", wintypes.ULARGE_INTEGER),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL

    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    _APP_JOB = None

    def _ensure_app_job():
        global _APP_JOB
        if _APP_JOB:
            return _APP_JOB

        hjob = _kernel32.CreateJobObjectW(None, None)
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        ok = _kernel32.SetInformationJobObject(
            hjob,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            return None

        _APP_JOB = hjob
        return _APP_JOB

    def assign_to_app_job(p: subprocess.Popen) -> None:
        """Привязать процесс к job, чтобы он умер, если убьют родителя."""
        try:
            job = _ensure_app_job()
            if not job:
                return
            ph = wintypes.HANDLE(int(getattr(p, "_handle")))
            _kernel32.AssignProcessToJobObject(job, ph)
        except Exception:
            pass

else:
    def assign_to_app_job(p: subprocess.Popen) -> None:
        return


# ===================== PATHS =====================
def get_user_data_dir(app_name: str = "XyesosBeta") -> str:
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or os.path.expanduser("~")
    )
    d = os.path.join(base, app_name)
    os.makedirs(d, exist_ok=True)
    return d


def get_exe_dir() -> str:
    # где лежит exe (или .py при разработке)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_res_dir() -> str:
    # где лежат ресурсы (в onefile это _MEIPASS)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def app_dir() -> str:
    # Работает и в .py, и в PyInstaller .exe
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


# ===================== SERVICE MODE =====================
def run_service_mode_if_requested() -> bool:
    if "--service" not in sys.argv:
        return False

    i = sys.argv.index("--service")
    if i + 1 >= len(sys.argv):
        return True

    name = (sys.argv[i + 1] or "").lower()
    rest = sys.argv[i + 2 :]

    base = app_dir()

    # гарантируем, что базовый путь есть в sys.path
    if base not in sys.path:
        sys.path.insert(0, base)

    def _is_flag_present(argv: list[str], flag: str) -> bool:
        return flag in argv

    def _append_assets_dir(argv: list[str], assets_path: str) -> list[str]:
        # если пользователь уже передал --assets-dir, не трогаем
        if _is_flag_present(argv, "--assets-dir"):
            return argv
        return [*argv, "--assets-dir", assets_path]

    # ✅ читаем выбранный монитор из cfg (он лежит в user-data, а не в _MEIPASS)
    def _read_selected_monitor() -> int:
        # дефолт
        mon = 1
        try:
            data_dir = get_user_data_dir("XyesosBeta")
            cfg_path = os.path.join(data_dir, "cfg")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if isinstance(cfg, dict):
                    v = cfg.get("monitor", 1)
                    try:
                        mon = int(v)
                    except Exception:
                        mon = 1
        except Exception:
            mon = 1

        # защита от мусора
        if mon < 1:
            mon = 1
        return mon

    # куда писать crash-лог: рядом с exe (в onefile _MEIPASS временный)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else base

    try:
        if name == "afk":
            argv = ["afk.py", "--autorun", "--no-hotkey", "--control-stdin", *rest]
            assets = os.path.join(base, "afk", "assets")
            sys.argv = _append_assets_dir(argv, assets)
            runpy.run_path(os.path.join(base, "afk", "afk.py"), run_name="__main__")
            return True

        if name == "sila":
            # ✅ передаём монитор в sila.py
            mon = _read_selected_monitor()
            argv = ["sila.py", "--autorun", "--no-hotkey", "--control-stdin", "--monitor", str(mon), *rest]
            assets = os.path.join(base, "sila", "assets")
            sys.argv = _append_assets_dir(argv, assets)
            runpy.run_path(os.path.join(base, "sila", "sila.py"), run_name="__main__")
            return True

        if name == "apelsin":
            mon = _read_selected_monitor()
            argv = ["apelsin.py", "--autorun", "--control-stdin", "--monitor", str(mon), *rest]
            assets = os.path.join(base, "apelsin", "assets")
            sys.argv = _append_assets_dir(argv, assets)
            runpy.run_path(os.path.join(base, "apelsin", "apelsin.py"), run_name="__main__")
            return True

        if name == "fish":
            argv = ["fisher.py", "--autorun", "--no-hotkey", "--control-stdin", *rest]
            assets = os.path.join(base, "fish", "assets")
            sys.argv = _append_assets_dir(argv, assets)
            runpy.run_path(os.path.join(base, "fish", "fisher.py"), run_name="__main__")
            return True

        if name == "belt":
            argv = ["belt_service.py", "--autorun", "--no-hotkey", "--control-stdin", *rest]
            assets = os.path.join(base, "autobelt", "assets")
            sys.argv = _append_assets_dir(argv, assets)
            runpy.run_path(os.path.join(base, "autobelt", "belt_service.py"), run_name="__main__")
            return True

        if name == "bot":
            # для бота assets-dir обычно не нужен
            sys.argv = ["bot.py", *rest]
            runpy.run_module("bot", run_name="__main__")
            return True

    except Exception:
        try:
            import traceback
            p = os.path.join(exe_dir, f"{name}_service_crash.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n\n=== SERVICE CRASH ===\n")
                f.write(traceback.format_exc())
        except Exception:
            pass

        return True

    # если имя сервиса неизвестно — просто выходим
    return True


# ===================== SELF CMD =====================
def self_cmd(*extra_args) -> List[str]:
    # В exe: запускаем сам exe
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra_args]

    # В проекте: запускаем тот .py, который реально запущен (gui.py)
    script = os.path.abspath(sys.argv[0])

    # Иногда argv[0] может быть пустым/не .py — тогда fallback на gui.py в app_dir()
    if not script.lower().endswith(".py") or not os.path.exists(script):
        script = os.path.join(app_dir(), "gui.py")

    return [sys.executable, script, *extra_args]


# ===================== SETTINGS =====================
FORBIDDEN_KEYS = {"F1", "F2", "F3", "F6", "F8", "F11", "F12"}

class HotkeySettings:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, "cfg")

        # загружаем весь cfg
        self.data = self._load()

        # гарантируем секцию hotkeys
        hk = self.data.get("hotkeys")
        if not isinstance(hk, dict):
            hk = {}
            self.data["hotkeys"] = hk

        # читаем хоткеи из cfg["hotkeys"]
        self.orange_hotkey = (hk.get("orange_hotkey") or "F7").upper().strip()
        self.afk_hotkey = (hk.get("afk_hotkey") or "F10").upper().strip()
        self.fish_hotkey = (hk.get("fish_hotkey") or "F5").upper().strip()
        self.sila_hotkey = (hk.get("sila_hotkey") or "F9").upper().strip()

    def _load(self):
        default_hotkeys = {
            "orange_hotkey": "F7",
            "afk_hotkey": "F10",
            "fish_hotkey": "F5",
            "sila_hotkey": "F9",
        }

        def _read_json(p: str) -> dict:
            try:
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        j = json.load(f)
                        return j if isinstance(j, dict) else {}
            except Exception:
                pass
            return {}

        cfg_path = self.path

        # читаем cfg
        cfg = _read_json(cfg_path)

        if not isinstance(cfg, dict):
            cfg = {}

        # гарантируем секцию hotkeys
        hk = cfg.get("hotkeys")
        if not isinstance(hk, dict):
            hk = {}
            cfg["hotkeys"] = hk

        # дотягиваем дефолты
        for k, v in default_hotkeys.items():
            vv = hk.get(k)
            if not isinstance(vv, str) or not vv.strip():
                hk[k] = v

        return cfg

    def save(self):
        try:
            cfg = self.data if isinstance(self.data, dict) else {}

            hk = cfg.get("hotkeys")
            if not isinstance(hk, dict):
                hk = {}
                cfg["hotkeys"] = hk

            hk["orange_hotkey"] = self.orange_hotkey
            hk["afk_hotkey"] = self.afk_hotkey
            hk["fish_hotkey"] = self.fish_hotkey
            hk["sila_hotkey"] = self.sila_hotkey

            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)

            self.data = cfg

        except Exception:
            pass

    @staticmethod
    def _is_forbidden_ks(ks: str) -> bool:
        return ks in FORBIDDEN_KEYS

    def set_orange(self, ks: str) -> bool:
        ks = ks.upper().strip()
        if ks.startswith("F") and ks[1:].isdigit() and self._is_forbidden_ks(ks):
            return False
        if ks in (self.afk_hotkey, self.fish_hotkey, self.sila_hotkey):
            return False
        self.orange_hotkey = ks
        self.save()
        return True

    def set_afk(self, ks: str) -> bool:
        ks = ks.upper().strip()
        if ks.startswith("F") and ks[1:].isdigit() and self._is_forbidden_ks(ks):
            return False
        if ks in (self.orange_hotkey, self.fish_hotkey, self.sila_hotkey):
            return False
        self.afk_hotkey = ks
        self.save()
        return True

    def set_fish(self, ks: str) -> bool:
        ks = ks.upper().strip()
        if ks.startswith("F") and ks[1:].isdigit() and self._is_forbidden_ks(ks):
            return False
        if ks in (self.orange_hotkey, self.afk_hotkey, self.sila_hotkey):
            return False
        self.fish_hotkey = ks
        self.save()
        return True

    def set_sila(self, ks: str) -> bool:
        ks = ks.upper().strip()
        if ks.startswith("F") and ks[1:].isdigit() and self._is_forbidden_ks(ks):
            return False
        if ks in (self.orange_hotkey, self.afk_hotkey, self.fish_hotkey):
            return False
        self.sila_hotkey = ks
        self.save()
        return True


# ===================== HOTKEY HELPERS =====================
def key_to_string(key) -> Optional[str]:
    if isinstance(key, keyboard.Key):
        name = key.name
        if name and name.startswith("f") and name[1:].isdigit():
            return f"F{name[1:]}"
    if isinstance(key, keyboard.KeyCode) and key.char:
        return key.char.upper()
    return None


def matches_hotkey(key, hk: str) -> bool:
    return key_to_string(key) == hk


# ===================== PROCESS MANAGER =====================
class ServiceManager:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir

        self.orange_proc: Optional[subprocess.Popen] = None
        self.fish_proc: Optional[subprocess.Popen] = None
        self.belt_proc: Optional[subprocess.Popen] = None
        self.afk_proc: Optional[subprocess.Popen] = None
        self.sila_proc: Optional[subprocess.Popen] = None

    def _creation_flags(self):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    def _popen(self, args, stdin_pipe: bool = False) -> subprocess.Popen:
        # 1) Откуда запускать процессы (cwd)
        exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else self.base_dir

        # 2) Куда писать логи: ВСЕГДА в base_dir
        try:
            os.makedirs(self.base_dir, exist_ok=True)
        except Exception:
            pass

        log_path = os.path.join(self.base_dir, "xyesos.log")

        try:
            logf = open(log_path, "a", encoding="utf-8", errors="ignore", buffering=1)
        except Exception:
            logf = None

        p = subprocess.Popen(
            args,
            cwd=exe_dir,
            creationflags=self._creation_flags(),
            stdin=(subprocess.PIPE if stdin_pipe else None),
            stdout=(logf if logf is not None else subprocess.DEVNULL),
            stderr=(logf if logf is not None else subprocess.DEVNULL),
        )

        # ВАЖНО: привязать к job, чтобы умер при "Завершить задачу" родителя
        try:
            assign_to_app_job(p)
        except Exception:
            pass

        return p

    @staticmethod
    def _stop_proc(proc: Optional[subprocess.Popen]) -> None:
        if proc is None:
            return

        # 1) быстро просим выйти
        try:
            if proc.poll() is None:
                try:
                    if getattr(proc, "stdin", None):
                        proc.stdin.write(b"STOP\n")
                        proc.stdin.flush()
                except Exception:
                    pass
        except Exception:
            pass

        # 2) “добиватель” в фоне (GUI не лагает)
        def _killer(p: subprocess.Popen):
            try:
                p.wait(timeout=1.5)
                return
            except Exception:
                pass
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=1.0)
                return
            except Exception:
                pass
            try:
                p.kill()
            except Exception:
                pass

        try:
            threading.Thread(target=_killer, args=(proc,), daemon=True).start()
        except Exception:
            pass

        # stdin можно закрыть сразу
        try:
            if getattr(proc, "stdin", None):
                proc.stdin.close()
        except Exception:
            pass

    # ---------- ORANGE ----------
    def orange_is_running(self) -> bool:
        return self.orange_proc is not None and self.orange_proc.poll() is None

    def orange_start(self):
        if self.orange_is_running():
            return
        args = self_cmd("--service", "apelsin")
        self.orange_proc = self._popen(args, stdin_pipe=True)

    def orange_stop(self):
        self._stop_proc(self.orange_proc)
        self.orange_proc = None

    def orange_toggle(self):
        if self.orange_is_running():
            self.orange_stop()
        else:
            self.orange_start()

    # ---------- FISH ----------
    def fish_is_running(self) -> bool:
        return self.fish_proc is not None and self.fish_proc.poll() is None

    def fish_start(self):
        if self.fish_is_running():
            return
        args = self_cmd("--service", "fish")
        self.fish_proc = self._popen(args, stdin_pipe=True)

    def fish_stop(self):
        self._stop_proc(self.fish_proc)
        self.fish_proc = None

    def fish_toggle(self):
        if self.fish_is_running():
            self.fish_stop()
        else:
            self.fish_start()

    # ---------- BELT ----------
    def belt_is_running(self) -> bool:
        return self.belt_proc is not None and self.belt_proc.poll() is None

    def belt_start(self):
        if self.belt_is_running():
            return
        args = self_cmd("--service", "belt")
        self.belt_proc = self._popen(args, stdin_pipe=True)

    def belt_stop(self):
        self._stop_proc(self.belt_proc)
        self.belt_proc = None

    # ---------- AFK ----------
    def afk_is_running(self) -> bool:
        return self.afk_proc is not None and self.afk_proc.poll() is None

    def afk_start(self):
        if self.afk_is_running():
            return
        args = self_cmd("--service", "afk")
        self.afk_proc = self._popen(args, stdin_pipe=True)

    def afk_stop(self):
        self._stop_proc(self.afk_proc)
        self.afk_proc = None

    def afk_toggle(self):
        if self.afk_is_running():
            self.afk_stop()
        else:
            self.afk_start()

    # ---------- SILA ----------
    def sila_is_running(self) -> bool:
        return self.sila_proc is not None and self.sila_proc.poll() is None

    def sila_start(self):
        if self.sila_is_running():
            return
        args = self_cmd("--service", "sila")
        self.sila_proc = self._popen(args, stdin_pipe=True)

    def sila_stop(self):
        self._stop_proc(self.sila_proc)
        self.sila_proc = None

    def sila_toggle(self):
        if self.sila_is_running():
            self.sila_stop()
        else:
            self.sila_start()

    # ---------- ALL ----------
    def stop_all(self):
        self.orange_stop()
        self.belt_stop()
        self.afk_stop()
        self.sila_stop()
        self.fish_stop()


# ===================== GLOBAL HOTKEY LISTENER =====================
class GlobalHotkeys:
    def __init__(
        self,
        settings: HotkeySettings,
        *,
        on_orange,
        on_afk,
        on_sila,
        on_fish,
        is_capture_active,
    ):
        self.settings = settings
        self.on_orange = on_orange
        self.on_afk = on_afk
        self.on_sila = on_sila
        self.on_fish = on_fish
        self.is_capture_active = is_capture_active
        self.listener = None

    def start(self):
        def on_press(key):
            if self.is_capture_active():
                return

            if matches_hotkey(key, self.settings.orange_hotkey):
                self.on_orange()
            elif matches_hotkey(key, self.settings.afk_hotkey):
                self.on_afk()
            elif matches_hotkey(key, self.settings.sila_hotkey):
                self.on_sila()
            elif matches_hotkey(key, self.settings.fish_hotkey):
                self.on_fish()

        self.listener = keyboard.Listener(on_press=on_press)
        self.listener.start()

    def stop(self):
        try:
            if self.listener:
                self.listener.stop()
                try:
                    self.listener.join(timeout=0.5)
                except Exception:
                    pass
        except Exception:
            pass
        self.listener = None
