# Telegram Bot

## 1. Bot yaratish
@BotFather ga yozing → `/newbot` → tokenni oling.

## 2. Lokal test (ixtiyoriy)
```
pip install -r requirements.txt
export BOT_TOKEN=your_token_here
python main.py
```

## 3. GitHub'ga joylash
```
git init
git add .
git commit -m "Telegram bot"
git remote add origin <repo-url>
git push -u origin main
```
`.env` fayl yaratsangiz ham, u `.gitignore` tufayli push qilinmaydi — token GitHub'ga tushmaydi.

## 4. Render'da deploy
1. render.com → New → Web Service → shu repo'ni tanlang
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `python main.py`
4. Environment bo'limida `BOT_TOKEN` nomi bilan haqiqiy tokenni qo'shing (faqat shu yerda, kodda emas)
5. Deploy qiling — Render URL beradi, bot avtomatik webhook o'rnatadi

`render.yaml` fayli mavjud bo'lgani uchun Render "Blueprint" orqali ham bir necha bosishda deploy qilish mumkin.
