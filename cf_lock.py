# cf_lock.py  (VARIANT #2: Authorization Bearer token, Worker proxy)
from __future__ import annotations

import os
import re
import time
import threading
import socket
import json
import hashlib
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Tuple

RENEW_EVERY_SEC = 25
TIMEOUT_SEC = 6

_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_have_lock = False

LOCK_URL = ""
AUTH_TOKEN = ""          # клиентский токен (простой секрет env.AUTH_TOKEN)
AUTH_TOKEN_ADMIN = ""    # опционально, но в твоём worker.js НЕ используется (оставим для совместимости)
CLIENT_ID = ""           # hostname/логика удобства (для обычных запросов)

# -------------------- CONFIG LOADING --------------------
def _read_json(p: Path) -> dict:
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text("utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _has_keys(d: dict) -> bool:
    try:
        url = str(d.get("cf_lock_url") or "").strip()
        tok = str(d.get("cf_auth_token") or "").strip()
        return bool(url and tok)
    except Exception:
        return False

def _load_config_file() -> dict:
    """
    Ищем config.json:
      1) рядом с exe (portable) / или рядом с .py в dev
      2) %LOCALAPPDATA%\\XyesosBeta\\config.json
      3) _MEIPASS\\config.json (если вшит)
    """
    candidates: list[Path] = []

    try:
        import sys
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / "config.json")
        else:
            candidates.append(Path(__file__).resolve().parent / "config.json")
    except Exception:
        pass

    try:
        base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())).strip()
        candidates.append(Path(base) / "XyesosBeta" / "config.json")
    except Exception:
        pass

    try:
        import sys
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "config.json")
    except Exception:
        pass

    last_any: dict = {}
    for p in candidates:
        d = _read_json(p)
        if not d:
            continue
        last_any = d
        if _has_keys(d):
            return d

    return last_any or {}

def _strip_bearer(s: str) -> str:
    return re.sub(r"^\s*Bearer\s+", "", (s or ""), flags=re.IGNORECASE).strip()

def init_lock_settings() -> None:
    global LOCK_URL, AUTH_TOKEN, AUTH_TOKEN_ADMIN, CLIENT_ID

    cfg = _load_config_file() or {}

    env_url = (os.environ.get("CF_LOCK_URL") or "").strip()
    env_tok = (os.environ.get("CF_AUTH_TOKEN") or "").strip()
    env_admin = (os.environ.get("CF_AUTH_TOKEN_ADMIN") or "").strip()
    env_client = (os.environ.get("CF_CLIENT_ID") or "").strip()

    cfg_url = str(cfg.get("cf_lock_url") or "").strip()
    cfg_tok = str(cfg.get("cf_auth_token") or "").strip()
    cfg_admin = str(cfg.get("cf_auth_token_admin") or "").strip()
    cfg_client = str(cfg.get("client_id") or "").strip()

    url = (env_url or cfg_url).strip()
    LOCK_URL = url.rstrip("/") if url else ""

    # важное: в конфиге/ENV может оказаться "Bearer XXX" — чистим
    AUTH_TOKEN = _strip_bearer(env_tok or cfg_tok)
    AUTH_TOKEN_ADMIN = _strip_bearer(env_admin or cfg_admin)

    # client_id для обычных запросов (в worker.js он обязателен)
    CLIENT_ID = (env_client or cfg_client or socket.gethostname()).strip()

# -------------------- DEVICE ID (salt=AUTH_TOKEN) --------------------
def get_device_id() -> str:
    if not AUTH_TOKEN:
        init_lock_settings()

    salt = (AUTH_TOKEN or "default-salt").encode("utf-8", errors="ignore")

    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as k:
                guid, _ = winreg.QueryValueEx(k, "MachineGuid")
                guid = str(guid).strip()
                if guid:
                    h = hashlib.sha256(salt + guid.encode("utf-8", errors="ignore")).hexdigest()
                    return f"mg:{h}"
        except Exception:
            pass

    host = socket.gethostname().strip() if socket.gethostname() else "unknown"
    h = hashlib.sha256(salt + host.encode("utf-8", errors="ignore")).hexdigest()
    return f"hn:{h}"

