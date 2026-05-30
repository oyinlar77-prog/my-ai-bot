"""
Telegram Bot — aiogram 3.x | Railway Webhook
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Funksiyalar:
  - Admin panel (faqat ADMIN_ID ko'radi)
      • AI provayderini almashtirish: Gemini / Groq
      • API kalitlarini yangilash
      • Bot statistikasi
  - Gemini yoki Groq (bitta kalit, rotatsiya yo'q)
  - Telegram Business (Bot Secretary) rejimi
  - Per-user System Prompt (xarakter sozlamasi)
  - Suhbat tarixi (context window)
  - Guruh moderatsiyasi (spam/link)
  - Guruhda AI: @mention yoki reply
  - AI javoblarida markdown belgilar tozalanadi
"""

import asyncio
import json
import logging
import os
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN: str    = os.environ["BOT_TOKEN"]
WEBHOOK_HOST: str = os.environ["WEBHOOK_HOST"]
PORT: int         = int(os.environ.get("PORT", 8080))

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# Admin ID — faqat shu odam admin panelni ko'radi
ADMIN_ID: int = 7595247253

# Boshlangich API kalitlari (Railway Variables dan)
_INIT_GEMINI_KEY: str = os.environ.get("GEMINI_API_KEY", "")
_INIT_GROQ_KEY:   str = os.environ.get("GROQ_API_KEY",   "")

# Model nomlari
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
GROQ_MODEL:   str = os.environ.get("GROQ_MODEL",   "llama3-8b-8192")

HISTORY_LIMIT = 10

# ─────────────────────────────────────────────────────────────────────────────
# Persistent storage — bot_data.json
# ─────────────────────────────────────────────────────────────────────────────
DATA_FILE = Path("bot_data.json")

def _load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_data(data: dict):
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

_db: dict = _load_data()
# Tuzilma:
#   "active_ai"     : "gemini" | "groq"
#   "gemini_key"    : str
#   "groq_key"      : str
#   "characters"    : { str(user_id): str }
#   "group_ai"      : { str(chat_id): bool }
#   "biz_conns"     : { str(owner_id): { "conn_id": str, "owner_name": str } }
#   "biz_secretary" : { str(owner_id): str }
#   "user_count"    : int
_db.setdefault("active_ai",     "gemini")
_db.setdefault("gemini_key",    _INIT_GEMINI_KEY)
_db.setdefault("groq_key",      _INIT_GROQ_KEY)
_db.setdefault("characters",    {})
_db.setdefault("group_ai",      {})
_db.setdefault("biz_conns",     {})
_db.setdefault("biz_secretary", {})
_db.setdefault("user_count",    0)
_save_data(_db)


# ── AI sozlamalari ────────────────────────────────────────────────────────────
def get_active_ai() -> str:
    return _db.get("active_ai", "gemini")

def set_active_ai(provider: str):
    _db["active_ai"] = provider
    _save_data(_db)

def get_gemini_key() -> str:
    return _db.get("gemini_key", "")

def get_groq_key() -> str:
    return _db.get("groq_key", "")

def set_gemini_key(key: str):
    _db["gemini_key"] = key.strip()
    _save_data(_db)

def set_groq_key(key: str):
    _db["groq_key"] = key.strip()
    _save_data(_db)


# ── Foydalanuvchi statistikasi ────────────────────────────────────────────────
def inc_user_count():
    _db["user_count"] = _db.get("user_count", 0) + 1
    _save_data(_db)

def get_user_count() -> int:
    return _db.get("user_count", 0)


# ── Xarakter ─────────────────────────────────────────────────────────────────
def get_character(user_id: int) -> str:
    return _db["characters"].get(str(user_id), "")

def set_character(user_id: int, text: str):
    _db["characters"][str(user_id)] = text
    _save_data(_db)

def del_character(user_id: int):
    _db["characters"].pop(str(user_id), None)
    _save_data(_db)


# ── Guruh AI rejimi ───────────────────────────────────────────────────────────
def is_group_ai_on(chat_id: int) -> bool:
    return _db["group_ai"].get(str(chat_id), False)

def toggle_group_ai(chat_id: int) -> bool:
    new_val = not _db["group_ai"].get(str(chat_id), False)
    _db["group_ai"][str(chat_id)] = new_val
    _save_data(_db)
    return new_val


# ── Business connections ──────────────────────────────────────────────────────
def save_biz_conn(owner_id: int, conn_id: str, owner_name: str):
    _db["biz_conns"][str(owner_id)] = {"conn_id": conn_id, "owner_name": owner_name}
    _save_data(_db)

def remove_biz_conn(owner_id: int):
    _db["biz_conns"].pop(str(owner_id), None)
    _save_data(_db)

