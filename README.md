# Telegram Bot — Railway + aiogram 3.x

## Fayl tuzilmasi
```
telegram_bot/
├── main.py           # Asosiy bot kodi
├── requirements.txt  # Kutubxonalar
├── Procfile          # Railway ishga tushirish
├── .env.example      # O'zgaruvchilar namunasi
└── README.md
```

---

## Railway'da deploy

### 1. GitHub'ga yuklang
```bash
git init
git add .
git commit -m "init"
git push origin main
```

### 2. Railway Variables
| Kalit             | Qiymat                               |
|-------------------|--------------------------------------|
| BOT_TOKEN         | BotFather tokeni                     |
| WEBHOOK_HOST      | https://your-app.up.railway.app      |
| GEMINI_API_KEYS   | key1,key2,key3 (vergul bilan)        |
| GROQ_API_KEYS     | key1,key2,key3 (vergul bilan)        |

PORT — Railway avtomatik beradi.

### 3. WEBHOOK_HOST
Deploy tugagach: Settings → Domains → domenni nusxalab WEBHOOK_HOST ga qo'ying.

---

## Funksiyalar

### Xarakter sozlamasi (System Prompt)
- "Mening xarakterim" tugmasi → AI qanday gaplashishini o'rgating
- Har foydalanuvchining o'z xarakteri saqlanadi (bot_data.json)
- "Xarakterimni ko'rish" / "Xarakterimni o'chirish" tugmalari mavjud

### Suhbat tarixi (Context)
- Oxirgi 10 ta xabar saqlanadi — AI kontekst bilan javob beradi
- "Suhbatni tozalash" tugmasi yoki /clear buyrug'i

### AI javoblar — toza matn
- Barcha markdown: **, *, #, ~, `, _ olib tashlanadi
- Faqat oddiy matn + emoji qaytariladi

### API Rotation
- Gemini → Groq fallback
- 429/403 xatosida kalit avtomatik almashadi
- /reset bilan barcha kalitlar qayta faollashadi

### Guruh rejimi
- @mention qilinsa yoki reply qilinsa — AI javob beradi
- /groupai (admin) — barcha xabarlarga AI javob rejimi
- Adminlar bo'lmagan foydalanuvchilar uchun spam moderatsiyasi

---

## Lokal test (ngrok bilan)
```bash
pip install -r requirements.txt
ngrok http 8080

export BOT_TOKEN="..."
export WEBHOOK_HOST="https://xxxx.ngrok.io"
export GEMINI_API_KEYS="key1,key2"
export GROQ_API_KEYS="key1,key2"
python main.py
```
