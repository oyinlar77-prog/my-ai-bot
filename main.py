"""
Telegram Bot — aiogram 3.x | Railway Webhook
Funksiyalar:
  - Gemini + Groq API key rotation
  - Per-user System Prompt (xarakter sozlamasi)
  - Suhbat tarixi (kontekst xotira)
  - Guruh moderatsiyasi (spam/link o'chirish)
  - Guruhda AI: @mention yoki reply qilganda javob
  - Barcha AI javoblarda markdown/maxsus belgilar tozalanadi
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

# Suhbat tarixi: har foydalanuvchida oxirgi N ta xabar saqlanadi
HISTORY_LIMIT = 10

# ─────────────────────────────────────────────────────────────────────────────
# Persistent storage — JSON fayl
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
# _db yapisi: { "characters": {user_id: str}, "group_ai": {chat_id: bool} }
_db.setdefault("characters", {})
_db.setdefault("group_ai", {})   # guruhda barcha xabarlarga javob berish rejimi


def get_character(user_id: int) -> str:
    return _db["characters"].get(str(user_id), "")

def set_character(user_id: int, text: str):
    _db["characters"][str(user_id)] = text
    _save_data(_db)

def del_character(user_id: int):
    _db["characters"].pop(str(user_id), None)
    _save_data(_db)

def is_group_ai_on(chat_id: int) -> bool:
    return _db["group_ai"].get(str(chat_id), False)

def toggle_group_ai(chat_id: int) -> bool:
    current = _db["group_ai"].get(str(chat_id), False)
    _db["group_ai"][str(chat_id)] = not current
    _save_data(_db)
    return not current

# ─────────────────────────────────────────────────────────────────────────────
# Conversation history (xotira, sessiya davomida)
# ─────────────────────────────────────────────────────────────────────────────
# { user_id: deque([{"role": "user"/"assistant", "content": "..."}, ...]) }
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
_BASE_RULES = (
    "QATTIQ QOIDA: Javobingda hech qachon quyidagi maxsus belgilarni ishlatma: "
    "*, **, #, ##, ###, ~, ~~, `, ```, _, __, @, &, $, ^, |, >, >>. "
    "Markdown formatlash yo'q. Faqat oddiy matn yoz. "
    "Zarur bo'lganda emoji ishlat, lekin ortiqcha emas.\n"
    "Javoblar aniq, tushunarli va qulay o'qilishi kerak.\n\n"
)

def build_system_prompt(user_id: int) -> str:
    custom = get_character(user_id)
    if custom:
        return _BASE_RULES + f"Foydalanuvchi xarakter ko'rsatmasi:\n{custom}"
    return _BASE_RULES + "Sen foydali, do'stona va aqlli AI yordamchisan."


# ─────────────────────────────────────────────────────────────────────────────
# Response cleaner — barcha markdown va keraksiz belgilarni olib tashlaydi
# ─────────────────────────────────────────────────────────────────────────────
_MARKDOWN_BOLD    = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_ITALIC  = re.compile(r"\*(.+?)\*")
_MARKDOWN_STRIKE  = re.compile(r"~~(.+?)~~")
_MARKDOWN_CODE    = re.compile(r"```[\s\S]*?```|`[^`]+`")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MARKDOWN_UNDER   = re.compile(r"__(.+?)__")
_MARKDOWN_UNDER2  = re.compile(r"_(.+?)_")
_LEFTOVER_CHARS   = re.compile(r"[*#~`^|>]")
_MULTI_NEWLINE    = re.compile(r"\n{3,}")
_LEAD_SPACES      = re.compile(r"^ +", re.MULTILINE)


def clean_response(text: str) -> str:
    """AI javobidan barcha markdown va keraksiz belgilarni olib tashlaydi."""
    text = _MARKDOWN_CODE.sub(lambda m: m.group(0).replace("`", ""), text)
    text = _MARKDOWN_BOLD.sub(r"\1", text)
    text = _MARKDOWN_ITALIC.sub(r"\1", text)
    text = _MARKDOWN_STRIKE.sub(r"\1", text)
    text = _MARKDOWN_UNDER.sub(r"\1", text)
    text = _MARKDOWN_UNDER2.sub(r"\1", text)
    text = _MARKDOWN_HEADING.sub("", text)
    text = _LEFTOVER_CHARS.sub("", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    text = _LEAD_SPACES.sub("", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# API Key Rotator
# ─────────────────────────────────────────────────────────────────────────────
class APIKeyRotator:
    def __init__(self, keys: list[str], provider: str):
        self.provider  = provider
        self.keys      = list(keys)
        self._cycle    = cycle(self.keys) if self.keys else iter([])
        self._failed: set[str] = set()

    def next_key(self) -> Optional[str]:
        for _ in range(max(len(self.keys), 1)):
            key = next(self._cycle, None)
            if key and key not in self._failed:
                return key
        return None

    def mark_failed(self, key: str):
        logger.warning(f"[{self.provider}] ...{key[-4:]} muvaffaqiyatsiz, o'tkazildi.")
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
# AI Providers (suhbat tarixi bilan)
# ─────────────────────────────────────────────────────────────────────────────
async def ask_gemini(user_id: int, user_text: str) -> Optional[str]:
    system = build_system_prompt(user_id)
    hist   = history_get(user_id)

    # Gemini format: contents ro'yxati
    contents = []
    for h in hist:
        role = "user" if h["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    for _ in range(max(len(GEMINI_KEYS), 1)):
        key = gemini_rot.next_key()
        if not key:
            break
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={key}"
        )
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=30)
                ) as r:
                    if r.status in (429, 403):
                        gemini_rot.mark_failed(key)
                        continue
                    if r.status != 200:
                        logger.error(f"Gemini HTTP {r.status}")
                        continue
                    data = await r.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logger.error(f"Gemini xato: {e}")
            gemini_rot.mark_failed(key)
    return None


async def ask_groq(user_id: int, user_text: str, model: str = "llama3-8b-8192") -> Optional[str]:
    system = build_system_prompt(user_id)
    hist   = history_get(user_id)

    messages = [{"role": "system", "content": system}]
    for h in hist:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})

    for _ in range(max(len(GROQ_KEYS), 1)):
        key = groq_rot.next_key()
        if not key:
            break
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 1024,
            "temperature": 0.7,
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload, headers=hdrs,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status in (429, 401):
                        groq_rot.mark_failed(key)
                        continue
                    if r.status != 200:
                        logger.error(f"Groq HTTP {r.status}")
                        continue
                    data = await r.json()
                    return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Groq xato: {e}")
            groq_rot.mark_failed(key)
    return None


async def ask_ai(user_id: int, user_text: str) -> str:
    """Gemini → Groq fallback. Tariх saqlanadi."""
    history_add(user_id, "user", user_text)

    answer = None
    if gemini_rot.has_keys:
        answer = await ask_gemini(user_id, user_text)
    if not answer and groq_rot.has_keys:
        answer = await ask_groq(user_id, user_text)

    if not answer:
        answer = "Hozirda AI xizmati mavjud emas. Keyinroq urinib ko'ring."
    else:
        answer = clean_response(answer)
        history_add(user_id, "assistant", answer)

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
    if _LINK_RE.search(text):  return True, "Havola/link"
    if _AD_RE.search(text):    return True, "Reklama matni"
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
    ],
    resize_keyboard=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────────────────────────────
class CharForm(StatesGroup):
    waiting = State()

# ─────────────────────────────────────────────────────────────────────────────
# Router & Handlers
# ─────────────────────────────────────────────────────────────────────────────
router = Router()


# ── /start ────────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    name = msg.from_user.first_name or "Do'stim"
    await msg.answer(
        f"Salom, {name}! Men aqlli AI yordamchiman.\n\n"
        "Nima qila olaman:\n"
        "  Sizga istalgan savolga javob beraman\n"
        "  Xarakter sozlashingiz mumkin — AI xuddi siz kabi gaplashadi\n"
        "  Guruhga qo'shilsam, @mention yoki reply orqali javob beraman\n"
        "  Guruhlarda spam va reklamani avtomatik o'chiraman\n\n"
        "Savol yozing yoki quyidagi tugmalardan foydalaning.",
        reply_markup=MAIN_KB,
    )


# ── /help ─────────────────────────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "Buyruqlar:\n\n"
        "/start       — Botni ishga tushirish\n"
        "/help        — Yordam\n"
        "/reset       — API kalitlarini yangilash\n"
        "/clear       — Suhbat tarixini tozalash\n"
        "/groupai     — Guruhda barcha xabarlarga AI javob (admin)\n\n"
        "Tugmalar:\n"
        "  'Mening xarakterim' — AI uslubini o'rgatish\n"
        "  'Xarakterimni korish' — Joriy sozlamani ko'rish\n"
        "  'Xarakterimni ochirish' — Standart rejimga qaytish\n"
        "  'Suhbatni tozalash' — Xotira tozalanadi\n\n"
        "Guruh uchun:\n"
        "  Botga @mention qiling yoki reply qiling — AI javob beradi\n"
        "  Bot guruhda admin bo'lishi kerak (xabar o'chirish uchun).",
    )


# ── /reset ────────────────────────────────────────────────────────────────────
@router.message(Command("reset"))
async def cmd_reset(msg: Message):
    gemini_rot.reset()
    groq_rot.reset()
    await msg.answer("API kalitlari yangilandi.")


# ── /clear ────────────────────────────────────────────────────────────────────
@router.message(Command("clear"))
async def cmd_clear(msg: Message):
    history_clear(msg.from_user.id)
    await msg.answer("Suhbat tarixi tozalandi. Yangi suhbat boshlaylik!")


# ── /groupai — guruhda barcha xabarlarga javob rejimi (admin only) ────────────
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
    status = "yoqildi" if state else "o'chirildi"
    await msg.reply(
        f"Guruh AI rejimi {status}.\n"
        + ("Endi barcha xabarlarga AI javob beradi." if state
           else "Endi faqat @mention yoki reply orqali javob beradi.")
    )


# ── Tugma: Mening xarakterim ─────────────────────────────────────────────────
@router.message(F.text == "Mening xarakterim")
async def btn_set_character(msg: Message, state: FSMContext):
    await state.set_state(CharForm.waiting)
    current = get_character(msg.from_user.id)
    current_text = f"\n\nHozirgi xarakter:\n{current}" if current else ""

    await msg.answer(
        "AI qanday uslubda javob bersin? Namunalar:\n\n"
        "  'Mening nomimdan javob ber, juda jiddiy va rasmiy tonda'\n"
        "  'Dostona, hazillashib va ko'cha jargonlarida gaplash'\n"
        "  'Har doim qisqa va aniq javob ber, ortiqcha gap yozma'\n"
        "  'Ingliz tilida javob ber, professional ohangda'\n"
        "  'Sen mening do'stim Jasur san, 25 yoshda, futbol ixlosmandisan'\n\n"
        "Xarakter ko'rsatmangizni yuboring:"
        + current_text,
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(CharForm.waiting, F.text)
async def receive_character(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if len(text) < 5:
        await msg.answer("Juda qisqa. Iltimos batafsil yozing (kamida 5 ta belgi).")
        return
    set_character(msg.from_user.id, text)
    await state.clear()
    await msg.answer(
        "Xarakter saqlandi!\n\n"
        f"{text}\n\n"
        "Endi AI xuddi shu ko'rsatma asosida javob beradi. "
        "Istalgan vaqt o'zgartirish mumkin.",
        reply_markup=MAIN_KB,
    )


# ── Tugma: Xarakterimni ko'rish ───────────────────────────────────────────────
@router.message(F.text == "Xarakterimni ko'rish")
async def btn_view_character(msg: Message):
    char = get_character(msg.from_user.id)
    if char:
        await msg.answer(f"Joriy xarakter ko'rsatmasi:\n\n{char}")
    else:
        await msg.answer(
            "Xarakter sozlanmagan.\n"
            "Standart AI rejimi ishlayapti.\n"
            "'Mening xarakterim' tugmasidan sozlang."
        )


# ── Tugma: Xarakterimni o'chirish ─────────────────────────────────────────────
@router.message(F.text == "Xarakterimni o'chirish")
async def btn_delete_character(msg: Message):
    uid = msg.from_user.id
    if get_character(uid):
        del_character(uid)
        await msg.answer(
            "Xarakter o'chirildi.\n"
            "Standart AI rejimiga qaytildi."
        )
    else:
        await msg.answer("Xarakter avval ham sozlanmagan edi.")


# ── Tugma: Suhbatni tozalash ──────────────────────────────────────────────────
@router.message(F.text == "Suhbatni tozalash")
async def btn_clear_history(msg: Message):
    history_clear(msg.from_user.id)
    await msg.answer(
        "Suhbat tarixi tozalandi.\n"
        "AI oldingi xabarlarni eslamaydi. Yangi suhbat boshlaylik!"
    )


# ── Guruh handler: moderatsiya + AI ──────────────────────────────────────────
@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
)
async def group_handler(msg: Message, bot: Bot):
    if not msg.text or not msg.from_user:
        return

    # Admin tekshiruvi
    try:
        member = await msg.chat.get_member(msg.from_user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False

    # Moderatsiya (adminlar bundan ozod)
    if not is_admin:
        spam, reason = is_spam(msg.text)
        if spam:
            await delete_and_warn(msg, reason)
            return

    # AI javob sharti
    bot_info = await bot.get_me()
    bot_username = (bot_info.username or "").lower()

    text_lower  = msg.text.lower()
    mentioned   = f"@{bot_username}" in text_lower
    reply_to_me = (
        msg.reply_to_message is not None
        and msg.reply_to_message.from_user is not None
        and msg.reply_to_message.from_user.id == bot_info.id
    )
    group_ai_mode = is_group_ai_on(msg.chat.id)

    should_respond = mentioned or reply_to_me or group_ai_mode

    if should_respond:
        # Mention matnini tozalash
        clean_text = re.sub(
            rf"@{re.escape(bot_username)}", "", msg.text, flags=re.IGNORECASE
        ).strip()
        if not clean_text:
            clean_text = "Salom!"

        await bot.send_chat_action(msg.chat.id, "typing")
        thinking = await msg.reply("...")
        answer = await ask_ai(msg.from_user.id, clean_text)
        await thinking.delete()
        await msg.reply(answer)


# ── Shaxsiy chat: barcha matnlarga AI javob ───────────────────────────────────
@router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def private_ai(msg: Message, state: FSMContext):
    # FSM holati aktiv bo'lsa — bu handler ishlamaydi (FSM ustunlik qiladi)
    await msg.bot.send_chat_action(msg.chat.id, "typing")
    thinking = await msg.answer("...")
    answer = await ask_ai(msg.from_user.id, msg.text)
    await thinking.delete()
    await msg.answer(answer)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook app setup
# ─────────────────────────────────────────────────────────────────────────────
async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start",   description="Botni ishga tushirish"),
        BotCommand(command="help",    description="Yordam"),
        BotCommand(command="clear",   description="Suhbat tarixini tozalash"),
        BotCommand(command="reset",   description="API kalitlarini yangilash"),
        BotCommand(command="groupai", description="Guruhda AI rejimi (admin)"),
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

    logger.info(f"Server ishga tushdi: 0.0.0.0:{PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
