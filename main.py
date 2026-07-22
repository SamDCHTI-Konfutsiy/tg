import os
import telebot
from flask import Flask, request

# Token kodda emas — Render'ning Environment bo'limidan o'qiladi
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Render bu o'zgaruvchini deploy paytida avtomatik beradi
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(message, "Salom! Men ishga tushdim ✅")


@bot.message_handler(func=lambda m: True)
def echo_all(message):
    bot.reply_to(message, message.text)


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200


@app.route("/")
def index():
    return "Bot ishlayapti"


if __name__ == "__main__":
    bot.remove_webhook()
    if RENDER_EXTERNAL_URL:
        bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
