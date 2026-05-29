"""
Telegram Bot — aiogram 3.x | Railway Webhook
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Funksiyalar:
  - Telegram Business (Bot Secretary) rejimi
      • BusinessConnection: ulanish/uzilish hodisalari
      • Business xabarlarga AI kotib sifatida javob
      • Har egasi uchun alohida kotib tili/xarakteri
  - Oddiy shaxsiy chat AI (Gemini → Groq fallback)
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
from itertools import cycle
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
    BusinessConnection,   # Business ulanish hodisasi
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
WEBHOOK_HOST: str = os.environ["WEBHOOK_HOST"]   # https://your-app.up.railway.app
PORT: int         = int(os.environ.get("PORT", 8080))

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

GEMINI_KEYS: list[str] = [
    k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()
]
GROQ_KEYS: list[str] = [
    k.strip() for k in os.environ.get("GROQ_API_KEYS", "").split(",") if k.strip()
]

HISTORY_LIMIT = 10   # Xotirada saqlanadigan xabarlar soni

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
#   "characters"    : { str(user_id): "system prompt matni" }
#   "group_ai"      : { str(chat_id): bool }
#   "biz_conns"     : { str(owner_id): { "conn_id": str, "owner_name": str } }
#   "biz_secretary" : { str(owner_id): "kotib ko'rsatmasi" }
_db.setdefault("characters",    {})
_db.setdefault("group_ai",      {})
_db.setdefault("biz_conns",     {})
_db.setdefault("biz_secretary", {})


# ── Oddiy xarakter ────────────────────────────────────────────────────────────
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
    """Eganing Business ulanishini saqlash."""
    _db["biz_conns"][str(owner_id)] = {
        "conn_id":    conn_id,
        "owner_name": owner_name,
    }
    _save_data(_db)

def remove_biz_conn(owner_id: int):
    """Business ulanishini o'chirish."""
    _db["biz_conns"].pop(str(owner_id), None)
    _save_data(_db)

def get_biz_conn(owner_id: int) -> Optional[dict]:
    """Eganing connection ma'lumotini qaytarish."""
    return _db["biz_conns"].get(str(owner_id))

def get_biz_conn_id_by_conn(conn_id: str) -> Optional[int]:
    """connection_id orqali owner_id topish."""
    for uid, info in _db["biz_conns"].items():
        if info.get("conn_id") == conn_id:
            return int(uid)
    return None


# ── Business kotib ko'rsatmasi ────────────────────────────────────────────────
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

def history_add(user_id: int, role: str, content: str):
    _histories[user_id].append({"role": role, "content": content})

def history_get(user_id: int) -> list[dict]:
    return list(_histories[user_id])

def history_clear(user_id: int):
    _histories[user_id].clear()


# ─────────────────────────────────────────────────────────────────────────────
# System Prompt builder
# ─────────────────────────────────────────────────────────────────────────────
_NO_MARKDOWN = (
    "QATTIQ QOIDA: Javobingda hech qachon quyidagi belgilarni ishlatma: "
    "*, **, #, ##, ###, ~, ~~, `, ```, _, __, @, &, $, ^, |, >, >>. "
    "Markdown formatlash YO'Q. Faqat oddiy matn. "
    "Zarur bo'lganda emoji ishlat, ortiqcha emas.\n"
    "Javoblar aniq, tushunarli va qulay o'qilishi kerak.\n\n"
)

def build_system_prompt(user_id: int) -> str:
    """Oddiy shaxsiy chat uchun system prompt."""
    custom = get_character(user_id)
    suffix = f"Foydalanuvchi ko'rsatmasi:\n{custom}" if custom \
             else "Sen foydali, do'stona va aqlli AI yordamchisan."
    return _NO_MARKDOWN + suffix


