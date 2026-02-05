# updater.py
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

# ---------------- PID helpers ----------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True

    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def wait_pid_exit(pid: int, timeout: float = 60.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)

def wait_file_unlock(path: str, timeout: float = 60.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with open(path, "ab"):
                return
        except Exception:
            time.sleep(0.2)

# ---------------- download ----------------
def download_to_temp(url: str, name_hint: str) -> str:
    tmp_dir = tempfile.mkdtemp(prefix="xy_update_")
    out = os.path.join(tmp_dir, name_hint)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"})

    with urllib.request.urlopen(req, timeout=40) as r, open(out, "wb") as f:
        shutil.copyfileobj(r, f)

    # защита от html/ошибки
    if not os.path.exists(out) or os.path.getsize(out) < 200_000:
        raise RuntimeError("download failed or file too small")

    return out

# ---------------- replace ----------------
def replace_exe(target: str, newexe: str) -> None:
    target = os.path.abspath(target)
    newexe = os.path.abspath(newexe)

    bak = target + ".bak"

    try:
        if os.path.exists(bak):
            os.remove(bak)
    except Exception:
        pass

    if os.path.exists(target):
        try:
            os.replace(target, bak)
        except Exception:
            try:
                os.remove(target)
            except Exception:
                pass

    os.replace(newexe, target)

# ---------------- launch ----------------
def launch(target: str):
    subprocess.Popen(
        [target],
        cwd=os.path.dirname(os.path.abspath(target)),
        close_fds=True,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0))

# ---------------- main ----------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--target", required=True)
    ap.add_argument("--url", required=True)
    args = ap.parse_args()

    pid = int(args.pid or 0)
    target = args.target
    url = args.url

    # 1) ждём пока GUI умрёт
    if pid:
        wait_pid_exit(pid, 60.0)

    # 2) ждём разблокировки exe
    wait_file_unlock(target, 60.0)

    # 3) качаем новый exe во временную папку
    name = os.path.basename(target)
    newexe = download_to_temp(url, name)

    # 4) заменяем
    replace_exe(target, newexe)

    # 5) запускаем обновлённый
    launch(target)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())