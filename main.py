import os
import logging

import telebot
from telebot import types

from hsk_parser import parse_exam
from answer_parser import parse_answer_key

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hsk-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
bot = telebot.TeleBot(BOT_TOKEN)

# In-memory per-chat session store.
# state: "idle" | "await_answer_key" | "quiz"
sessions = {}


def new_session():
    return {
        "state": "idle",
        "exam": None,          # parsed exam dict
        "answer_key": None,    # {number: letter} or None
        "order": [],           # question indices in quiz order
        "pos": 0,              # current position in `order`
        "user_answers": {},    # {question_number: letter}
        "quiz_message_id": None,
    }


def get_session(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = new_session()
    return sessions[chat_id]


# --------------------------------------------------------------------
# /start
# --------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    sessions[message.chat.id] = new_session()
    bot.reply_to(
        message,
        "Salom! 👋 Menga rasmiy HSK imtihon PDF faylini yuboring — "
        "men undan interaktiv test tuzib beraman.\n\n"
        "Ixtiyoriy: agar javoblar kaliti PDF fayli ham bo'lsa, imtihon "
        "faylidan keyin uni ham yuboring — testni tekshirib, ball "
        "chiqarib beraman.",
    )


@bot.message_handler(commands=["skip"])
def cmd_skip(message):
    s = get_session(message.chat.id)
    if s["state"] != "await_answer_key":
        return
    start_quiz(message.chat.id)


# --------------------------------------------------------------------
# Document handling
# --------------------------------------------------------------------
@bot.message_handler(content_types=["document"])
def handle_document(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    doc = message.document

    if not (doc.file_name or "").lower().endswith(".pdf"):
        bot.reply_to(message, "Iltimos, faqat PDF fayl yuboring.")
        return

    file_info = bot.get_file(doc.file_id)
    data = bot.download_file(file_info.file_path)
    tmp_path = f"/tmp/{doc.file_unique_id}.pdf"
    with open(tmp_path, "wb") as f:
        f.write(data)

    if s["state"] == "await_answer_key":
        _handle_answer_key_upload(message, tmp_path)
        return

    _handle_exam_upload(message, tmp_path)


def _handle_exam_upload(message, path):
    chat_id = message.chat.id
    wait = bot.reply_to(message, "📄 Fayl tahlil qilinmoqda...")
    try:
        exam = parse_exam(path)
    except Exception:
        log.exception("exam parse failed")
        bot.edit_message_text(
            "Faylni o'qib bo'lmadi. PDF matn qatlamiga ega ekanligiga "
            "ishonch hosil qiling va qayta yuboring.",
            chat_id, wait.message_id,
        )
        return

    if not exam["questions"]:
        bot.edit_message_text(
            "Bu fayldan test savollari topilmadi. Rasmiy HSK imtihon "
            "PDF faylini yuborganingizga ishonch hosil qiling.",
            chat_id, wait.message_id,
        )
        return

    s = get_session(chat_id)
    s["exam"] = exam
    s["answer_key"] = None
    s["order"] = [q["number"] for q in exam["questions"]]
    s["pos"] = 0
    s["user_answers"] = {}
    s["state"] = "await_answer_key"

    level = exam["level"] or "aniqlanmadi"
    code = exam["code"] or "aniqlanmadi"
    n = len(exam["questions"])
    w = len(exam["writing_tasks"])

    text = (
        f"✅ Tahlil qilindi!\n\n"
        f"HSK daraja: {level}\n"
        f"Imtihon kodi: {code}\n"
        f"Test savollari (variantli): {n} ta\n"
        f"Yozma topshiriqlar: {w} ta (interaktiv tekshirilmaydi)\n\n"
        f"Agar rasmiy javoblar kaliti PDF faylingiz bo'lsa, hozir "
        f"yuboring — test oxirida ballingizni ko'rsataman.\n"
        f"Bo'lmasa, /skip yozing yoki pastdagi tugmani bosing."
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➡️ Javoblarsiz boshlash", callback_data="skip_key"))
    bot.edit_message_text(text, chat_id, wait.message_id)
    bot.send_message(chat_id, "Davom etish uchun tanlang:", reply_markup=kb)


def _handle_answer_key_upload(message, path):
    chat_id = message.chat.id
    wait = bot.reply_to(message, "📄 Javoblar kaliti tahlil qilinmoqda...")
    try:
        answers = parse_answer_key(path)
    except Exception:
        log.exception("answer key parse failed")
        answers = {}

    s = get_session(chat_id)
    if not answers:
        bot.edit_message_text(
            "Javoblar kalitini o'qib bo'lmadi, shuning uchun test "
            "javoblarsiz boshlanadi.",
            chat_id, wait.message_id,
        )
    else:
        s["answer_key"] = answers
        bot.edit_message_text(
            f"✅ Javoblar kaliti o'qildi ({len(answers)} ta javob). "
            f"Test oxirida ballingiz ko'rsatiladi.",
            chat_id, wait.message_id,
        )
    start_quiz(chat_id)


@bot.callback_query_handler(func=lambda c: c.data == "skip_key")
def cb_skip_key(call):
    bot.answer_callback_query(call.id)
    start_quiz(call.message.chat.id)


# --------------------------------------------------------------------
# Quiz flow
# --------------------------------------------------------------------
def start_quiz(chat_id):
    s = get_session(chat_id)
    s["state"] = "quiz"
    s["pos"] = 0
    s["user_answers"] = {}
    msg = bot.send_message(chat_id, "Test boshlanmoqda...")
    s["quiz_message_id"] = msg.message_id
    send_question(chat_id)


def _question_by_number(exam, number):
    for q in exam["questions"]:
        if q["number"] == number:
            return q
    return None


def _format_question(q, pos, total):
    lines = [f"📝 Savol {pos + 1}/{total}  (№{q['number']}, {q['section']})", ""]
    if q["passage"]:
        lines.append(q["passage"])
        lines.append("")
    if q["stem"]:
        lines.append(q["stem"])
        lines.append("")
    for letter in "ABCD":
        lines.append(f"{letter}. {q['options'][letter]}")
    text = "\n".join(lines)
    return text[:4000]  # stay under Telegram's message size limit


def send_question(chat_id):
    s = get_session(chat_id)
    exam = s["exam"]
    total = len(s["order"])

    if s["pos"] >= total:
        finish_quiz(chat_id)
        return

    number = s["order"][s["pos"]]
    q = _question_by_number(exam, number)
    text = _format_question(q, s["pos"], total)

    kb = types.InlineKeyboardMarkup(row_width=4)
    kb.add(*[
        types.InlineKeyboardButton(letter, callback_data=f"ans:{number}:{letter}")
        for letter in "ABCD"
    ])

    try:
        bot.edit_message_text(text, chat_id, s["quiz_message_id"], reply_markup=kb)
    except Exception:
        msg = bot.send_message(chat_id, text, reply_markup=kb)
        s["quiz_message_id"] = msg.message_id


@bot.callback_query_handler(func=lambda c: c.data.startswith("ans:"))
def cb_answer(call):
    chat_id = call.message.chat.id
    s = get_session(chat_id)
    if s["state"] != "quiz":
        bot.answer_callback_query(call.id)
        return

    _, number_s, letter = call.data.split(":")
    number = int(number_s)
    s["user_answers"][number] = letter
    bot.answer_callback_query(call.id, text=f"Tanlandi: {letter}")

    s["pos"] += 1
    send_question(chat_id)


def finish_quiz(chat_id):
    s = get_session(chat_id)
    exam = s["exam"]
    answer_key = s["answer_key"]
    s["state"] = "idle"

    lines = ["🏁 Test yakunlandi!\n"]

    if answer_key:
        correct = 0
        graded = 0
        detail = []
        for number in s["order"]:
            user_letter = s["user_answers"].get(number)
            correct_letter = answer_key.get(number)
            if correct_letter is None:
                continue
            graded += 1
            is_ok = user_letter == correct_letter
            if is_ok:
                correct += 1
            mark = "✅" if is_ok else "❌"
            detail.append(
                f"{mark} №{number}: sizniki {user_letter or '—'}"
                + ("" if is_ok else f", to'g'risi {correct_letter}")
            )
        pct = round(100 * correct / graded) if graded else 0
        lines.append(f"Natija: {correct}/{graded} to'g'ri ({pct}%)\n")
        lines.append("\n".join(detail))
    else:
        lines.append("Javoblar kaliti berilmagani uchun ball hisoblanmadi. "
                      "Sizning tanlovlaringiz:\n")
        lines.append("\n".join(
            f"№{n}: {s['user_answers'].get(n, '—')}" for n in s["order"]
        ))

    if exam["writing_tasks"]:
        lines.append(
            f"\n\n✍️ Yozma bo'limda {len(exam['writing_tasks'])} ta "
            f"topshiriq bor edi (bular avtomatik tekshirilmaydi)."
        )

    full_text = "\n".join(lines)
    for chunk_start in range(0, len(full_text), 4000):
        bot.send_message(chat_id, full_text[chunk_start:chunk_start + 4000])

    bot.send_message(chat_id, "Yangi test uchun /start yozing yoki boshqa PDF yuboring.")


if __name__ == "__main__":
    log.info("Bot polling boshlandi")
    bot.infinity_polling()