def build_secretary_prompt(owner_id: int, owner_name: str) -> str:
    """
    Business kotib uchun system prompt.
    Ega o'z ko'rsatmasini bergan bo'lsa — undan foydalanamiz.
    Berilmagan bo'lsa — standart professional kotib rejimi.
    """
    custom = get_biz_secretary(owner_id)

    base = (
        _NO_MARKDOWN
        + f"Sen '{owner_name}' nomidan javob beradigan aqlli AI kotibsan.\n"
        + "Senga '{owner_name}'ning mijozlari, do'stlari va hamkorlari yozmoqda.\n"
        + "Xabarlarni tahlil qil va eganing nomidan mos, professional javob yoz.\n"
        + "Javoblar qisqa, aniq va odamiy bo'lsin.\n\n"
    )

    if custom:
        return base + f"Eganing shaxsiy ko'rsatmasi:\n{custom}"
    return base + (
        "Standart kotib rejimi: xushmuomala, professional, qisqa javob ber. "
        "Uchrashuv, savol yoki so'rovlarni muloyimlik bilan qabul qil."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Response cleaner
# ─────────────────────────────────────────────────────────────────────────────
_RE_CODE      = re.compile(r"```[\s\S]*?```|`[^`]+`")
_RE_BOLD      = re.compile(r"\*\*(.+?)\*\*")
_RE_ITALIC    = re.compile(r"\*(.+?)\*")
_RE_STRIKE    = re.compile(r"~~(.+?)~~")
_RE_UNDER2    = re.compile(r"__(.+?)__")
_RE_UNDER     = re.compile(r"_(.+?)_")
_RE_HEADING   = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_LEFTOVER  = re.compile(r"[*#~`^|>]")
_RE_NEWLINES  = re.compile(r"\n{3,}")
_RE_SPACES    = re.compile(r"^ +", re.MULTILINE)

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
# API Key Rotator
# ─────────────────────────────────────────────────────────────────────────────
class APIKeyRotator:
    def __init__(self, keys: list[str], provider: str):
        self.provider = provider
        self.keys     = list(keys)
        self._cycle   = cycle(self.keys) if self.keys else iter([])
        self._failed: set[str] = set()

    def next_key(self) -> Optional[str]:
        for _ in range(max(len(self.keys), 1)):
            key = next(self._cycle, None)
            if key and key not in self._failed:
                return key
        return None

    def mark_failed(self, key: str):
        logger.warning(f"[{self.provider}] ...{key[-4:]} ishlamadi, o'tkazildi.")
        self._failed.add(key)

    def reset(self):
        self._failed.clear()
        self._cycle = cycle(self.keys) if self.keys else iter([])
        logger.info(f"[{self.provider}] Kalitlar yangilandi.")

    @property
    def has_keys(self) -> bool:
        return bool(self.keys)


gemini_rot = APIKeyRotator(GEMINI_KEYS, "Gemini")
groq_rot   = APIKeyRotator(GROQ_KEYS,   "Groq")


# ─────────────────────────────────────────────────────────────────────────────
# AI Providers  (Gemini free + Groq free — kuchli xato boshqaruvi)
# ─────────────────────────────────────────────────────────────────────────────

# Free tier model nomlari
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
GROQ_MODEL   = os.environ.get("GROQ_MODEL",   "llama3-8b-8192")


def _gemini_contents(hist: list[dict], user_text: str) -> list[dict]:
    """Suhbat tarixini Gemini formatiga o'tkazish."""
    contents = []
    for h in hist:
        role = "user" if h["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    return contents


async def ask_gemini(system: str, hist: list[dict], user_text: str) -> Optional[str]:
    """
    Gemini free API. Barcha xatolar log qilinadi.
    Qaytaradi: javob matni yoki None.
    """
    if not gemini_rot.has_keys:
        return None

    contents = _gemini_contents(hist, user_text)

    for attempt in range(len(gemini_rot.keys)):
        key = gemini_rot.next_key()
        if not key:
            logger.error("Gemini: barcha kalitlar muvaffaqiyatsiz.")
            break

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={key}"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": contents,
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

                    # Tarmoq/server xatolar
                    if resp.status == 429:
                        logger.warning(f"Gemini [{attempt+1}] ...{key[-4:]}: rate limit (429)")
                        gemini_rot.mark_failed(key)
                        continue
                    if resp.status in (400, 401, 403):
                        logger.error(
                            f"Gemini [{attempt+1}] ...{key[-4:]}: "
                            f"HTTP {resp.status} → {raw[:300]}"
                        )
                        gemini_rot.mark_failed(key)
                        continue
                    if resp.status != 200:
                        logger.error(
                            f"Gemini [{attempt+1}] ...{key[-4:]}: "
                            f"HTTP {resp.status} → {raw[:300]}"
                        )
                        continue

                    # JSON parse
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.error(f"Gemini: JSON parse xato → {raw[:300]}")
                        continue

                    # Xavfsizlik filtri yoki bo'sh candidates
                    block = data.get("promptFeedback", {}).get("blockReason")
                    if block:
                        logger.warning(f"Gemini: so'rov bloklandi → {block}")
                        return None

                    candidates = data.get("candidates", [])
                    if not candidates:
                        logger.error(f"Gemini: candidates bo'sh → {data}")
                        continue

                    # Finish reason tekshiruvi
                    finish = candidates[0].get("finishReason", "STOP")
                    if finish not in ("STOP", "MAX_TOKENS", "1", 1):
                        logger.warning(f"Gemini: finishReason={finish}")
                        # Ba'zan matn bo'lishi mumkin, davom etamiz

                    # Matn olish
                    try:
                        text = (
                            candidates[0]["content"]["parts"][0]["text"]
                        )
                        if text and text.strip():
                            logger.info(f"Gemini: javob olindi ({len(text)} belgi)")
                            return text
                        logger.warning(f"Gemini: bo'sh matn qaytdi")
                    except (KeyError, IndexError) as e:
                        logger.error(f"Gemini: javob strukturasi noto'g'ri → {e} | {candidates[0]}")
                        continue

        except asyncio.TimeoutError:
            logger.error(f"Gemini [{attempt+1}] ...{key[-4:]}: timeout (40s)")
        except aiohttp.ClientError as e:
            logger.error(f"Gemini [{attempt+1}] ...{key[-4:]}: tarmoq xato → {e}")
        except Exception as e:
            logger.error(f"Gemini [{attempt+1}] ...{key[-4:]}: kutilmagan xato → {e}")
            gemini_rot.mark_failed(key)

    return None


async def ask_groq(system: str, hist: list[dict], user_text: str) -> Optional[str]:
    """
    Groq free API. Barcha xatolar log qilinadi.
    Qaytaradi: javob matni yoki None.
    """
    if not groq_rot.has_keys:
        return None

    messages = [{"role": "system", "content": system}]
    for h in hist:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})

    for attempt in range(len(groq_rot.keys)):
        key = groq_rot.next_key()
        if not key:
            logger.error("Groq: barcha kalitlar muvaffaqiyatsiz.")
            break

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model":       GROQ_MODEL,
            "messages":    messages,
            "max_tokens":  1024,
            "temperature": 0.7,
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
                        logger.warning(f"Groq [{attempt+1}] ...{key[-4:]}: rate limit (429)")
                        groq_rot.mark_failed(key)
                        continue
                    if resp.status in (400, 401, 403):
                        logger.error(
                            f"Groq [{attempt+1}] ...{key[-4:]}: "
                            f"HTTP {resp.status} → {raw[:300]}"
                        )
                        groq_rot.mark_failed(key)
                        continue
                    if resp.status != 200:
                        logger.error(
                            f"Groq [{attempt+1}] ...{key[-4:]}: "
                            f"HTTP {resp.status} → {raw[:300]}"
                        )
                        continue

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.error(f"Groq: JSON parse xato → {raw[:300]}")
                        continue

                    choices = data.get("choices", [])
                    if not choices:
                        logger.error(f"Groq: choices bo'sh → {data}")
                        continue

                    text = choices[0].get("message", {}).get("content", "")
                    if text and text.strip():
                        logger.info(f"Groq: javob olindi ({len(text)} belgi)")
                        return text
                    logger.warning(f"Groq: bo'sh matn qaytdi")

        except asyncio.TimeoutError:
            logger.error(f"Groq [{attempt+1}] ...{key[-4:]}: timeout (40s)")
        except aiohttp.ClientError as e:
            logger.error(f"Groq [{attempt+1}] ...{key[-4:]}: tarmoq xato → {e}")
        except Exception as e:
            logger.error(f"Groq [{attempt+1}] ...{key[-4:]}: kutilmagan xato → {e}")
            groq_rot.mark_failed(key)

    return None