# -------------------- HTTP --------------------
def _post(path: str, payload: Optional[dict] = None, *, use_admin_token: bool = False) -> dict:
    if (not LOCK_URL) or (not AUTH_TOKEN):
        init_lock_settings()

    if not LOCK_URL:
        return {"ok": False, "error": "no_lock_url"}
    if not AUTH_TOKEN:
        return {"ok": False, "error": "no_auth_token"}

    # ВАЖНО: в твоём worker.js проверяется ТОЛЬКО env.AUTH_TOKEN.
    # Поэтому админский токен как отдельный секрет не нужен, но оставим fallback:
    token = (AUTH_TOKEN_ADMIN if use_admin_token else AUTH_TOKEN) or AUTH_TOKEN
    token = _strip_bearer(token)

    if not token:
        return {"ok": False, "error": "no_auth_token"}

    url = f"{LOCK_URL}{path}"
    req = urllib.request.Request(url, method="POST")

    # Worker ждёт Bearer
    req.add_header("Authorization", f"Bearer {token}")

    # Worker ОБЯЗАТЕЛЬНО ждёт x-client-id
    # А DurableObject для /admin/set и /bind/set требует clientId === "admin-bot"
    if use_admin_token:
        cid = "admin-bot"
    else:
        cid = (CLIENT_ID or "").strip() or socket.gethostname().strip() or "unknown-pc"
    req.add_header("x-client-id", cid)

    # Cloudflare может банить urllib по "подписи" (Error 1010) — притворяемся браузером
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    req.add_header("Cache-Control", "no-cache")
    req.add_header("Pragma", "no-cache")

    req.add_header("content-type", "application/json")
    req.add_header("accept", "application/json")

    data = json.dumps(payload or {}).encode("utf-8")

    try:
        with urllib.request.urlopen(req, data=data, timeout=TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except Exception:
                return {"ok": False, "error": "bad_json", "raw": raw}

    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        j = None
        if raw:
            try:
                j = json.loads(raw)
            except Exception:
                j = None
        return {
            "ok": False,
            "error": "http_error",
            "status": int(getattr(e, "code", 0) or 0),
            "detail": str(e),
            "raw": raw,
            "json": j,
        }
    except urllib.error.URLError as e:
        return {"ok": False, "error": "http_error", "detail": f"URLError: {e}"}
    except Exception as e:
        return {"ok": False, "error": "http_error", "detail": repr(e)}

# -------------------- LOCK API --------------------
def try_acquire_lock() -> Tuple[bool, str]:
    global _have_lock

    init_lock_settings()

    if not LOCK_URL or not AUTH_TOKEN:
        _have_lock = False
        return False, ""

    r = _post("/lock", {})
    ok = bool(r.get("ok"))
    locked = bool(r.get("locked"))
    owner = str(r.get("owner") or "")

    _have_lock = bool(ok and locked and owner)
    return _have_lock, owner

def confirm_lock() -> bool:
    r = _post("/confirm", {})
    return bool(r.get("ok"))

def release_lock() -> bool:
    global _have_lock
    init_lock_settings()
    if not LOCK_URL or not AUTH_TOKEN:
        _have_lock = False
        return False
    r = _post("/release", {})
    _have_lock = False
    return bool(r.get("ok"))

def _renew_loop() -> None:
    global _have_lock
    while not _stop.is_set():
        time.sleep(RENEW_EVERY_SEC)
        if _stop.is_set():
            break
        r = _post("/renew", {})
        ok = bool(r.get("ok"))
        locked = bool(r.get("locked"))
        error = str(r.get("error") or "")
        if error == "preauth_timeout":
            _have_lock = False
            break
        _have_lock = bool(ok and locked)
        if not _have_lock:
            break

def start_renew_thread() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_renew_loop, daemon=True)
    _thread.start()

def stop_renew_thread() -> None:
    _stop.set()

def have_lock() -> bool:
    return bool(_have_lock)

def get_queue_position() -> Optional[int]:
    init_lock_settings()
    if not LOCK_URL or not AUTH_TOKEN:
        return None
    r = _post("/queue", {})
    if not bool(r.get("ok")):
        return None
    try:
        pos = int(r.get("position"))
        return pos if pos > 0 else None
    except Exception:
        return None

# -------------------- BOT DB --------------------
def botdb_get_admin() -> dict:
    init_lock_settings()
    return _post("/admin/get", {}, use_admin_token=False)

def botdb_set_admin(chat_id: int) -> dict:
    init_lock_settings()
    # admin-only (clientId="admin-bot")
    return _post("/admin/set", {"chatId": int(chat_id)}, use_admin_token=True)

def botdb_users_add(user_id: int, username: str = "") -> dict:
    init_lock_settings()
    # обычно это тоже лучше делать admin-only
    return _post("/users/add", {"userId": str(int(user_id)), "username": (username or "")}, use_admin_token=True)

def botdb_users_has(user_id: int) -> dict:
    init_lock_settings()
    return _post("/users/has", {"userId": str(int(user_id))}, use_admin_token=False)

def botdb_bind_check(user_id: int, device_id: str) -> dict:
    init_lock_settings()
    return _post("/bind/check", {"userId": str(int(user_id)), "deviceId": str(device_id)}, use_admin_token=False)

def botdb_bind_set(user_id: int, device_id: str, username: str = "", force: bool = False) -> dict:
    init_lock_settings()
    # admin-only (clientId="admin-bot")
    return _post(
        "/bind/set",
        {"userId": str(int(user_id)), "deviceId": str(device_id), "username": (username or ""), "force": bool(force)},
        use_admin_token=True,
    )
