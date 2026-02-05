import asyncio
import json
import os
import secrets
import sys
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_ID

# ===================== PATHS (onefile-safe + user-writable) =====================
def _res_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def _exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _user_data_dir(app_name: str = "XyesosBeta") -> str:
    candidates = [
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
        os.path.expanduser("~"),
        os.getcwd(),
    ]
    base = next((c for c in candidates if c and str(c).strip()), os.getcwd())
    d = os.path.join(str(base).strip(), app_name)
    os.makedirs(d, exist_ok=True)
    return d

RES_DIR = _res_dir()
EXE_DIR = _exe_dir()
DATA_DIR = _user_data_dir("XyesosBeta")

# ВАЖНО для импорта cf_lock/config:
if EXE_DIR not in sys.path:
    sys.path.insert(0, EXE_DIR)
if RES_DIR not in sys.path:
    sys.path.insert(1, RES_DIR)

import cf_lock  # noqa: E402

STATE_PATH = os.path.join(DATA_DIR, "auth_state.json")
LOCK_PATH  = os.path.join(DATA_DIR, "bot_process.lock")
LOG_PATH   = os.path.join(DATA_DIR, "bot.log")

def _log(*args) -> None:
    try:
        s = " ".join(str(a) for a in args)
        print(s, flush=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass

# --------------------- DEBUG PRINTS ---------------------
try:
    _log("CF_LOCK IMPORTED FROM:", getattr(cf_lock, "__file__", "??"))
    cf_lock.init_lock_settings()
    _log("LOCK_URL:", getattr(cf_lock, "LOCK_URL", ""))
    _log("HAS_AUTH_TOKEN:", bool(getattr(cf_lock, "AUTH_TOKEN", "")))
except Exception as e:
    _log("[bot] cf_lock init debug failed:", repr(e))

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

def acquire_process_lock() -> bool:
    try:
        if os.path.exists(LOCK_PATH):
            try:
                with open(LOCK_PATH, "r", encoding="utf-8") as f:
                    old_pid = int((f.read() or "0").strip() or "0")
            except Exception:
                old_pid = 0

            if old_pid and _pid_alive(old_pid):
                _log(f"[bot] Another instance is running (pid={old_pid}). Exit.")
                return False

            try:
                os.remove(LOCK_PATH)
            except Exception:
                pass

        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True

    except Exception as e:
        _log("[bot] acquire lock error:", repr(e))
        return False

def release_process_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass

def read_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def write_state(data: dict) -> None:
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.replace(tmp, STATE_PATH)
        except Exception:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _ensure_device_id() -> str:
    try:
        cf_lock.init_lock_settings()
    except Exception:
        pass
    return cf_lock.get_device_id()

def normalize_username(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("@"):
        s = s[1:]
    return s.lower()

def make_request_id() -> str:
    return secrets.token_hex(8)

def admin_kb(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [   InlineKeyboardButton(text="✅ Выдать ключ", callback_data=f"approve:{request_id}"),
                InlineKeyboardButton(text="❌ Отказать", callback_data=f"deny:{request_id}")]])

def admin_rebind_kb(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔁 Перепривязать и выдать", callback_data=f"force:{request_id}"),
                InlineKeyboardButton(text="❌ Отказать", callback_data=f"deny:{request_id}"),
            ]
        ]
    )

def user_rebind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Запросить перепривязку", callback_data="user_rebind")]])

async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: float = 3.0) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        _log(f"[bot] delete_message failed chat_id={chat_id} message_id={message_id}: {repr(e)}")

def _is_network_error(e: Exception) -> bool:
    return isinstance(e, (TelegramNetworkError, aiohttp.ClientError, OSError))

# ===================== BOT DB HELPERS (Cloudflare) =====================
def _cf_get_admin_chat_id() -> Optional[int]:
    try:
        cf_lock.init_lock_settings()
        rr = cf_lock.botdb_get_admin()
        if not isinstance(rr, dict):
            return None

        chat_id = rr.get("chatId")
        if isinstance(chat_id, (int, float)) and int(chat_id) != 0:
            return int(chat_id)

        admin = rr.get("admin") or {}
        if isinstance(admin, dict):
            chat_id = admin.get("chatId")
            if isinstance(chat_id, (int, float)) and int(chat_id) != 0:
                return int(chat_id)

    except Exception:
        pass
    return None