async def _call_ai(system: str, hist: list[dict], user_text: str) -> str:
    """
    Gemini → Groq fallback.
    Ikkisi ham ishlamasa — aniq xato xabari qaytaradi.
    """
    # 1. Gemini
    if gemini_rot.has_keys:
        answer = await ask_gemini(system, hist, user_text)
        if answer:
            return clean_response(answer)
        logger.warning("Gemini ishlamadi, Groq ga o'tilmoqda...")

    # 2. Groq
    if groq_rot.has_keys:
        answer = await ask_groq(system, hist, user_text)
        if answer:
            return clean_response(answer)
        logger.warning("Groq ham ishlamadi.")

    # 3. Ikkisi ham ishlamadi
    if not gemini_rot.has_keys and not groq_rot.has_keys:
        return (
            "AI sozlanmagan.\n"
            "GEMINI_API_KEYS yoki GROQ_API_KEYS ni Railway Variables ga qo'shing."
        )
    return (
        "AI vaqtincha ishlamayapti.\n"
        "Bir ozdan keyin urinib ko'ring yoki /reset buyrug'ini yuboring."
    )


async def ask_ai(user_id: int, user_text: str) -> str:
    """Oddiy chat uchun AI — tariх saqlanadi."""
    history_add(user_id, "user", user_text)
    system = build_system_prompt(user_id)
    hist   = history_get(user_id)[:-1]   # oxirgi user xabarsiz (allaqo'shildi)
    answer = await _call_ai(system, hist, user_text)
    history_add(user_id, "assistant", answer)
    return answer