def get_biz_conn(owner_id: int) -> Optional[dict]:
    return _db["biz_conns"].get(str(owner_id))

def get_biz_conn_id_by_conn(conn_id: str) -> Optional[int]:
    for uid, info in _db["biz_conns"].items():
        if info.get("conn_id") == conn_id:
            return int(uid)
    return None

def get_biz_secretary(owner_id: int) -> str:
    return _db["biz_secretary"].get(str(owner_id), "")

def set_biz_secretary(owner_id: int, text: str):
    _db["biz_secretary"][str(owner_id)] = text
    _save_data(_db)

def del_biz_secretary(owner_id: int):
    _db["biz_secretary"].pop(str(owner_id), None)
    _save_data(_db)


# ─────────────────────────────────────────────────────────────────────────────
# Conversation history
# ─────────────────────────────────────────────────────────────────────────────
_histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))

def history_add(uid: int, role: str, content: str):
    _histories[uid].append({"role": role, "content": content})

def history_get(uid: int) -> list[dict]:
    return list(_histories[uid])

def history_clear(uid: int):
    _histories[uid].clear()


# ─────────────────────────────────────────────────────────────────────────────
# System Prompt builder
# ─────────────────────────────────────────────────────────────────────────────
_NO_MARKDOWN = (
    "QATTIQ QOIDA: Javobingda hech qachon quyidagi belgilarni ishlatma: "
    "*, **, #, ##, ###, ~, ~~, `, ```, _, __, &, $, ^, |, >, >>. "
    "Markdown formatlash YOQ. Faqat oddiy matn. "
    "Zarur bulganda emoji ishlat, ortiqcha emas.\n"
    "Javoblar aniq, tushunarli va qulay oqilishi kerak.\n\n"
)

def build_system_prompt(user_id: int) -> str:
    custom = get_character(user_id)
    suffix = ("Foydalanuvchi korsatmasi:\n" + custom) if custom \
             else "Sen foydali, dostona va aqlli AI yordamchisan."
    return _NO_MARKDOWN + suffix

def build_secretary_prompt(owner_id: int, owner_name: str) -> str:
    custom = get_biz_secretary(owner_id)
    base = (
        _NO_MARKDOWN
        + "Sen " + owner_name + " nomidan javob beradigan aqlli AI kotibsan.\n"
        + "Xabarlarni tahlil qil va eganing nomidan mos, professional javob yoz.\n"
        + "Javoblar qisqa, aniq va odamiy bolsin.\n\n"
    )
    if custom:
        return base + "Eganing shaxsiy korsatmasi:\n" + custom
    return base + "Standart kotib rejimi: xushmuomala, professional, qisqa javob ber."


# ─────────────────────────────────────────────────────────────────────────────
# Response cleaner
# ─────────────────────────────────────────────────────────────────────────────
_RE_CODE     = re.compile(r"```[\s\S]*?```|`[^`]+`")
_RE_BOLD     = re.compile(r"\*\*(.+?)\*\*")
_RE_ITALIC   = re.compile(r"\*(.+?)\*")
_RE_STRIKE   = re.compile(r"~~(.+?)~~")
_RE_UNDER2   = re.compile(r"__(.+?)__")
_RE_UNDER    = re.compile(r"_(.+?)_")
_RE_HEADING  = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_LEFTOVER = re.compile(r"[*#~`^|>]")
_RE_NEWLINES = re.compile(r"\n{3,}")
_RE_SPACES   = re.compile(r"^ +", re.MULTILINE)