def _cf_user_allowed(user_id: int) -> bool:
    try:
        cf_lock.init_lock_settings()
        rr = cf_lock.botdb_users_has(int(user_id))
        if not isinstance(rr, dict):
            return False
        return bool(rr.get("allowed") or (rr.get("ok") and rr.get("has")))
    except Exception:
        return False

def _cf_add_user_allowed(user_id: int, username: str) -> None:
    try:
        cf_lock.init_lock_settings()
        cf_lock.botdb_users_add(int(user_id), username or "")
    except Exception:
        pass

def _cf_bind_check(user_id: int, device_id: str) -> Tuple[bool, bool, str]:
    try:
        cf_lock.init_lock_settings()
        rr = cf_lock.botdb_bind_check(int(user_id), device_id)
        if not isinstance(rr, dict) or not bool(rr.get("ok")):
            return False, False, ""
        bound = bool(rr.get("bound"))
        match = bool(rr.get("match"))
        key = str(rr.get("key") or "").strip()
        return bound, match, key
    except Exception:
        return False, False, ""

def _cf_bind_set(user_id: int, device_id: str, username: str, force: bool = False) -> dict:
    try:
        cf_lock.init_lock_settings()
        return cf_lock.botdb_bind_set(int(user_id), device_id, username or "", force=force)
    except Exception as e:
        return {"ok": False, "error": "exception", "detail": repr(e)}

# ===================== STATUS (for GUI) =====================
def set_bot_status(status: str, err: str = "") -> None:
    st = read_state()
    st["bot_status"] = str(status)
    st["bot_last_ts"] = int(time.time())
    if err:
        st["bot_last_err"] = err
    else:
        st.pop("bot_last_err", None)
    write_state(st)

async def _heartbeat(bot: Bot, interval: float = 12.0) -> None:
    while True:
        try:
            await bot.get_me()
            set_bot_status("connected")
        except Exception as e:
            set_bot_status("connecting", repr(e))
        await asyncio.sleep(interval)

# -------------------- pendings helpers (fix overwrite) --------------------
def _get_pendings(st: dict) -> Dict[str, Any]:
    p = st.get("pendings")
    if isinstance(p, dict):
        return p
    return {}

def _set_pending(st: dict, request_id: str, pending: dict) -> None:
    pendings = _get_pendings(st)
    pendings[str(request_id)] = dict(pending)
    st["pendings"] = pendings

def _pop_pending(st: dict, request_id: str) -> None:
    pendings = _get_pendings(st)
    pendings.pop(str(request_id), None)
    st["pendings"] = pendings