async def ask_ai_secretary(owner_id: int, owner_name: str,
                           sender_id: int, user_text: str) -> str:
    """
    Business kotib uchun AI.
    Tariх sender_id bo'yicha saqlanadi (har mijoz alohida).
    """
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
    r"\b(реклама|reklama|sotiladi|продаётся|купить|kup|прода[её]м|"
    r"заработок|daromad|пишите|yozing|подписывайтесь|obuna|"
    r"акция|chegirma|скидка|бесплатно|bepul|free\s+money|discount|promo)\b",
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
        f"Xabaringiz o'chirildi. Sabab: {reason} taqiqlangan.\n"
        f"Guruhda reklama va havolalar joylash mumkin emas.",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(30)
    try:
        await sent.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Mening xarakterim")],
        [
            KeyboardButton(text="Xarakterimni ko'rish"),
            KeyboardButton(text="Xarakterimni o'chirish"),
        ],
        [KeyboardButton(text="Suhbatni tozalash")],
        [KeyboardButton(text="Kotib sozlamalari")],
    ],
    resize_keyboard=True,
)

BIZ_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Kotib ko'rsatmasini o'rnatish")],
        [
            KeyboardButton(text="Kotib ko'rsatmasini ko'rish"),
            KeyboardButton(text="Kotib ko'rsatmasini o'chirish"),
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


# ─────────────────────────────────────────────────────────────────────────────
# Router & Handlers
# ─────────────────────────────────────────────────────────────────────────────
router = Router()


# ══════════════════════════════════════════════════════════════════════════════
# BUSINESS MODE HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

@router.business_connection()
async def on_business_connection(event: BusinessConnection, bot: Bot):
    """
    Kimdir botni Business kotib sifatida qo'shganda yoki o'chirganda chaqiriladi.
    Telegram Settings → Business → Chatbots → Bot qo'shish.
    """
    owner    = event.user
    owner_id = owner.id
    owner_name = owner.full_name or owner.first_name or "Ega"

    if event.is_enabled:
        # Yangi ulanish — ma'lumotlarni saqlaymiz
        save_biz_conn(owner_id, event.id, owner_name)
        logger.info(f"Business ulanish: {owner_name} (id={owner_id}), conn={event.id}")

        try:
            await bot.send_message(
                chat_id=owner_id,
                text=(
                    f"Salom, {owner_name}!\n\n"
                    "Men endi sizning Business kotibbingizman.\n"
                    "Sizga yozgan har bir odamga men avtomatik AI javob beraman.\n\n"
                    "Sozlash uchun quyidagilarni bajaring:\n"
                    "  1. 'Kotib sozlamalari' tugmasini bosing\n"
                    "  2. Kotib ko'rsatmasini yozing — AI xuddi siz kabi gaplashadi\n\n"
                    "Masalan: 'Mening nomimdan rasmiy va qisqa javob ber' yoki\n"
                    "'Dostona, hazil bilan, o'zbek tilida gaplash'\n\n"
                    "Ko'rsatma berilmasa — standart professional kotib rejimi ishlaydi."
                ),
                reply_markup=MAIN_KB,
            )
        except Exception as e:
            logger.warning(f"Business welcome xabar yuborilmadi: {e}")

    else:
        # Ulanish o'chirildi
        remove_biz_conn(owner_id)
        logger.info(f"Business uzilish: {owner_name} (id={owner_id})")

        try:
            await bot.send_message(
                chat_id=owner_id,
                text=(
                    "Business kotib rejimi o'chirildi.\n"
                    "Endi xabarlaringizga avtomatik javob berilmaydi.\n"
                    "Qayta yoqish uchun Telegram → Sozlamalar → Business → Chatbotlar."
                ),
                reply_markup=MAIN_KB,
            )
        except Exception as e:
            logger.warning(f"Business uzilish xabari yuborilmadi: {e}")


@router.message(F.business_connection_id.is_not(None), F.text)
async def on_business_message(msg: Message, bot: Bot):
    """
    Business kotibi orqali kelgan xabar.
    msg.business_connection_id — qaysi eganing ulanishi ekanini bildiradi.
    msg.from_user  — xabar yozgan mijoz/odam.
    msg.chat       — eganing shu odam bilan chat kanali.

    Bot ushbu chatga ega nomidan avtomatik javob yozadi.
    """
    conn_id = msg.business_connection_id
    if not conn_id or not msg.from_user or not msg.text:
        return

    # Ega kimligini aniqlaymiz
    owner_id = get_biz_conn_id_by_conn(conn_id)
    if owner_id is None:
        logger.warning(f"Business conn '{conn_id}' uchun ega topilmadi.")
        return

    owner_info = get_biz_conn(owner_id)
    owner_name = owner_info.get("owner_name", "Ega") if owner_info else "Ega"
    sender_id  = msg.from_user.id

    # Eganing o'zi yozayotgan bo'lsa — javob bermaymiz
    if sender_id == owner_id:
        return

    logger.info(
        f"Business xabar: {msg.from_user.full_name} → {owner_name} | "
        f"'{msg.text[:50]}'"
    )

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
            business_connection_id=conn_id,   # <-- Bu shart! Business orqali yuborish
        )
        logger.info(f"Business javob yuborildi → {msg.from_user.full_name}")
    except Exception as e:
        logger.error(f"Business javob yuborishda xato: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ODDIY BUYRUQLAR
# ══════════════════════════════════════════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    name = (msg.from_user.first_name or "Do'stim") if msg.from_user else "Do'stim"
    conn = get_biz_conn(msg.from_user.id) if msg.from_user else None
    biz_status = "Ulangan (faol)" if conn else "Ulanmagan"

    await msg.answer(
        f"Salom, {name}! Men aqlli AI yordamchiman.\n\n"
        "Imkoniyatlar:\n"
        "  Shaxsiy chatda AI bilan suhbat\n"
        "  Business kotib rejimi (auto-javob)\n"
        "  Xarakter sozlamasi — AI siz kabi gaplashadi\n"
        "  Guruhlarda spam moderatsiyasi\n\n"
        f"Business kotib: {biz_status}\n"
        "(Yoqish: Telegram → Sozlamalar → Business → Chatbotlar)\n\n"
        "Savol yozing yoki quyidagi tugmalarni bosing.",
        reply_markup=MAIN_KB,
    )


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "Buyruqlar:\n\n"
        "/start     — Boshlash\n"
        "/help      — Yordam\n"
        "/test      — API kalitlarini tekshirish\n"
        "/reset     — API kalitlarini yangilash\n"
        "/clear     — Suhbat tarixini tozalash\n"
        "/groupai   — Guruhda AI rejimi (admin)\n"
        "/bizstatus — Business kotib holati\n\n"
        "Tugmalar:\n"
        "  'Mening xarakterim'    — AI uslubini o'rgatish\n"
        "  'Kotib sozlamalari'    — Business kotib ko'rsatmasi\n"
        "  'Suhbatni tozalash'    — Xotira tozalanadi\n\n"
        "Business kotib:\n"
        "  Telegram → Sozlamalar → Business → Chatbotlar → Bot qo'shing\n"
        "  Shundan so'ng sizga yozganlar AI javob oladi.",
    )


