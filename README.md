# HSK Test Bot

Rasmiy HSK imtihon PDF faylini yuklang — bot uni tahlil qilib,
Telegram ichida interaktiv testga aylantiradi. Ixtiyoriy ravishda
javoblar kaliti PDF faylini ham yuborsangiz, test oxirida bot
javoblaringizni tekshirib, ballingizni chiqarib beradi.

## Fayllar

- `main.py` — bot logikasi (polling, hujjat qabul qilish, test oqimi)
- `hsk_parser.py` — imtihon PDF'ini savol/variantlarga ajratuvchi parser
- `answer_parser.py` — rasmiy javoblar kaliti PDF'ini o'qivchi parser
- `requirements.txt` — kerakli kutubxonalar

## Qanday ishlaydi

1. `/start` — botni ishga tushirish
2. HSK imtihon PDF faylini yuborasiz (masalan H51001.pdf)
3. Bot tahlil qiladi: daraja, imtihon kodi, nechta savol topilgani
4. Ixtiyoriy: javoblar kaliti PDF faylini yuborasiz, yoki `/skip`
5. Test boshlanadi — har bir savol uchun A/B/C/D tugmalari chiqadi
6. Oxirida natija: agar javoblar kaliti berilgan bo'lsa — ball va
   har bir savol bo'yicha to'g'ri/xato; bo'lmasa — faqat tanlovlar

## Parser haqida

Parser aniq shu H51001 fayliga moslashtirilmagan — u HSK imtihonlar
uchun umumiy bo'lgan tuzilishni (raqamlangan savollar, A-D variantlar,
ikki ustunli tinglash bo'limi, umumiy matnli o'qish savollari,
javoblar kaliti jadvali) avtomatik aniqlaydi. Shu sababli boshqa HSK
darajalari (1-6) va boshqa yillardagi rasmiy PDF fayllar bilan ham
ishlashi kutiladi, lekin har bir PDF export'ining o'z formatlashi
farq qilishi mumkin — parser ba'zi savollarni topa olmasa, ular
shunchaki testga kiritilmaydi (xato bermaydi).

Yozma bo'lim (gap to'ldirish, insho) avtomatik tekshirilmaydi —
bular matn sifatida ko'rsatiladi, lekin ball hisoblanmaydi.

## O'rnatish (Render/Railway)

`BOT_TOKEN` environment variable'ini qo'shing (BotFather'dan olingan
token). Start command: `python3 main.py`.
