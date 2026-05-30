import asyncio
import json
import logging
import os
import re
from collections import deque
from typing import Optional

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
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
    Update,
)

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("BusinessBot")

# ─── Env variables ───────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
WEBHOOK_HOST = os.environ["WEBHOOK_HOST"]
PORT         = int(os.environ.get("PORT", 8080))
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
GEMINI_MODEL    = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
GROQ_MODEL      = os.environ.get("GROQ_MODEL", "llama3-8b-8192")
ADMIN_ID        = 7595247253

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

DATA_FILE = "bot_data.json"

# ─── Bot data I/O ────────────────────────────────────────────────────────────
def load_data() -> dict:
    default = {
        "active_ai": "gemini",
        "gemini_key": GEMINI_API_KEY,
        "groq_key":   GROQ_API_KEY,
        "characters": {},
        "group_ai":   {},
        "biz_conns":  {},
        "biz_secretary": {},
        "user_count": 0,
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            default.update(saved)
        except Exception as e:
            logger.error("Ma'lumot fayli o'qilmadi: %s", e)
    return default


def save_data(data: dict) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Ma'lumot saqlashda xato: %s", e)


# ─── In-memory conversation histories ───────────────────────────────────────
# {str(user_id): deque([{"role":..,"content":..}, ...])}
histories: dict[str, deque] = {}

def get_history(uid: int) -> deque:
    key = str(uid)
    if key not in histories:
        histories[key] = deque(maxlen=10)
    return histories[key]

def clear_history(uid: int) -> None:
    histories[str(uid)] = deque(maxlen=10)

# ─── Text cleaner ────────────────────────────────────────────────────────────
CLEAN_RE = re.compile(r"[\*#~`_\^|>\\]+")

def clean_text(text: str) -> str:
    return CLEAN_RE.sub("", text).strip()

# ─── FSM States ──────────────────────────────────────────────────────────────
class AdminFSM(StatesGroup):
    waiting_gemini_key = State()
    waiting_groq_key   = State()

class CharacterFSM(StatesGroup):
    waiting_character  = State()

class SecretaryFSM(StatesGroup):
    waiting_secretary  = State()

# ─── Keyboards ───────────────────────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Mening xarakterim"), KeyboardButton(text="Xarakterimni ko'rish")],
        [KeyboardButton(text="Xarakterimni o'chirish"), KeyboardButton(text="Suhbatni tozalash")],
        [KeyboardButton(text="Kotib sozlamalari")],
    ],
    resize_keyboard=True,
)

BIZ_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Kotib ko'rsatmasini o'rnatish")],
        [KeyboardButton(text="Kotib ko'rsatmasini ko'rish"), KeyboardButton(text="Kotibni o'chirish")],
        [KeyboardButton(text="Orqaga")],
    ],
    resize_keyboard=True,
)


def admin_kb(active_ai: str) -> InlineKeyboardMarkup:
    gem_mark = "✅" if active_ai == "gemini" else "☑️"
    grq_mark  = "✅" if active_ai == "groq"   else "☑️"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{gem_mark} Gemini", callback_data="ai_gemini"),
            InlineKeyboardButton(text=f"{grq_mark} Groq",   callback_data="ai_groq"),
        ],
        [InlineKeyboardButton(text="🔑 Gemini kalitini yangilash", callback_data="update_gemini_key")],
        [InlineKeyboardButton(text="🔑 Groq kalitini yangilash",   callback_data="update_groq_key")],
        [InlineKeyboardButton(text="📊 Statistika",  callback_data="stats")],
        [InlineKeyboardButton(text="🧪 AI test",     callback_data="ai_test")],
        [InlineKeyboardButton(text="❌ Yopish",       callback_data="close_admin")],
    ])

# ─── AI Providers ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "Sen professional biznes yordamchisisisan. "
    "Javoblaringda hech qanday markdown belgilarini ishlatma: "
    "**, *, #, ~~, `, __, _, ^, |, > kabi belgilarni aslo yozma. "
    "Faqat oddiy matn yoz."
)