@router.message(Command("reset"))
async def cmd_reset(msg: Message):
    gemini_rot.reset()
    groq_rot.reset()
    await msg.answer("API kalitlari yangilandi.")


@router.message(Command("clear"))
async def cmd_clear(msg: Message):
    if msg.from_user:
        history_clear(msg.from_user.id)
    await msg.answer("Suhbat tarixi tozalandi. Yangi suhbat boshlaylik!")


@router.message(Command("test"))
async def cmd_test(msg: Message):
    """
    API kalitlarini bevosita tekshirish.
    Railway loglarida ham batafsil ma'lumot ko'rinadi.
    """
    lines = ["API kalitlari tekshirilmoqda...\n"]
    status_msg = await msg.answer(lines[0])

    # ── Gemini ────────────────────────────────────────────────────────
    if not gemini_rot.has_keys:
        lines.append("Gemini: GEMINI_API_KEYS bo'sh — kalit qo'shilmagan")
    else:
        lines.append(f"Gemini: {len(gemini_rot.keys)} ta kalit — sinovda...")
        await status_msg.edit_text("\n".join(lines))
        gemini_rot.reset()   # failed flaglarni tozalaymiz

        test_ans = await ask_gemini(
            "Qisqa javob ber. Maxsus belgi ishlatma.",
            [],
            "Salom. Faqat 'Ishlayapman' deb yoz.",
        )
        if test_ans:
            lines[-1] = f"Gemini: ishlaydi (model: {GEMINI_MODEL})"
        else:
            lines[-1] = (
                f"Gemini: ISHLAMADI (model: {GEMINI_MODEL})\n"
                "  → Railway Logs da 'Gemini' qatorlarini tekshiring\n"
                "  → Kalit to'g'riligini aistudio.google.com da tekshiring"
            )

    # ── Groq ──────────────────────────────────────────────────────────
    if not groq_rot.has_keys:
        lines.append("Groq: GROQ_API_KEYS bo'sh — kalit qo'shilmagan")
    else:
        lines.append(f"\nGroq: {len(groq_rot.keys)} ta kalit — sinovda...")
        await status_msg.edit_text("\n".join(lines))
        groq_rot.reset()

        test_ans = await ask_groq(
            "Qisqa javob ber. Maxsus belgi ishlatma.",
            [],
            "Salom. Faqat 'Ishlayapman' deb yoz.",
        )
        if test_ans:
            lines[-1] = f"Groq: ishlaydi (model: {GROQ_MODEL})"
        else:
            lines[-1] = (
                f"Groq: ISHLAMADI (model: {GROQ_MODEL})\n"
                "  → Railway Logs da 'Groq' qatorlarini tekshiring\n"
                "  → Kalit to'g'riligini console.groq.com da tekshiring"
            )

    # ── Yakuniy ───────────────────────────────────────────────────────
    lines.append(
        "\nKalit formati: key1,key2,key3 (bo'sh joysiz)\n"
        "Model sozlash: GEMINI_MODEL, GROQ_MODEL variable lari"
    )
    await status_msg.edit_text("\n".join(lines))


