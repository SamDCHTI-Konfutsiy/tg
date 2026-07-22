import os
import telebot

BOT_TOKEN = os.environ["BOT_TOKEN"]
bot = telebot.TeleBot(BOT_TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(message, "Salom! Men ishga tushdim ✅")


@bot.message_handler(func=lambda m: True)
def echo_all(message):
    bot.reply_to(message, message.text)


bot.infinity_polling()