async def _run_polling_forever() -> None:
    if not acquire_process_lock():
        return

    backoff = 5
    max_backoff = 30

    while True:
        bot: Bot | None = None
        hb_task: asyncio.Task | None = None

        try:
            st0 = read_state()
            token = (st0.get("bot_token") or "").strip()
            if not token:
                _log(f"[bot] No bot_token in auth_state.json ({STATE_PATH}) -> exit")
                set_bot_status("offline", "no bot_token")
                return

            set_bot_status("connecting")

            bot = Bot(token=token)
            dp = Dispatcher()

            await bot.get_me()
            set_bot_status("connecting", "starting polling")

            hb_task = asyncio.create_task(_heartbeat(bot, interval=12.0))

            @dp.message(Command("id"))
            async def cmd_id(msg: Message):
                await msg.answer(
                    f"Ваш Telegram ID: `{msg.from_user.id}`\n"
                    f"ADMIN_ID в config.py: `{ADMIN_ID}`",
                    parse_mode="Markdown",
                )

            @dp.message(CommandStart())
            async def on_start(msg: Message):
                st = read_state()

                # ===== ADMIN PATH (только по ID) =====
                if int(msg.from_user.id) == int(ADMIN_ID):
                    try:
                        rr = cf_lock.botdb_set_admin(msg.chat.id)
                        _log("[bot] admin/set response:", rr)
                        if bool(rr.get("ok")):
                            sent = await msg.answer("✅ Админ зарегистрирован (Cloudflare).")
                        else:
                            sent = await msg.answer(f"⚠️ Cloudflare ответил ошибкой: {rr}")
                    except Exception as e:
                        _log("[bot] admin/set exception:", repr(e))
                        sent = await msg.answer(f"⚠️ Исключение при вызове Cloudflare: {repr(e)}")

                    asyncio.create_task(_delete_later(bot, sent.chat.id, sent.message_id, 6.0))
                    return

                # ===== USER PATH =====
                expected = normalize_username(st.get("expected_username", ""))
                user_un = normalize_username(msg.from_user.username or "")

                if not expected:
                    await msg.answer(
                        "Приложение ещё не передало ожидаемый username.\n"
                        "Сначала откройте приложение и введите @username."
                    )
                    return

                if not user_un:
                    await msg.answer(
                        "У вас не установлен Telegram username.\n"
                        "Зайдите в настройки Telegram и установите его."
                    )
                    return

                if user_un != expected:
                    await msg.answer(
                        "Username не совпал.\n"
                        f"Ожидаю: @{expected}\n"
                        f"Вы: @{user_un}"
                    )
                    return

                device_id = _ensure_device_id()

                # ✅ Если в allowlist — истина на Cloudflare
                if _cf_user_allowed(msg.from_user.id):
                    bound, match, key = _cf_bind_check(msg.from_user.id, device_id)

                    # ✅ привязан к этому ПК + key есть -> отдаём key
                    if bound and match and key:
                        st["key"] = key
                        st["key_for"] = user_un
                        write_state(st)
                        await msg.answer(
                            "✅ Ключ уже выдан.\n\n"
                            f"Ваш ключ: `{key}`",
                            parse_mode="Markdown",
                        )
                        return

                    # привязан к другому ПК -> заявка на перепривязку
                    if bound and not match:
                        request_id = make_request_id()
                        _set_pending(
                            st,
                            request_id,
                            {
                                "id": request_id,
                                "user_id": msg.from_user.id,
                                "chat_id": msg.chat.id,
                                "username": user_un,
                                "device_id": device_id,
                                "created_at": int(time.time()),
                                "status": "pending_rebind",
                            },
                        )
                        write_state(st)

                        admin_chat_id = _cf_get_admin_chat_id()
                        if admin_chat_id:
                            await bot.send_message(
                                int(admin_chat_id),
                                (
                                    "🔁 Запрос на перепривязку лицензии\n\n"
                                    f"Пользователь: @{user_un}\n"
                                    f"User ID: {msg.from_user.id}\n"
                                    f"Новый Device ID: `{device_id}`\n"
                                    f"Request ID: {request_id}\n\n"
                                    "Разрешить перепривязку и выдать ключ?"
                                ),
                                parse_mode="Markdown",
                                reply_markup=admin_rebind_kb(request_id),
                            )

                        await msg.answer(
                            "⛔ Эта лицензия уже привязана к другому ПК.\n"
                            "✅ Я отправил запрос админу на перепривязку. Ожидайте решения."
                        )
                        return

                    # ещё не привязан -> bind и получаем key от CF
                    if not bound:
                        rr_bind = _cf_bind_set(msg.from_user.id, device_id, user_un, force=False)
                        if not bool(rr_bind.get("ok")):
                            await msg.answer(f"⚠️ Не удалось привязать устройство: {rr_bind}")
                            return

                        key2 = str(rr_bind.get("key") or "").strip()
                        if not key2:
                            await msg.answer(
                                "⚠️ Ключ от Cloudflare не пришёл.\n"
                                "Нажмите кнопку ниже, чтобы запросить перепривязку у администратора.",
                                reply_markup=user_rebind_kb(),
                            )
                            return

                        st["key"] = key2
                        st["key_for"] = user_un
                        write_state(st)

                        await msg.answer(
                            "✅ Ключ выдан автоматически.\n\n"
                            f"Ваш ключ: `{key2}`",
                            parse_mode="Markdown",
                        )
                        return

                    # bound==True, match==True, но key пустой (старые записи) -> предлагаем кнопку
                    await msg.answer(
                        "⚠️ Ключ на сервере не найден.\n"
                        "Нажмите кнопку ниже, чтобы запросить перепривязку у администратора.",
                        reply_markup=user_rebind_kb(),
                    )
                    return

                # fallback: локальный ключ
                if normalize_username(st.get("key_for") or "") == user_un and (st.get("key") or "").strip():
                    await msg.answer(
                        "✅ Ключ уже сохранён локально.\n"
                        f"Ваш ключ: `{st['key']}`",
                        parse_mode="Markdown",
                    )
                    return

                # Заявка на выдачу
                request_id = make_request_id()
                _set_pending(
                    st,
                    request_id,
                    {
                        "id": request_id,
                        "user_id": msg.from_user.id,
                        "chat_id": msg.chat.id,
                        "username": user_un,
                        "device_id": device_id,
                        "created_at": int(time.time()),
                        "status": "pending",
                    },
                )
                write_state(st)

                admin_chat_id = _cf_get_admin_chat_id()
                if not admin_chat_id:
                    await msg.answer("⏳ Админ ещё не зарегистрирован (Cloudflare).")
                    return

                await bot.send_message(
                    int(admin_chat_id),
                    (
                        "🔐 Запрос на выдачу ключа\n\n"
                        f"Пользователь: @{user_un}\n"
                        f"User ID: {msg.from_user.id}\n"
                        f"Device ID: `{device_id}`\n"
                        f"Request ID: {request_id}\n\n"
                        "Разрешить выдачу ключа?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=admin_kb(request_id),
                )
                await msg.answer("⏳ Запрос отправлен администратору. Ожидайте решения.")

            # --- USER кнопка: "🔁 Запросить перепривязку" ---
            @dp.callback_query(F.data == "user_rebind")
            async def on_user_rebind(cb: CallbackQuery):
                if int(cb.from_user.id) == int(ADMIN_ID):
                    await cb.answer("Это кнопка для пользователей.", show_alert=True)
                    return

                st = read_state()

                expected = normalize_username(st.get("expected_username", ""))
                user_un = normalize_username(cb.from_user.username or "")

                if not expected:
                    await cb.answer("Сначала откройте приложение и введите @username.", show_alert=True)
                    return

                if not user_un:
                    await cb.answer("Установите Telegram username в настройках.", show_alert=True)
                    return

                if user_un != expected:
                    await cb.answer(f"Username не совпал. Ожидаю @{expected}.", show_alert=True)
                    return

                device_id = _ensure_device_id()
                request_id = make_request_id()

                _set_pending(
                    st,
                    request_id,
                    {
                        "id": request_id,
                        "user_id": cb.from_user.id,
                        "chat_id": cb.message.chat.id if cb.message else cb.from_user.id,
                        "username": user_un,
                        "device_id": device_id,
                        "created_at": int(time.time()),
                        "status": "pending_rebind",
                    },
                )
                write_state(st)

                admin_chat_id = _cf_get_admin_chat_id()
                if not admin_chat_id:
                    await cb.answer("Админ ещё не зарегистрирован (Cloudflare).", show_alert=True)
                    return

                await cb.bot.send_message(
                    int(admin_chat_id),
                    (
                        "🔁 Запрос на перепривязку лицензии\n\n"
                        f"Пользователь: @{user_un}\n"
                        f"User ID: {cb.from_user.id}\n"
                        f"Новый Device ID: `{device_id}`\n"
                        f"Request ID: {request_id}\n\n"
                        "Разрешить перепривязку и выдать ключ?"
                    ),
                    parse_mode="Markdown",
                    reply_markup=admin_rebind_kb(request_id),
                )

                try:
                    if cb.message:
                        await cb.message.edit_text(
                            "✅ Запрос на перепривязку отправлен администратору.\n"
                            "Ожидайте решения."
                        )
                except Exception:
                    pass

                await cb.answer("Запрос отправлен ✅")

            # --- ADMIN callbacks: approve/deny/force ---
            @dp.callback_query(F.data.regexp(r"^(approve|deny|force):"))
            async def on_admin_callback(cb: CallbackQuery):
                if int(cb.from_user.id) != int(ADMIN_ID):
                    await cb.answer("Недостаточно прав.", show_alert=True)
                    return

                data = (cb.data or "").strip()
                try:
                    action, request_id = data.split(":", 1)
                except ValueError:
                    await cb.answer("Некорректные данные.", show_alert=True)
                    return

                action = action.strip().lower()
                request_id = request_id.strip()

                st = read_state()
                pendings = _get_pendings(st)
                pending: Dict[str, Any] = pendings.get(request_id) or {}

                pending_status = (pending.get("status") or "").strip()
                if not pending or pending.get("id") != request_id or pending_status not in ("pending", "pending_rebind"):
                    await cb.answer("Заявка не найдена или уже обработана.", show_alert=True)
                    return

                user_chat_id = int(pending.get("chat_id") or 0)
                user_un = normalize_username(pending.get("username") or "")
                user_id = int(pending.get("user_id") or 0)
                device_id = str(pending.get("device_id") or "").strip()

                if not user_chat_id or not user_id or not user_un or not device_id:
                    await cb.answer("Данные заявки повреждены.", show_alert=True)
                    return

                # DENY
                if action == "deny":
                    _pop_pending(st, request_id)
                    write_state(st)

                    try:
                        await cb.message.edit_text(
                            "❌ Отказано\n\n"
                            f"Пользователь: @{user_un}\n"
                            f"User ID: {user_id}\n"
                            f"Device ID: `{device_id}`\n"
                            f"Request ID: {request_id}",
                            parse_mode="Markdown",
                            reply_markup=None,
                        )
                    except Exception:
                        pass

                    await cb.bot.send_message(user_chat_id, "❌ Администратор отказал.")
                    await cb.answer("Отказ отправлен ❌")
                    return

                # APPROVE / FORCE
                if action in ("approve", "force"):
                    if pending_status == "pending_rebind" and action == "approve":
                        try:
                            await cb.message.edit_text(
                                "⚠️ Это запрос на перепривязку.\n\n"
                                f"Пользователь: @{user_un}\n"
                                f"User ID: {user_id}\n"
                                f"Новый Device ID: `{device_id}`\n"
                                f"Request ID: {request_id}\n\n"
                                "Нажмите «Перепривязать и выдать».",
                                parse_mode="Markdown",
                                reply_markup=admin_rebind_kb(request_id),
                            )
                        except Exception:
                            pass
                        await cb.answer("Нужен force 🔁", show_alert=True)
                        return

                    bound, match, _ = _cf_bind_check(user_id, device_id)
                    if action == "approve" and bound and not match:
                        try:
                            await cb.message.edit_text(
                                "⚠️ Конфликт привязки устройства!\n\n"
                                f"Пользователь: @{user_un}\n"
                                f"User ID: {user_id}\n"
                                f"Device ID: `{device_id}`\n\n"
                                "Эта лицензия уже привязана к другому устройству.\n"
                                "Если это тот же владелец — нажмите перепривязку.",
                                parse_mode="Markdown",
                                reply_markup=admin_rebind_kb(request_id),
                            )
                        except Exception:
                            pass
                        await cb.answer("Нужна перепривязка 🔁", show_alert=True)
                        return

                    rr_bind = _cf_bind_set(user_id, device_id, user_un, force=(action == "force"))
                    if not bool(rr_bind.get("ok")):
                        await cb.answer("Не удалось привязать устройство.", show_alert=True)
                        try:
                            await cb.message.edit_text(
                                f"⚠️ Ошибка binding: {rr_bind}\n\n"
                                f"Пользователь: @{user_un}\n"
                                f"User ID: {user_id}\n"
                                f"Device ID: `{device_id}`\n"
                                f"Request ID: {request_id}",
                                parse_mode="Markdown",
                                reply_markup=admin_rebind_kb(request_id),
                            )
                        except Exception:
                            pass
                        return

                    _cf_add_user_allowed(user_id, user_un)

                    key = str(rr_bind.get("key") or "").strip()
                    if not key:
                        await cb.answer("Ключ не пришёл от Cloudflare.", show_alert=True)
                        return

                    st["key"] = key
                    st["key_for"] = user_un
                    _pop_pending(st, request_id)
                    write_state(st)

                    await cb.bot.send_message(
                        user_chat_id,
                        "✅ Администратор одобрил.\n\n"
                        f"Ваш ключ: `{key}`",
                        parse_mode="Markdown",
                    )

                    try:
                        await cb.message.edit_text(
                            "✅ Готово\n\n"
                            f"Пользователь: @{user_un}\n"
                            f"User ID: {user_id}\n"
                            f"Device ID: `{device_id}`\n"
                            f"Request ID: {request_id}\n"
                            f"Ключ: `{key}`",
                            parse_mode="Markdown",
                            reply_markup=None,
                        )
                    except Exception:
                        pass

                    await cb.answer("Ключ выдан ✅")
                    return

                await cb.answer("Неизвестное действие.", show_alert=True)

            _log("[bot] started polling")
            backoff = 5

            await dp.start_polling(bot)
            _log("[bot] polling finished")
            return

        except Exception as e:
            if _is_network_error(e):
                _log(f"[bot] network error: {repr(e)} -> retry in {backoff}s")
                set_bot_status("connecting", repr(e))
                await asyncio.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)
                continue

            _log(f"[bot] fatal error: {repr(e)}")
            set_bot_status("connecting", repr(e))
            raise

        finally:
            if hb_task is not None:
                try:
                    hb_task.cancel()
                except Exception:
                    pass

            if bot is not None:
                try:
                    await bot.session.close()
                except Exception:
                    pass

async def main():
    try:
        await _run_polling_forever()
    finally:
        release_process_lock()

if __name__ == "__main__":
    asyncio.run(main())