@router.message(Command("bizstatus"))
async def cmd_bizstatus(msg: Message):
    if not msg.from_user:
        return
    conn = get_biz_conn(msg.from_user.id)
    if conn:
        sec = get_biz_secretary(msg.from_user.id)
        sec_text = f"Ko'rsatma: {sec[:80]}..." if len(sec) > 80 else (sec or "Standart (berilmagan)")
        await msg.answer(
            "Business kotib: Faol\n\n"
            f"Kotib ko'rsatmasi: {sec_text}\n\n"
            "O'zgartirish: 'Kotib sozlamalari' tugmasi."
        )
    else:
        await msg.answer(
            "Business kotib: Ulanmagan\n\n"
            "Ulanish: Telegram → Sozlamalar → Business → Chatbotlar → ushbu botni qo'shing."
        )


@router.message(
    Command("groupai"),
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def cmd_groupai(msg: Message, bot: Bot):
    try:
        m = await msg.chat.get_member(msg.from_user.id)
        if m.status not in ("administrator", "creator"):
            await msg.reply("Bu buyruq faqat adminlar uchun.")
            return
    except Exception:
        return
    state = toggle_group_ai(msg.chat.id)
    holat = "yoqildi" if state else "o'chirildi"
    izoh  = "Barcha xabarlarga AI javob beradi." if state \
            else "Faqat @mention yoki reply orqali javob beradi."
    await msg.reply(f"Guruh AI rejimi {holat}.\n{izoh}")


# ── Tugma: Mening xarakterim ─────────────────────────────────────────────────
@router.message(F.text == "Mening xarakterim")
async def btn_set_character(msg: Message, state: FSMContext):
    await state.set_state(CharForm.waiting)
    current = get_character(msg.from_user.id) if msg.from_user else ""
    hint = f"\n\nHozirgi xarakter:\n{current}" if current else ""
    await msg.answer(
        "AI qanday uslubda javob bersin? Namunalar:\n\n"
        "  'Mening nomimdan javob ber, juda jiddiy va rasmiy tonda'\n"
        "  'Dostona, hazillashib va ko'cha jargonlarida gaplash'\n"
        "  'Har doim qisqa va aniq, ortiqcha gap yozma'\n"
        "  'Ingliz tilida, professional ohangda'\n"
        "  'Sen 25 yoshli Jasursan, futbol ixlosmandisan'\n\n"
        "Ko'rsatmangizni yuboring:" + hint,
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(CharForm.waiting, F.text)
async def receive_character(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if len(text) < 5:
        await msg.answer("Juda qisqa. Batafsil yozing (kamida 5 belgi).")
        return
    if msg.from_user:
        set_character(msg.from_user.id, text)
    await state.clear()
    await msg.answer(
        f"Xarakter saqlandi!\n\n{text}\n\nEndi AI shu ko'rsatma bilan javob beradi.",
        reply_markup=MAIN_KB,
    )


@router.message(F.text == "Xarakterimni ko'rish")
async def btn_view_character(msg: Message):
    char = get_character(msg.from_user.id) if msg.from_user else ""
    if char:
        await msg.answer(f"Joriy xarakter:\n\n{char}")
    else:
        await msg.answer("Xarakter sozlanmagan. Standart AI rejimi ishlayapti.")


@router.message(F.text == "Xarakterimni o'chirish")
async def btn_delete_character(msg: Message):
    if msg.from_user and get_character(msg.from_user.id):
        del_character(msg.from_user.id)
        await msg.answer("Xarakter o'chirildi. Standart AI rejimiga qaytildi.")
    else:
        await msg.answer("Xarakter sozlanmagan edi.")


@router.message(F.text == "Suhbatni tozalash")
async def btn_clear_history(msg: Message):
    if msg.from_user:
        history_clear(msg.from_user.id)
    await msg.answer("Suhbat tarixi tozalandi. Yangi suhbat boshlaylik!")


# ── Tugma: Kotib sozlamalari menyusi ─────────────────────────────────────────
@router.message(F.text == "Kotib sozlamalari")
async def btn_biz_menu(msg: Message):
    conn = get_biz_conn(msg.from_user.id) if msg.from_user else None
    status = "Faol" if conn else "Ulanmagan"
    await msg.answer(
        f"Business kotib sozlamalari\n\n"
        f"Holat: {status}\n\n"
        "Kotib ko'rsatmasi — AI mijozlarga sizning nomingizdan javob beradi.\n"
        "Ko'rsatma berilmasa, standart professional kotib ishlaydi.",
        reply_markup=BIZ_KB,
    )


@router.message(F.text == "Kotib ko'rsatmasini o'rnatish")
async def btn_set_secretary(msg: Message, state: FSMContext):
    await state.set_state(SecretaryForm.waiting)
    current = get_biz_secretary(msg.from_user.id) if msg.from_user else ""
    hint = f"\n\nHozirgi ko'rsatma:\n{current}" if current else ""
    await msg.answer(
        "Kotibingiz qanday ishlashini yozing. Namunalar:\n\n"
        "  'Mening nomimdan rasmiy, qisqa javob ber. Men tadbirkorman.'\n"
        "  'Dostona va quvnoq ohangda javob ber, men blogersman'\n"
        "  'Har doim so'rovlarni qabul qil va uchrashuvni tayinla'\n"
        "  'Ingliz tilida javob ber, professional konsultant sifatida'\n\n"
        "Ko'rsatmangizni yuboring:" + hint,
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(SecretaryForm.waiting, F.text)
async def receive_secretary(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if len(text) < 5:
        await msg.answer("Juda qisqa. Batafsil yozing (kamida 5 belgi).")
        return
    if msg.from_user:
        set_biz_secretary(msg.from_user.id, text)
    await state.clear()
    await msg.answer(
        f"Kotib ko'rsatmasi saqlandi!\n\n{text}\n\n"
        "Endi AI kotib ushbu ko'rsatma asosida sizning nomingizdan javob beradi.",
        reply_markup=BIZ_KB,
    )


@router.message(F.text == "Kotib ko'rsatmasini ko'rish")
async def btn_view_secretary(msg: Message):
    sec = get_biz_secretary(msg.from_user.id) if msg.from_user else ""
    if sec:
        await msg.answer(f"Joriy kotib ko'rsatmasi:\n\n{sec}")
    else:
        await msg.answer("Kotib ko'rsatmasi berilmagan.\nStandart professional kotib ishlaydi.")


@router.message(F.text == "Kotib ko'rsatmasini o'chirish")
async def btn_delete_secretary(msg: Message):
    if msg.from_user and get_biz_secretary(msg.from_user.id):
        del_biz_secretary(msg.from_user.id)
        await msg.answer("Kotib ko'rsatmasi o'chirildi. Standart rejimga qaytildi.")
    else:
        await msg.answer("Kotib ko'rsatmasi berilmagan edi.")


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
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False

    if not is_admin:
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
    group_ai_mode = is_group_ai_on(msg.chat.id)

    if mentioned or reply_to_me or group_ai_mode:
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
    # FSM aktiv bo'lsa bu handler ishlamaydi
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
        BotCommand(command="start",      description="Botni ishga tushirish"),
        BotCommand(command="help",       description="Yordam"),
        BotCommand(command="test",       description="API kalitlarini tekshirish"),
        BotCommand(command="clear",      description="Suhbat tarixini tozalash"),
        BotCommand(command="reset",      description="API kalitlarini yangilash"),
        BotCommand(command="bizstatus",  description="Business kotib holati"),
        BotCommand(command="groupai",    description="Guruhda AI rejimi (admin)"),
    ])
    logger.info(f"Webhook o'rnatildi: {WEBHOOK_URL}")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logger.info("Webhook o'chirildi.")


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