def clean_response(text: str) -> str:
    text = _RE_CODE.sub(lambda m: m.group(0).replace("`", ""), text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC.sub(r"\1", text)
    text = _RE_STRIKE.sub(r"\1", text)
    text = _RE_UNDER2.sub(r"\1", text)
    text = _RE_UNDER.sub(r"\1", text)
    text = _RE_HEADING.sub("", text)
    text = _RE_LEFTOVER.sub("", text)
    text = _RE_NEWLINES.sub("\n\n", text)
    text = _RE_SPACES.sub("", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# AI Providers — bitta kalit, rotatsiya yo'q
# ─────────────────────────────────────────────────────────────────────────────

def _gemini_contents(hist: list[dict], user_text: str) -> list[dict]:
    contents = []
    for h in hist:
        role = "user" if h["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    return contents


async def ask_gemini(system: str, hist: list[dict], user_text: str) -> Optional[str]:
    key = get_gemini_key()
    if not key:
        logger.error("Gemini: kalit yo'q (GEMINI_API_KEY bo'sh)")
        return None

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={key}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": _gemini_contents(hist, user_text),
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1024,
            "candidateCount": 1,
        },
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                raw = await resp.text()

                if resp.status == 429:
                    logger.warning("Gemini: limit tugadi (429) — boshqa so'rov qabul qilinmaydi")
                    return None
                if resp.status in (400, 401, 403):
                    logger.error(f"Gemini: HTTP {resp.status} — kalit xato yoki blok → {raw[:200]}")
                    return None
                if resp.status != 200:
                    logger.error(f"Gemini: HTTP {resp.status} → {raw[:200]}")
                    return None

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.error(f"Gemini: JSON parse xato → {raw[:200]}")
                    return None

                block = data.get("promptFeedback", {}).get("blockReason")
                if block:
                    logger.warning(f"Gemini: bloklandi → {block}")
                    return None

                candidates = data.get("candidates", [])
                if not candidates:
                    logger.error(f"Gemini: candidates bosh → {data}")
                    return None

                try:
                    text = candidates[0]["content"]["parts"][0]["text"]
                    if text and text.strip():
                        logger.info(f"Gemini: javob olindi ({len(text)} belgi)")
                        return text
                    logger.warning("Gemini: bosh matn qaytdi")
                    return None
                except (KeyError, IndexError) as e:
                    logger.error(f"Gemini: javob strukturasi notogri → {e}")
                    return None

    except asyncio.TimeoutError:
        logger.error("Gemini: timeout (40s)")
    except aiohttp.ClientError as e:
        logger.error(f"Gemini: tarmoq xato → {e}")
    except Exception as e:
        logger.error(f"Gemini: kutilmagan xato → {e}")
    return None


async def ask_groq(system: str, hist: list[dict], user_text: str) -> Optional[str]:
    key = get_groq_key()
    if not key:
        logger.error("Groq: kalit yo'q (GROQ_API_KEY bo'sh)")
        return None

    messages = [{"role": "system", "content": system}]
    for h in hist:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model":       GROQ_MODEL,
        "messages":    messages,
        "max_tokens":  1024,
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                raw = await resp.text()

                if resp.status == 429:
                    logger.warning("Groq: limit tugadi (429) — boshqa so'rov qabul qilinmaydi")
                    return None
                if resp.status in (400, 401, 403):
                    logger.error(f"Groq: HTTP {resp.status} — kalit xato → {raw[:200]}")
                    return None
                if resp.status != 200:
                    logger.error(f"Groq: HTTP {resp.status} → {raw[:200]}")
                    return None

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.error(f"Groq: JSON parse xato → {raw[:200]}")
                    return None

                choices = data.get("choices", [])
                if not choices:
                    logger.error(f"Groq: choices bosh → {data}")
                    return None

                text = choices[0].get("message", {}).get("content", "")
                if text and text.strip():
                    logger.info(f"Groq: javob olindi ({len(text)} belgi)")
                    return text
                logger.warning("Groq: bosh matn qaytdi")
                return None

    except asyncio.TimeoutError:
        logger.error("Groq: timeout (40s)")
    except aiohttp.ClientError as e:
        logger.error(f"Groq: tarmoq xato → {e}")
    except Exception as e:
        logger.error(f"Groq: kutilmagan xato → {e}")
    return None


async def _call_ai(system: str, hist: list[dict], user_text: str) -> str:
    """
    Faqat aktiv AI provayderini chaqiradi.
    Limit tugasa yoki xato bolsa — aniq xabar qaytaradi.
    """
    provider = get_active_ai()
    answer   = None

    if provider == "gemini":
        answer = await ask_gemini(system, hist, user_text)
        if not answer:
            return (
                "Gemini AI hozirda javob bermadi.\n"
                "Sabab: limit tugagan yoki kalit xato bolishi mumkin.\n"
                "Admin /admin → AI almashtirishdan Groq ga otishi mumkin."
            )
    else:
        answer = await ask_groq(system, hist, user_text)
        if not answer:
            return (
                "Groq AI hozirda javob bermadi.\n"
                "Sabab: limit tugagan yoki kalit xato bolishi mumkin.\n"
                "Admin /admin → AI almashtirishdan Gemini ga otishi mumkin."
            )

    return clean_response(answer)


async def ask_ai(user_id: int, user_text: str) -> str:
    history_add(user_id, "user", user_text)
    system = build_system_prompt(user_id)
    hist   = history_get(user_id)[:-1]
    answer = await _call_ai(system, hist, user_text)
    history_add(user_id, "assistant", answer)
    return answer


async def ask_ai_secretary(owner_id: int, owner_name: str,
                           sender_id: int, user_text: str) -> str:
    history_add(sender_id, "user", user_text)
    system = build_secretary_prompt(owner_id, owner_name)
    hist   = history_get(sender_id)[:-1]
    answer = await _call_ai(system, hist, user_text)
    history_add(sender_id, "assistant", answer)
    return answer


# ─────────────────────────────────────────────────────────────────────────────
# Moderation
# ─────────────────────────────────────────────────────────────────────────────
_LINK_RE = re.compile(
    r"(https?://\S+|t\.me/\S+|@\w{5,}|www\.\S+|bit\.ly/\S+|tinyurl\.com/\S+)",
    re.IGNORECASE,
)
_AD_RE = re.compile(
    r"\b(reklama|sotiladi|kupish|chegirma|bepul|obuna|"
    r"реклама|продаётся|купить|скидка|бесплатно|подписывайтесь|"
    r"free\s+money|discount|promo)\b",
    re.IGNORECASE,
)

def is_spam(text: str) -> tuple[bool, str]:
    if _LINK_RE.search(text): return True, "Havola/link"
    if _AD_RE.search(text):   return True, "Reklama matni"
    return False, ""

async def delete_and_warn(msg: Message, reason: str):
    mention = f'<a href="tg://user?id={msg.from_user.id}">{msg.from_user.full_name}</a>'
    try:
        await msg.delete()
    except Exception:
        pass
    sent = await msg.answer(
        f"Ogohlantirish: {mention}\n"
        f"Xabaringiz ochirildi. Sabab: {reason} taqiqlangan.\n"
        "Guruhda reklama va havolalar joylash mumkin emas.",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(30)
    try:
        await sent.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Admin panel inline klaviaturalar
# ─────────────────────────────────────────────────────────────────────────────
def admin_main_kb() -> InlineKeyboardMarkup:
    provider = get_active_ai()
    gem_mark  = "✅ " if provider == "gemini" else ""
    groq_mark = "✅ " if provider == "groq"   else ""
    gem_key   = get_gemini_key()
    groq_key  = get_groq_key()
    gem_info  = ("..." + gem_key[-6:]) if gem_key else "YOQ"
    groq_info = ("..." + groq_key[-6:]) if groq_key else "YOQ"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{gem_mark}Gemini ({gem_info})",
                callback_data="admin_set_gemini",
            ),
            InlineKeyboardButton(
                text=f"{groq_mark}Groq ({groq_info})",
                callback_data="admin_set_groq",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Gemini kalitini yangilash",
                callback_data="admin_upd_gemini_key",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Groq kalitini yangilash",
                callback_data="admin_upd_groq_key",
            ),
        ],
        [
            InlineKeyboardButton(text="Statistika", callback_data="admin_stats"),
            InlineKeyboardButton(text="AI test",    callback_data="admin_test"),
        ],
        [
            InlineKeyboardButton(text="Yopish", callback_data="admin_close"),
        ],
    ])

