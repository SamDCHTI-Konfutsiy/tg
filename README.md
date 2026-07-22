# HSK Test Bot

Rasmiy HSK imtihon PDF'larini interaktiv Telegram testlariga aylantiruvchi bot.
Yolg'iz yoki do'stlar bilan (xona rejimida, vaqt va reyting bilan) yechish mumkin.

## Fayllar
- `main.py` — bot (test oqimi, xonalar, admin panel)
- `db.py` — JSON-asosli doimiy test kutubxonasi
- `hsk_parser.py` — imtihon PDF parseri (HSK1-6, universal)
- `answer_parser.py` — javoblar kaliti PDF parseri
- `requirements.txt` — kutubxonalar

## Foydalanuvchi imkoniyatlari
- PDF yuborish -> avtomatik tahlil -> test
- /tests -> kutubxonadagi tayyor testlar (HSK darajasi bo'yicha)
- Yolg'iz rejim: vaqt o'lchanadi, oxirida ball (javob kaliti bo'lsa)
- Xona rejimi: havola ulashiladi, 3-2-1 sanoq, hammaga bir xil savollar,
  jonli reyting (ball birinchi, teng bo'lsa vaqt hal qiladi)

## Admin (faqat ADMIN_ID)
- /admin -> kutubxona ro'yxati, testlarni o'chirish
- Javoblar kaliti PDF yuborsa -> qaysi testga biriktirishni tanlaydi
- Dublikat testlar avtomatik birlashadi: ko'proq savolli versiya qoladi

## O'rnatish (Railway)
Environment variables:
- `BOT_TOKEN` — BotFather'dan
- `ADMIN_ID` — admin Telegram ID (standart: 660086073)
- `DB_PATH` — ixtiyoriy; doimiy saqlash uchun Volume ulab `/data/exams_db.json`
  qiymatini bering (aks holda har deploy'da kutubxona tozalanadi!)

Start command: `python3 main.py`