async def ask_gemini(
    session: aiohttp.ClientSession,
    api_key: str,
    history: list[dict],
    user_msg: str,
    system_extra: str = "",
) -> str:
    sys_text = SYSTEM_PROMPT
    if system_extra:
        sys_text = system_extra + "\n\n" + sys_text

    contents = []
    for h in history:
        role = "user" if h["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_msg}]})

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": sys_text}]},
        "contents": contents,
    }

    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 429:
                return "Limit tugadi, admin /admin dan boshqa AI ga o'tishi mumkin."
            if r.status != 200:
                text_err = await r.text()
                logger.error("Gemini xato %d: %s", r.status, text_err[:300])
                return f"Gemini xatosi ({r.status}). Admin /admin dan tekshirsin."
            data = await r.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            return clean_text(raw)
    except asyncio.TimeoutError:
        return "Gemini javob bermadi (timeout). Keyinroq urinib ko'ring."
    except Exception as e:
        logger.error("Gemini so'rovda xato: %s", e)
        return "Gemini bilan bog'lanishda xato yuz berdi."


async def ask_groq(
    session: aiohttp.ClientSession,
    api_key: str,
    history: list[dict],
    user_msg: str,
    system_extra: str = "",
) -> str:
    sys_text = SYSTEM_PROMPT
    if system_extra:
        sys_text = system_extra + "\n\n" + sys_text

    messages = [{"role": "system", "content": sys_text}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_msg})

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL, "messages": messages, "max_tokens": 1024}

    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 429:
                return "Limit tugadi, admin /admin dan boshqa AI ga o'tishi mumkin."
            if r.status != 200:
                text_err = await r.text()
                logger.error("Groq xato %d: %s", r.status, text_err[:300])
                return f"Groq xatosi ({r.status}). Admin /admin dan tekshirsin."
            data = await r.json()
            raw = data["choices"][0]["message"]["content"]
            return clean_text(raw)
    except asyncio.TimeoutError:
        return "Groq javob bermadi (timeout). Keyinroq urinib ko'ring."
    except Exception as e:
        logger.error("Groq so'rovda xato: %s", e)
        return "Groq bilan bog'lanishda xato yuz berdi."


async def ask_ai(
    session: aiohttp.ClientSession,
    bot_data: dict,
    uid: int,
    user_msg: str,
    system_extra: str = "",
) -> str:
    hist = list(get_history(uid))
    active = bot_data.get("active_ai", "gemini")

    if active == "gemini":
        key = bot_data.get("gemini_key") or GEMINI_API_KEY
        if not key:
            return "Gemini API kaliti yo'q. Admin /admin dan kalit kiriting."
        reply = await ask_gemini(session, key, hist, user_msg, system_extra)
    else:
        key = bot_data.get("groq_key") or GROQ_API_KEY
        if not key:
            return "Groq API kaliti yo'q. Admin /admin dan kalit kiriting."
        reply = await ask_groq(session, key, hist, user_msg, system_extra)

    # Save to history
    h = get_history(uid)
    h.append({"role": "user", "content": user_msg})
    h.append({"role": "assistant", "content": reply})
    return reply


# ─── Spam detection ──────────────────────────────────────────────────────────
SPAM_LINK_RE    = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)
SPAM_MENTION_RE = re.compile(r"@\w+")
ADS_WORDS       = ["реклама", "reklama", "promote", "адvert", "купить", "sotib", "арзон", "discount"]


def is_spam(text: str) -> bool:
    if SPAM_LINK_RE.search(text):
        return True
    mentions = SPAM_MENTION_RE.findall(text)
    if len(mentions) >= 5:
        return True
    low = text.lower()
    if any(w in low for w in ADS_WORDS):
        return True
    return False


# ─── Router & handlers ───────────────────────────────────────────────────────
router = Router()