def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Orqaga", callback_data="admin_back")]
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Foydalanuvchi klaviaturalari
# ─────────────────────────────────────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Mening xarakterim")],
        [
            KeyboardButton(text="Xarakterimni korish"),
            KeyboardButton(text="Xarakterimni ochirish"),
        ],
        [KeyboardButton(text="Suhbatni tozalash")],
        [KeyboardButton(text="Kotib sozlamalari")],
    ],
    resize_keyboard=True,
)

BIZ_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Kotib korsatmasini ornatish")],
        [
            KeyboardButton(text="Kotib korsatmasini korish"),
            KeyboardButton(text="Kotib korsatmasini ochirish"),
        ],
        [KeyboardButton(text="Orqaga")],
    ],
    resize_keyboard=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────────────────────────────
class CharForm(StatesGroup):
    waiting = State()

class SecretaryForm(StatesGroup):
    waiting = State()

class AdminForm(StatesGroup):
    gemini_key = State()
    groq_key   = State()


# ─────────────────────────────────────────────────────────────────────────────
# Helper: admin tekshiruvi
# ─────────────────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────
router = Router()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

def _admin_status_text() -> str:
    provider  = get_active_ai()
    gem_key   = get_gemini_key()
    groq_key  = get_groq_key()
    prov_name = "Gemini" if provider == "gemini" else "Groq"
    gem_disp  = ("..." + gem_key[-8:])  if gem_key  else "kiritilmagan"
    groq_disp = ("..." + groq_key[-8:]) if groq_key else "kiritilmagan"
    users     = get_user_count()

    return (
        "Admin paneli\n\n"
        f"Faol AI: {prov_name}\n\n"
        f"Gemini kalit: {gem_disp}\n"
        f"Groq kalit:   {groq_disp}\n\n"
        f"Jami foydalanuvchilar: {users}\n\n"
        "AI almashtirish uchun tugmani bosing:"
    )


