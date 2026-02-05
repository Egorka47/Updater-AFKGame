# autobelt/belt_service.py
import sys
import time
import threading
import argparse
from dataclasses import dataclass
from typing import Callable, Optional

from pynput import keyboard
from pynput.keyboard import Controller as KeyboardController, KeyCode

# ===================== STOP (for service control) =====================
STOP_EVENT = threading.Event()

def request_stop(reason=""):
    STOP_EVENT.set()

def _stdin_stop_watcher():
    try:
        while not STOP_EVENT.is_set():
            line = sys.stdin.readline()
            if not line:
                time.sleep(0.05)
                continue
            if line.strip().upper().startswith("STOP"):
                request_stop("STDIN")
                return
    except Exception:
        pass


# ===================== CONFIG =====================
@dataclass
class AutoBeltConfig:
    # через сколько секунд после F нажимать J
    f_to_j_delay: float = 3.5

    # анти-спам: минимальный интервал между срабатываниями (после F)
    enter_cooldown_sec: float = 2.0

    # защита от слишком частого J (на всякий)
    min_j_interval: float = 0.25

    # если True — повторное F пока таймер активен перезапустит таймер
    # если False — повторное F игнорируется, пока таймер не отработал
    restart_timer_on_repeat_f: bool = True


# ===================== SERVICE =====================
class AutoBeltService:
    def __init__(self, cfg: AutoBeltConfig, on_status: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.on_status = on_status or (lambda _: None)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.kb = KeyboardController()
        self.key_j = KeyCode.from_vk(0x4A)  # J
        self.key_f = KeyCode.from_vk(0x46)  # F

        self._last_j = 0.0
        self._last_fire = 0.0

        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

        self._listener: Optional[keyboard.Listener] = None

        # чтобы не ловить автоповтор при удержании F
        self._f_is_down = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.on_status("включено")

    def stop(self):
        self._stop.set()

        with self._lock:
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None

        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass

        try:
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.0)
        except Exception:
            pass

        self.on_status("выключено")

    def _press_j(self):
        now = time.time()
        if now - self._last_j < self.cfg.min_j_interval:
            return
        self.kb.press(self.key_j)
        time.sleep(0.02)
        self.kb.release(self.key_j)
        self._last_j = now

    def _timer_fire(self):
        # таймер может сработать уже после stop()
        if self._stop.is_set() or STOP_EVENT.is_set():
            return
        self._press_j()
        self.on_status("J (после F)")

    def _schedule_j(self):
        now = time.time()

        # анти-спам
        if (now - self._last_fire) < self.cfg.enter_cooldown_sec:
            return

        with self._lock:
            if self._timer is not None:
                if self.cfg.restart_timer_on_repeat_f:
                    try:
                        self._timer.cancel()
                    except Exception:
                        pass
                    self._timer = None
                else:
                    # таймер уже есть и перезапуск запрещён
                    return

            self._last_fire = now
            self._timer = threading.Timer(self.cfg.f_to_j_delay, self._timer_fire)
            self._timer.daemon = True
            self._timer.start()

        self.on_status(f"F → через {self.cfg.f_to_j_delay:.1f}s нажму J")

    def _on_press(self, key):
        if self._stop.is_set() or STOP_EVENT.is_set():
            return False

        try:
            # сравниваем по vk, чтобы работало независимо от раскладки
            if hasattr(key, "vk") and key.vk == self.key_f.vk:
                if not self._f_is_down:
                    self._f_is_down = True
                    self._schedule_j()
        except Exception:
            pass

    def _on_release(self, key):
        if self._stop.is_set() or STOP_EVENT.is_set():
            return False

        try:
            if hasattr(key, "vk") and key.vk == self.key_f.vk:
                self._f_is_down = False
        except Exception:
            pass

    def _loop(self):
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

        # держим поток живым
        while not self._stop.is_set() and not STOP_EVENT.is_set():
            time.sleep(0.1)


# ===================== API for GUI/SERVICE =====================
_service: Optional[AutoBeltService] = None

def start():
    global _service
    if _service is None:
        cfg = AutoBeltConfig()
        _service = AutoBeltService(cfg)
    _service.start()

def stop():
    global _service
    if _service is not None:
        _service.stop()
        _service = None


# ===================== CLI ENTRYPOINT =====================
def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--autorun", action="store_true")
    ap.add_argument("--control-stdin", action="store_true")
    ap.add_argument("--delay", type=float, default=None, help="Delay seconds after F to press J")
    ap.add_argument("--cooldown", type=float, default=None, help="Cooldown seconds between triggers")
    ap.add_argument("--no-restart", action="store_true", help="Do not restart timer on repeated F")
    args, _ = ap.parse_known_args()

    if args.control_stdin:
        threading.Thread(target=_stdin_stop_watcher, daemon=True).start()

    if args.autorun:
        cfg = AutoBeltConfig()
        if args.delay is not None:
            cfg.f_to_j_delay = max(0.0, float(args.delay))
        if args.cooldown is not None:
            cfg.enter_cooldown_sec = max(0.0, float(args.cooldown))
        if args.no_restart:
            cfg.restart_timer_on_repeat_f = False

        global _service
        _service = AutoBeltService(cfg)
        _service.start()

    try:
        while not STOP_EVENT.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        request_stop("KeyboardInterrupt")
    finally:
        stop()


if __name__ == "__main__":
    main()