# ── /start ───────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, bot_data: dict, session: aiohttp.ClientSession):
    uid = message.from_user.id
    uid_str = str(uid)

    if uid_str not in bot_data.get("_seen_users", {}):
        bot_data.setdefault("_seen_users", {})[uid_str] = 1
        bot_data["user_count"] = bot_data.get("user_count", 0) + 1
        save_data(bot_data)

    active = bot_data.get("active_ai", "gemini").upper()
    name = message.from_user.first_name or "Foydalanuvchi"
    await message.answer(
        f"Salom, {name}!\n\n"
        f"Men sizning shaxsiy AI yordamchingizman.\n"
        f"Hozir faol AI: {active}\n\n"
        f"Biror narsa so'rang yoki quyidagi tugmalardan foydalaning.",
        reply_markup=MAIN_KB,
    )


# ── /help ────────────────────────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Buyruqlar:\n"
        "/start — Botni ishga tushirish\n"
        "/help — Yordam\n"
        "/clear — Suhbat tarixini tozalash\n"
        "/bizstatus — Business ulanish holati\n"
        "/groupai — Guruhda AI rejimini o'zgartirish (guruh admini)\n"
        "/admin — Admin panel (faqat admin)\n\n"
        "Klaviatura tugmalaridan ham foydalanishingiz mumkin.",
        reply_markup=MAIN_KB,
    )


# ── /clear ───────────────────────────────────────────────────────────────────
@router.message(Command("clear"))
async def cmd_clear(message: Message):
    clear_history(message.from_user.id)
    await message.answer("Suhbat tarixi tozalandi.", reply_markup=MAIN_KB)


# ── /bizstatus ───────────────────────────────────────────────────────────────
@router.message(Command("bizstatus"))
async def cmd_bizstatus(message: Message, bot_data: dict):
    uid_str = str(message.from_user.id)
    conn = bot_data.get("biz_conns", {}).get(uid_str)
    if conn:
        conn_id  = conn.get("conn_id", "?")
        sec_text = bot_data.get("biz_secretary", {}).get(uid_str) or "O'rnatilmagan"
        await message.answer(
            f"Business ulanish: faol\n"
            f"Ulanish ID: {conn_id}\n"
            f"Kotib ko'rsatmasi: {sec_text}"
        )
    else:
        await message.answer("Sizda faol business ulanish yo'q.")


# ── /groupai ─────────────────────────────────────────────────────────────────
@router.message(Command("groupai"))
async def cmd_groupai(message: Message, bot_data: dict):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi.")
        return

    member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ("administrator", "creator"):
        await message.answer("Bu buyruq faqat guruh adminlari uchun.")
        return

    cid_str = str(message.chat.id)
    current = bot_data.get("group_ai", {}).get(cid_str, False)
    bot_data.setdefault("group_ai", {})[cid_str] = not current
    save_data(bot_data)

    status = "yoqildi (barcha xabarlarga javob)" if not current else "o'chirildi (faqat @mention/reply)"
    await message.answer(f"Guruh AI rejimi: {status}")


# ── /admin ───────────────────────────────────────────────────────────────────
@router.message(Command("admin"))
async def cmd_admin(message: Message, bot_data: dict):
    if message.from_user.id != ADMIN_ID:
        return
    active = bot_data.get("active_ai", "gemini")
    await message.answer("Admin panel:", reply_markup=admin_kb(active))


# ─── Admin callbacks ──────────────────────────────────────────────────────────
@router.callback_query(F.data.in_({"ai_gemini", "ai_groq", "update_gemini_key",
                                    "update_groq_key", "stats", "ai_test", "close_admin"}))