@router.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    if not msg.from_user or not is_admin(msg.from_user.id):
        return   # Admin emas — hech narsa ko'rsatmaymiz, javob ham bermaymiz
    await state.clear()
    await msg.answer(_admin_status_text(), reply_markup=admin_main_kb())


# ── Callback: AI almashtirish ─────────────────────────────────────────────────
@router.callback_query(F.data == "admin_set_gemini")
async def cb_set_gemini(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q.", show_alert=True)
        return
    set_active_ai("gemini")
    await cb.answer("Gemini faollashtirildi!", show_alert=True)
    await cb.message.edit_text(_admin_status_text(), reply_markup=admin_main_kb())


@router.callback_query(F.data == "admin_set_groq")
async def cb_set_groq(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q.", show_alert=True)
        return
    set_active_ai("groq")
    await cb.answer("Groq faollashtirildi!", show_alert=True)
    await cb.message.edit_text(_admin_status_text(), reply_markup=admin_main_kb())


# ── Callback: Kalit yangilash ─────────────────────────────────────────────────
@router.callback_query(F.data == "admin_upd_gemini_key")
async def cb_upd_gemini(cb: CallbackQuery, state: FSMContext):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(AdminForm.gemini_key)
    await cb.message.edit_text(
        "Yangi Gemini API kalitini yuboring:\n\n"
        "Olish: aistudio.google.com → Get API key",
        reply_markup=admin_back_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "admin_upd_groq_key")
async def cb_upd_groq(cb: CallbackQuery, state: FSMContext):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(AdminForm.groq_key)
    await cb.message.edit_text(
        "Yangi Groq API kalitini yuboring:\n\n"
        "Olish: console.groq.com → API Keys",
        reply_markup=admin_back_kb(),
    )
    await cb.answer()


@router.message(AdminForm.gemini_key, F.text)
async def admin_receive_gemini_key(msg: Message, state: FSMContext):
    if not msg.from_user or not is_admin(msg.from_user.id):
        return
    key = msg.text.strip()
    if len(key) < 10:
        await msg.answer("Kalit juda qisqa. Qaytadan yuboring.")
        return
    set_gemini_key(key)
    await state.clear()
    try:
        await msg.delete()
    except Exception:
        pass
    await msg.answer(
        "Gemini kalit saqlandi.\n"
        f"Oxirgi 8 belgi: ...{key[-8:]}\n\n"
        "Admin panelga qaytish uchun /admin",
    )
    logger.info(f"Admin: Gemini kalit yangilandi (...{key[-8:]})")


@router.message(AdminForm.groq_key, F.text)
async def admin_receive_groq_key(msg: Message, state: FSMContext):
    if not msg.from_user or not is_admin(msg.from_user.id):
        return
    key = msg.text.strip()
    if len(key) < 10:
        await msg.answer("Kalit juda qisqa. Qaytadan yuboring.")
        return
    set_groq_key(key)
    await state.clear()
    try:
        await msg.delete()
    except Exception:
        pass
    await msg.answer(
        "Groq kalit saqlandi.\n"
        f"Oxirgi 8 belgi: ...{key[-8:]}\n\n"
        "Admin panelga qaytish uchun /admin",
    )
    logger.info(f"Admin: Groq kalit yangilandi (...{key[-8:]})")


# ── Callback: Statistika ──────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_stats")
async def cb_stats(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q.", show_alert=True)
        return
    users    = get_user_count()
    provider = get_active_ai()
    biz_cnt  = len(_db.get("biz_conns", {}))
    chars    = len(_db.get("characters", {}))

    text = (
        "Statistika\n\n"
        f"Jami foydalanuvchilar: {users}\n"
        f"Business ulanishlar:   {biz_cnt}\n"
        f"Xarakter sozlaganlar:  {chars}\n"
        f"Faol AI:               {provider.capitalize()}\n"
    )
    await cb.message.edit_text(text, reply_markup=admin_back_kb())
    await cb.answer()


# ── Callback: AI test ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_test")
async def cb_test(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q.", show_alert=True)
        return
    await cb.message.edit_text("AI sinab korilmoqda...", reply_markup=None)
    await cb.answer()

    provider = get_active_ai()
    system   = "Qisqa javob ber. Maxsus belgi ishlatma."
    prompt   = "Faqat 'Ishlayapman' deb yoz."

    if provider == "gemini":
        result = await ask_gemini(system, [], prompt)
    else:
        result = await ask_groq(system, [], prompt)

    if result:
        status = f"{provider.capitalize()} ishlaydi.\nJavob: {result[:100]}"
    else:
        status = (
            f"{provider.capitalize()} ISHLAMADI.\n"
            "Kalit notogri yoki limit tugagan.\n"
            "Kalitni yangilang yoki boshqa AI ga oting."
        )
    await cb.message.edit_text(status, reply_markup=admin_back_kb())


# ── Callback: Orqaga / Yopish ─────────────────────────────────────────────────
@router.callback_query(F.data == "admin_back")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q.", show_alert=True)
        return
    await state.clear()
    await cb.message.edit_text(_admin_status_text(), reply_markup=admin_main_kb())
    await cb.answer()


@router.callback_query(F.data == "admin_close")
async def cb_close(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q.", show_alert=True)
        return
    await cb.message.delete()
    await cb.answer("Panel yopildi.")


# ══════════════════════════════════════════════════════════════════════════════
# BUSINESS MODE HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

@router.business_connection()
async def on_business_connection(event: BusinessConnection, bot: Bot):
    owner      = event.user
    owner_id   = owner.id
    owner_name = owner.full_name or owner.first_name or "Ega"

    if event.is_enabled:
        save_biz_conn(owner_id, event.id, owner_name)
        logger.info(f"Business ulanish: {owner_name} (id={owner_id})")
        try:
            await bot.send_message(
                chat_id=owner_id,
                text=(
                    f"Salom, {owner_name}!\n\n"
                    "Men endi sizning Business kotibbingizman.\n"
                    "Sizga yozgan har bir odamga men avtomatik AI javob beraman.\n\n"
                    "Kotib uslubini sozlash: 'Kotib sozlamalari' tugmasi."
                ),
                reply_markup=MAIN_KB,
            )
        except Exception as e:
            logger.warning(f"Business welcome yuborilmadi: {e}")
    else:
        remove_biz_conn(owner_id)
        logger.info(f"Business uzilish: {owner_name} (id={owner_id})")
        try:
            await bot.send_message(
                chat_id=owner_id,
                text="Business kotib rejimi ochirildi.",
                reply_markup=MAIN_KB,
            )
        except Exception as e:
            logger.warning(f"Business uzilish xabari yuborilmadi: {e}")


@router.message(F.business_connection_id.is_not(None), F.text)
async def on_business_message(msg: Message, bot: Bot):
    conn_id = msg.business_connection_id
    if not conn_id or not msg.from_user or not msg.text:
        return

    owner_id = get_biz_conn_id_by_conn(conn_id)
    if owner_id is None:
        logger.warning(f"Business conn topilmadi: {conn_id}")
        return

    owner_info = get_biz_conn(owner_id)
    owner_name = owner_info.get("owner_name", "Ega") if owner_info else "Ega"
    sender_id  = msg.from_user.id

    if sender_id == owner_id:
        return

    logger.info(f"Business xabar: {msg.from_user.full_name} → {owner_name}")

    try:
        await bot.send_chat_action(
            chat_id=msg.chat.id,
            action="typing",
            business_connection_id=conn_id,
        )
    except Exception:
        pass

    answer = await ask_ai_secretary(owner_id, owner_name, sender_id, msg.text)

    try:
        await bot.send_message(
            chat_id=msg.chat.id,
            text=answer,
            business_connection_id=conn_id,
        )
    except Exception as e:
        logger.error(f"Business javob yuborishda xato: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ODDIY BUYRUQLAR
# ══════════════════════════════════════════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    if not msg.from_user:
        return

    uid  = msg.from_user.id
    name = msg.from_user.first_name or "Dost"

    # Yangi foydalanuvchi hisoblagichi
    inc_user_count()

    provider  = get_active_ai().capitalize()
    biz_conn  = get_biz_conn(uid)
    biz_holat = "Ulangan" if biz_conn else "Ulanmagan"

    text = (
        f"Salom, {name}! Men aqlli AI yordamchiman.\n\n"
        f"Faol AI: {provider}\n"
        f"Business kotib: {biz_holat}\n\n"
        "Nima qila olaman:\n"
        "  Shaxsiy chatda AI bilan suhbat\n"
        "  Business kotib (auto-javob)\n"
        "  Xarakter sozlamasi\n"
        "  Guruhlarda spam moderatsiyasi\n\n"
        "Savol yozing yoki tugmalardan foydalaning."
    )
    await msg.answer(text, reply_markup=MAIN_KB)


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "Buyruqlar:\n\n"
        "/start    — Boshlash\n"
        "/help     — Yordam\n"
        "/clear    — Suhbat tarixini tozalash\n"
        "/groupai  — Guruhda AI rejimi (admin)\n"
        "/bizstatus — Business kotib holati\n\n"
        "Tugmalar:\n"
        "  'Mening xarakterim'  — AI uslubini orgating\n"
        "  'Kotib sozlamalari'  — Business kotib korsatmasi\n"
        "  'Suhbatni tozalash'  — Xotira tozalanadi",
    )


@router.message(Command("clear"))
async def cmd_clear(msg: Message):
    if msg.from_user:
        history_clear(msg.from_user.id)
    await msg.answer("Suhbat tarixi tozalandi. Yangi suhbat boshlaylik!")


@router.message(Command("bizstatus"))
async def cmd_bizstatus(msg: Message):
    if not msg.from_user:
        return
    conn = get_biz_conn(msg.from_user.id)
    if conn:
        sec     = get_biz_secretary(msg.from_user.id)
        sec_txt = (sec[:80] + "...") if len(sec) > 80 else (sec or "Standart (berilmagan)")
        await msg.answer(
            "Business kotib: Faol\n\n"
            f"Kotib korsatmasi: {sec_txt}"
        )
    else:
        await msg.answer(
            "Business kotib: Ulanmagan\n\n"
            "Ulanish: Telegram → Sozlamalar → Business → Chatbotlar."
        )


@router.message(
    Command("groupai"),
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def cmd_groupai(msg: Message):
    if not msg.from_user:
        return
    try:
        m = await msg.chat.get_member(msg.from_user.id)
        if m.status not in ("administrator", "creator"):
            await msg.reply("Bu buyruq faqat guruh adminlari uchun.")
            return
    except Exception:
        return
    state  = toggle_group_ai(msg.chat.id)
    holat  = "yoqildi" if state else "ochirildi"
    izoh   = "Barcha xabarlarga AI javob beradi." if state \
             else "Faqat @mention yoki reply orqali javob beradi."
    await msg.reply(f"Guruh AI rejimi {holat}.\n{izoh}")


# ── Tugmalar: Xarakter ────────────────────────────────────────────────────────
@router.message(F.text == "Mening xarakterim")
async def btn_set_character(msg: Message, state: FSMContext):
    await state.set_state(CharForm.waiting)
    current = get_character(msg.from_user.id) if msg.from_user else ""
    hint    = ("\n\nHozirgi xarakter:\n" + current) if current else ""
    await msg.answer(
        "AI qanday uslubda javob bersin? Namunalar:\n\n"
        "  'Mening nomimdan juda jiddiy va rasmiy tonda gaplash'\n"
        "  'Dostona, hazillashib va kochada gaplash'\n"
        "  'Har doim qisqa va aniq, ortiqcha gap yozma'\n"
        "  'Ingliz tilida, professional ohangda'\n\n"
        "Korsatmangizni yuboring:" + hint,
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(CharForm.waiting, F.text)
async def receive_character(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if len(text) < 5:
        await msg.answer("Juda qisqa. Kamida 5 belgi yozing.")
        return
    if msg.from_user:
        set_character(msg.from_user.id, text)
    await state.clear()
    await msg.answer(
        "Xarakter saqlandi!\n\n" + text + "\n\nEndi AI shu korsatma bilan javob beradi.",
        reply_markup=MAIN_KB,
    )

@router.message(F.text == "Xarakterimni korish")
async def btn_view_character(msg: Message):
    char = get_character(msg.from_user.id) if msg.from_user else ""
    if char:
        await msg.answer("Joriy xarakter:\n\n" + char)
    else:
        await msg.answer("Xarakter sozlanmagan. Standart AI rejimi ishlayapti.")

@router.message(F.text == "Xarakterimni ochirish")
async def btn_delete_character(msg: Message):
    if msg.from_user and get_character(msg.from_user.id):
        del_character(msg.from_user.id)
        await msg.answer("Xarakter ochirildi. Standart AI rejimiga qaytildi.")
    else:
        await msg.answer("Xarakter sozlanmagan edi.")

@router.message(F.text == "Suhbatni tozalash")
async def btn_clear_history(msg: Message):
    if msg.from_user:
        history_clear(msg.from_user.id)
    await msg.answer("Suhbat tarixi tozalandi. Yangi suhbat boshlaylik!")


# ── Tugmalar: Kotib ───────────────────────────────────────────────────────────
@router.message(F.text == "Kotib sozlamalari")
async def btn_biz_menu(msg: Message):
    conn   = get_biz_conn(msg.from_user.id) if msg.from_user else None
    holat  = "Faol" if conn else "Ulanmagan"
    await msg.answer(
        f"Business kotib sozlamalari\n\nHolat: {holat}\n\n"
        "Kotib korsatmasi — AI mijozlarga sizning nomingizdan javob beradi.",
        reply_markup=BIZ_KB,
    )

@router.message(F.text == "Kotib korsatmasini ornatish")
async def btn_set_secretary(msg: Message, state: FSMContext):
    await state.set_state(SecretaryForm.waiting)
    current = get_biz_secretary(msg.from_user.id) if msg.from_user else ""
    hint    = ("\n\nHozirgi korsatma:\n" + current) if current else ""
    await msg.answer(
        "Kotibingiz qanday ishlashini yozing. Namunalar:\n\n"
        "  'Mening nomimdan rasmiy, qisqa javob ber'\n"
        "  'Dostona va quvnoq ohangda javob ber'\n"
        "  'Har doim uchrashuvni tayinlashga harakat qil'\n\n"
        "Korsatmangizni yuboring:" + hint,
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(SecretaryForm.waiting, F.text)
async def receive_secretary(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if len(text) < 5:
        await msg.answer("Juda qisqa. Kamida 5 belgi yozing.")
        return
    if msg.from_user:
        set_biz_secretary(msg.from_user.id, text)
    await state.clear()
    await msg.answer(
        "Kotib korsatmasi saqlandi!\n\n" + text,
        reply_markup=BIZ_KB,
    )

@router.message(F.text == "Kotib korsatmasini korish")
async def btn_view_secretary(msg: Message):
    sec = get_biz_secretary(msg.from_user.id) if msg.from_user else ""
    if sec:
        await msg.answer("Joriy kotib korsatmasi:\n\n" + sec)
    else:
        await msg.answer("Kotib korsatmasi berilmagan. Standart professional kotib ishlaydi.")

@router.message(F.text == "Kotib korsatmasini ochirish")
async def btn_delete_secretary(msg: Message):
    if msg.from_user and get_biz_secretary(msg.from_user.id):
        del_biz_secretary(msg.from_user.id)
        await msg.answer("Kotib korsatmasi ochirildi. Standart rejimga qaytildi.")
    else:
        await msg.answer("Kotib korsatmasi berilmagan edi.")

@router.message(F.text == "Orqaga")
async def btn_back(msg: Message):
    await msg.answer("Asosiy menyu.", reply_markup=MAIN_KB)


# ── Guruh handler ─────────────────────────────────────────────────────────────
@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
)
async def group_handler(msg: Message, bot: Bot):
    if not msg.text or not msg.from_user:
        return

    try:
        member   = await msg.chat.get_member(msg.from_user.id)
        is_grp_admin = member.status in ("administrator", "creator")
    except Exception:
        is_grp_admin = False

    if not is_grp_admin:
        spam, reason = is_spam(msg.text)
        if spam:
            await delete_and_warn(msg, reason)
            return

    bot_info     = await bot.get_me()
    bot_username = (bot_info.username or "").lower()
    mentioned    = f"@{bot_username}" in msg.text.lower()
    reply_to_me  = (
        msg.reply_to_message is not None
        and msg.reply_to_message.from_user is not None
        and msg.reply_to_message.from_user.id == bot_info.id
    )

    if mentioned or reply_to_me or is_group_ai_on(msg.chat.id):
        clean_text = re.sub(
            rf"@{re.escape(bot_username)}", "", msg.text, flags=re.IGNORECASE
        ).strip() or "Salom!"
        await bot.send_chat_action(msg.chat.id, "typing")
        thinking = await msg.reply("...")
        answer   = await ask_ai(msg.from_user.id, clean_text)
        await thinking.delete()
        await msg.reply(answer)


# ── Shaxsiy chat ──────────────────────────────────────────────────────────────
@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def private_ai(msg: Message, state: FSMContext):
    if not msg.from_user or not msg.text:
        return
    await msg.bot.send_chat_action(msg.chat.id, "typing")
    thinking = await msg.answer("...")
    answer   = await ask_ai(msg.from_user.id, msg.text)
    await thinking.delete()
    await msg.answer(answer)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook app
# ─────────────────────────────────────────────────────────────────────────────
async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start",     description="Botni ishga tushirish"),
        BotCommand(command="help",      description="Yordam"),
        BotCommand(command="clear",     description="Suhbat tarixini tozalash"),
        BotCommand(command="bizstatus", description="Business kotib holati"),
        BotCommand(command="groupai",   description="Guruhda AI rejimi (admin)"),
    ])
    logger.info(f"Webhook ornatildi: {WEBHOOK_URL}")
    logger.info(f"Faol AI: {get_active_ai()}")
    logger.info(f"Gemini kalit: {'bor' if get_gemini_key() else 'YOQ'}")
    logger.info(f"Groq kalit:   {'bor' if get_groq_key()   else 'YOQ'}")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logger.info("Webhook ochirildi.")


def main():
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    logger.info(f"Server: 0.0.0.0:{PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