async def admin_callback(call: CallbackQuery, state: FSMContext, bot_data: dict, session: aiohttp.ClientSession):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo'q!", show_alert=True)
        return

    data_key = call.data

    if data_key in ("ai_gemini", "ai_groq"):
        new_ai = "gemini" if data_key == "ai_gemini" else "groq"
        bot_data["active_ai"] = new_ai
        save_data(bot_data)
        await call.message.edit_reply_markup(reply_markup=admin_kb(new_ai))
        await call.answer(f"Faol AI: {new_ai.upper()}")

    elif data_key == "update_gemini_key":
        await call.message.delete()
        await call.message.answer("Yangi Gemini API kalitini yuboring:")
        await state.set_state(AdminFSM.waiting_gemini_key)
        await call.answer()

    elif data_key == "update_groq_key":
        await call.message.delete()
        await call.message.answer("Yangi Groq API kalitini yuboring:")
        await state.set_state(AdminFSM.waiting_groq_key)
        await call.answer()

    elif data_key == "stats":
        user_count = bot_data.get("user_count", 0)
        biz_count  = len(bot_data.get("biz_conns", {}))
        await call.answer(
            f"Foydalanuvchilar: {user_count}\nBusiness ulanishlar: {biz_count}",
            show_alert=True,
        )

    elif data_key == "ai_test":
        await call.answer("Test so'rovi yuborilmoqda...")
        reply = await ask_ai(session, bot_data, ADMIN_ID, "Salom! Qisqacha o'zingni tanishtir.")
        await call.message.answer(f"AI test javobi:\n\n{reply}")

    elif data_key == "close_admin":
        await call.message.delete()
        await call.answer()


@router.message(AdminFSM.waiting_gemini_key)
async def fsm_gemini_key(message: Message, state: FSMContext, bot_data: dict):
    if message.from_user.id != ADMIN_ID:
        return
    new_key = message.text.strip()
    bot_data["gemini_key"] = new_key
    save_data(bot_data)
    await message.delete()
    await message.answer("Gemini kaliti yangilandi va xabar o'chirildi.")
    await state.clear()


@router.message(AdminFSM.waiting_groq_key)
async def fsm_groq_key(message: Message, state: FSMContext, bot_data: dict):
    if message.from_user.id != ADMIN_ID:
        return
    new_key = message.text.strip()
    bot_data["groq_key"] = new_key
    save_data(bot_data)
    await message.delete()
    await message.answer("Groq kaliti yangilandi va xabar o'chirildi.")
    await state.clear()


# ─── Character FSM ────────────────────────────────────────────────────────────
@router.message(F.text == "Mening xarakterim")
async def char_set_start(message: Message, state: FSMContext):
    await message.answer(
        "Botning xarakterini kiriting (masalan: 'Sen hazilkash do'stsan'):",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(CharacterFSM.waiting_character)


@router.message(CharacterFSM.waiting_character)
async def char_set_save(message: Message, state: FSMContext, bot_data: dict):
    uid_str = str(message.from_user.id)
    char = message.text.strip()
    bot_data.setdefault("characters", {})[uid_str] = char
    save_data(bot_data)
    await message.answer("Xarakter saqlandi!", reply_markup=MAIN_KB)
    await state.clear()


@router.message(F.text == "Xarakterimni ko'rish")
async def char_view(message: Message, bot_data: dict):
    uid_str = str(message.from_user.id)
    char = bot_data.get("characters", {}).get(uid_str)
    if char:
        await message.answer(f"Sizning xarakteringiz:\n\n{char}")
    else:
        await message.answer("Xarakter o'rnatilmagan.")


@router.message(F.text == "Xarakterimni o'chirish")
async def char_delete(message: Message, bot_data: dict):
    uid_str = str(message.from_user.id)
    bot_data.setdefault("characters", {}).pop(uid_str, None)
    save_data(bot_data)
    await message.answer("Xarakter o'chirildi.")


# ─── Clear history button ────────────────────────────────────────────────────
@router.message(F.text == "Suhbatni tozalash")
async def clear_history_btn(message: Message):
    clear_history(message.from_user.id)
    await message.answer("Suhbat tarixi tozalandi.")


# ─── Secretary FSM ────────────────────────────────────────────────────────────
@router.message(F.text == "Kotib sozlamalari")
async def biz_menu(message: Message):
    await message.answer("Kotib sozlamalari:", reply_markup=BIZ_KB)


@router.message(F.text == "Orqaga")
async def biz_back(message: Message):
    await message.answer("Asosiy menyu:", reply_markup=MAIN_KB)


@router.message(F.text == "Kotib ko'rsatmasini o'rnatish")
async def secretary_set_start(message: Message, state: FSMContext):
    await message.answer(
        "Kotib uchun ko'rsatma kiriting\n"
        "(masalan: 'Mijozlarga xushmuomala bo'l, narxlarni aytma'):",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(SecretaryFSM.waiting_secretary)


@router.message(SecretaryFSM.waiting_secretary)
async def secretary_set_save(message: Message, state: FSMContext, bot_data: dict):
    uid_str = str(message.from_user.id)
    sec = message.text.strip()
    bot_data.setdefault("biz_secretary", {})[uid_str] = sec
    save_data(bot_data)
    await message.answer("Kotib ko'rsatmasi saqlandi!", reply_markup=BIZ_KB)
    await state.clear()


@router.message(F.text == "Kotib ko'rsatmasini ko'rish")
async def secretary_view(message: Message, bot_data: dict):
    uid_str = str(message.from_user.id)
    sec = bot_data.get("biz_secretary", {}).get(uid_str)
    if sec:
        await message.answer(f"Kotib ko'rsatmasi:\n\n{sec}")
    else:
        await message.answer("Kotib ko'rsatmasi o'rnatilmagan.")


@router.message(F.text == "Kotibni o'chirish")
async def secretary_delete(message: Message, bot_data: dict):
    uid_str = str(message.from_user.id)
    bot_data.setdefault("biz_secretary", {}).pop(uid_str, None)
    save_data(bot_data)
    await message.answer("Kotib o'chirildi.", reply_markup=BIZ_KB)


# ─── Business connection handler ─────────────────────────────────────────────
@router.business_connection()
async def on_business_connection(event: BusinessConnection, bot_data: dict):
    uid_str = str(event.user.id)
    owner_name = event.user.full_name or event.user.first_name or "Egasi"

    if event.is_enabled:
        bot_data.setdefault("biz_conns", {})[uid_str] = {
            "conn_id": event.id,
            "owner_name": owner_name,
        }
        save_data(bot_data)
        logger.info("Business ulanish qo'shildi: %s (%s)", uid_str, event.id)
        try:
            await event.bot.send_message(
                chat_id=event.user_chat_id,
                text=(
                    f"Business kotib ulanishi faollashtirildi!\n"
                    f"Ulanish ID: {event.id}\n"
                    f"Endi mijozlaringizga avtomatik javob beriladi."
                ),
            )
        except Exception as e:
            logger.error("Business welcome xabar yuborishda xato: %s", e)
    else:
        bot_data.setdefault("biz_conns", {}).pop(uid_str, None)
        save_data(bot_data)
        logger.info("Business ulanish uzildi: %s", uid_str)
        try:
            await event.bot.send_message(
                chat_id=event.user_chat_id,
                text="Business kotib ulanishi o'chirildi.",
            )
        except Exception as e:
            logger.error("Business disconnect xabar yuborishda xato: %s", e)


# ─── Business message handler ────────────────────────────────────────────────
@router.message(F.business_connection_id.is_not(None))
async def on_business_message(message: Message, bot_data: dict, session: aiohttp.ClientSession):
    conn_id = message.business_connection_id

    # Find owner by conn_id
    owner_id_str: Optional[str] = None
    for oid, info in bot_data.get("biz_conns", {}).items():
        if info.get("conn_id") == conn_id:
            owner_id_str = oid
            break

    if owner_id_str is None:
        logger.warning("Business conn_id %s uchun egasi topilmadi", conn_id)
        return

    # If the owner is writing — skip
    if str(message.from_user.id) == owner_id_str:
        return

    sec_prompt = bot_data.get("biz_secretary", {}).get(owner_id_str, "")
    user_text  = message.text or message.caption or ""
    if not user_text:
        return

    visitor_id = message.from_user.id
    reply = await ask_ai(session, bot_data, visitor_id, user_text, system_extra=sec_prompt)

    try:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=reply,
            business_connection_id=conn_id,
        )
    except Exception as e:
        logger.error("Business reply yuborishda xato: %s", e)


# ─── Group message handler ────────────────────────────────────────────────────
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_message(message: Message, bot_data: dict, session: aiohttp.ClientSession):
    cid_str = str(message.chat.id)
    text = message.text or message.caption or ""

    # Spam check (exempt admins)
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False

    if not is_admin and text and is_spam(text):
        try:
            await message.delete()
            warn = await message.answer(
                f"@{message.from_user.username or message.from_user.first_name} "
                "spam/reklama xabar yubordi va o'chirildi."
            )
            await asyncio.sleep(30)
            await warn.delete()
        except Exception as e:
            logger.error("Spam handling xato: %s", e)
        return

    group_ai_on = bot_data.get("group_ai", {}).get(cid_str, False)
    bot_info = await message.bot.get_me()
    bot_username = bot_info.username or ""

    mentioned = (
        f"@{bot_username}" in (text or "")
        or (message.reply_to_message and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == message.bot.id)
    )

    if not group_ai_on and not mentioned:
        return
    if not text:
        return

    reply = await ask_ai(session, bot_data, message.from_user.id, text)
    try:
        await message.reply(reply)
    except Exception as e:
        logger.error("Guruh javob yuborishda xato: %s", e)


# ─── Private message handler (AI chat) ───────────────────────────────────────
@router.message(F.chat.type == ChatType.PRIVATE)
async def private_message(message: Message, bot_data: dict, session: aiohttp.ClientSession):
    text = message.text or message.caption or ""
    if not text:
        return

    uid = message.from_user.id
    uid_str = str(uid)
    char = bot_data.get("characters", {}).get(uid_str, "")

    await message.bot.send_chat_action(message.chat.id, "typing")
    reply = await ask_ai(session, bot_data, uid, text, system_extra=char)
    await message.answer(reply)


# ─── Middleware to inject bot_data & session ──────────────────────────────────
from aiogram import BaseMiddleware
from typing import Callable, Any


class DataMiddleware(BaseMiddleware):
    def __init__(self, bot_data: dict, session: aiohttp.ClientSession):
        self.bot_data = bot_data
        self.session  = session

    async def __call__(self, handler: Callable, event: Any, data: dict) -> Any:
        data["bot_data"] = self.bot_data
        data["session"]  = self.session
        return await handler(event, data)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    bot_data = load_data()

    connector = aiohttp.TCPConnector(limit=100)
    session   = aiohttp.ClientSession(connector=connector)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Middleware
    middleware = DataMiddleware(bot_data, session)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)
    dp.business_connection.middleware(middleware)

    dp.include_router(router)

    # Set bot commands
    await bot.set_my_commands([
        BotCommand(command="start",    description="Botni ishga tushirish"),
        BotCommand(command="help",     description="Yordam"),
        BotCommand(command="clear",    description="Suhbatni tozalash"),
        BotCommand(command="bizstatus",description="Business holati"),
        BotCommand(command="groupai",  description="Guruh AI rejimi (admin)"),
        BotCommand(command="admin",    description="Admin panel"),
    ])

    # Webhook setup
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info("Webhook o'rnatildi: %s", WEBHOOK_URL)

    # aiohttp app
    app = web.Application()

    async def handle_webhook(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            update = Update.model_validate(body)
            await dp.feed_update(bot, update)
        except Exception as e:
            logger.error("Webhook request xato: %s", e)
        return web.Response(status=200)

    app.router.add_post(WEBHOOK_PATH, handle_webhook)

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    # Graceful shutdown
    async def on_shutdown(app_: web.Application):
        logger.info("Bot to'xtatilmoqda...")
        await bot.delete_webhook()
        await session.close()
        logger.info("Bot to'xtatildi.")

    app.on_shutdown.append(on_shutdown)

    logger.info("Server ishga tushdi: 0.0.0.0:%d", PORT)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    # Keep running
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
