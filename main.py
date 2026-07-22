import os
import time
import random
import string
import logging
import threading

import telebot
from telebot import types

import db
from hsk_parser import parse_exam
from answer_parser import parse_answer_key

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hsk-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "660086073"))
bot = telebot.TeleBot(BOT_TOKEN)

sessions = {}          # chat_id -> personal quiz session (solo or room participant)
rooms = {}             # room_id -> group-quiz room
pending_answer_keys = {}
_bot_username_cache = None


def new_session():
    return {
        "state": "idle",       # idle | await_answer_key | quiz
        "level": None,
        "code": None,
        "questions": [],
        "writing_tasks": [],
        "answer_key": None,
        "order": [],
        "pos": 0,
        "user_answers": {},
        "quiz_message_id": None,
        "room_id": None,
        "start_time": None,
    }


def get_session(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = new_session()
    return sessions[chat_id]


def safe_handler(func):
    def wrapper(update, *args, **kwargs):
        try:
            return func(update, *args, **kwargs)
        except Exception:
            log.exception("Handler xatosi: %s", func.__name__)
            try:
                chat_id = update.message.chat.id if hasattr(update, "message") else update.chat.id
                bot.send_message(
                    chat_id,
                    "⚠️ Kutilmagan xatolik yuz berdi. /start yozib qayta urinib ko'ring.",
                )
            except Exception:
                log.exception("Foydalanuvchiga xato haqida xabar berib bo'lmadi")
    return wrapper


def is_admin(user_id):
    return user_id == ADMIN_ID


def fmt_duration(seconds):
    seconds = max(0, int(seconds or 0))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def bot_username():
    global _bot_username_cache
    if _bot_username_cache is None:
        _bot_username_cache = bot.get_me().username
    return _bot_username_cache


def room_link(room_id):
    return f"https://t.me/{bot_username()}?start=room_{room_id}"


def offer_mode_choice(chat_id, level, code, message_id=None, text="Qanday o'ynaymiz?"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🙋 Yolg'iz o'ynayman", callback_data=f"solo:{level}:{code}"))
    kb.add(types.InlineKeyboardButton("👥 Do'stlar bilan (xona)", callback_data=f"room:{level}:{code}"))
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)


# --------------------------------------------------------------------
# /start (with optional room_<id> deep link), /tests, /admin
# --------------------------------------------------------------------
@bot.message_handler(commands=["start"])
@safe_handler
def cmd_start(message):
    parts = message.text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else None

    if payload and payload.startswith("room_"):
        join_room(message, payload[len("room_"):])
        return

    sessions[message.chat.id] = new_session()
    bot.reply_to(
        message,
        "Salom! 👋\n\n"
        "📄 Menga rasmiy HSK imtihon PDF faylini yuboring — men undan interaktiv "
        "test tuzib beraman.\n"
        "📚 Yoki /tests — boshqalar yuklagan tayyor testlardan tanlang.\n"
        "👥 Testni tanlagach, do'stlaringiz bilan xona ochib, birga yechishingiz "
        "ham mumkin — hammaga bir xil savollar, vaqt bo'yicha reyting bilan!\n\n"
        "Ixtiyoriy: javoblar kaliti PDF fayli — test oxirida ballingiz chiqadi.",
    )


@bot.message_handler(commands=["skip"])
@safe_handler
def cmd_skip(message):
    s = get_session(message.chat.id)
    if s["state"] != "await_answer_key":
        return
    offer_mode_choice(message.chat.id, s["level"], s["code"])


@bot.message_handler(commands=["tests"])
@safe_handler
def cmd_tests(message):
    levels = sorted({e["level"] for e in db.list_exams()})
    if not levels:
        bot.reply_to(message, "Kutubxonada hali testlar yo'q. PDF yuborib birinchi bo'ling!")
        return
    kb = types.InlineKeyboardMarkup()
    for lvl in levels:
        kb.add(types.InlineKeyboardButton(f"HSK{lvl}", callback_data=f"lvl:{lvl}"))
    bot.send_message(message.chat.id, "Qaysi HSK darajasi?", reply_markup=kb)


@bot.message_handler(commands=["admin"])
@safe_handler
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return
    send_admin_panel(message.chat.id)


def send_admin_panel(chat_id, message_id=None):
    exams = db.list_exams()
    text = (
        f"🛠 Admin panel\n\nKutubxonada {len(exams)} ta test bor.\n\n"
        f"PDF yuborsangiz avtomatik qo'shiladi (yaxshirog'i saqlanadi)."
    )
    kb = types.InlineKeyboardMarkup()
    for e in exams:
        flag = "✅" if e.get("answer_key") else "➖"
        kb.add(types.InlineKeyboardButton(
            f"{flag} HSK{e['level']} {e['code']} ({len(e['questions'])} ta) 🗑",
            callback_data=f"delexam:{e['level']}:{e['code']}",
        ))
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)


# --------------------------------------------------------------------
# Document handling (exam OR answer-key PDF, auto-detected)
# --------------------------------------------------------------------
@bot.message_handler(content_types=["document"])
@safe_handler
def handle_document(message):
    chat_id = message.chat.id
    doc = message.document

    if not (doc.file_name or "").lower().endswith(".pdf"):
        bot.reply_to(message, "Iltimos, faqat PDF fayl yuboring.")
        return

    file_info = bot.get_file(doc.file_id)
    raw = bot.download_file(file_info.file_path)
    tmp_path = f"/tmp/{doc.file_unique_id}.pdf"
    with open(tmp_path, "wb") as f:
        f.write(raw)

    wait = bot.reply_to(message, "📄 Fayl tahlil qilinmoqda...")

    try:
        exam = parse_exam(tmp_path)
    except Exception:
        log.exception("exam parse failed")
        exam = {"questions": [], "writing_tasks": [], "level": None, "code": None}

    if len(exam["questions"]) >= 5:
        process_exam_upload(message, wait, exam, doc)
        return

    try:
        answers = parse_answer_key(tmp_path)
    except Exception:
        log.exception("answer key parse failed")
        answers = {}

    if answers:
        process_answer_key_upload(message, wait, answers)
        return

    bot.edit_message_text(
        "Bu fayldan na savollar, na javoblar kaliti topilmadi. "
        "Rasmiy HSK PDF ekanligiga ishonch hosil qiling.",
        chat_id, wait.message_id,
    )


def process_exam_upload(message, wait, exam, doc):
    chat_id = message.chat.id
    level = db.normalize_level(exam["level"])
    code = exam["code"] or f"NOCODE-{doc.file_unique_id[:8]}"

    record, replaced = db.upsert_exam(
        level, code, exam["questions"], exam["writing_tasks"],
        uploaded_by=message.from_user.id,
        file_unique_id=doc.file_unique_id,
    )

    s = get_session(chat_id)
    s["level"] = level
    s["code"] = code
    s["questions"] = record["questions"]
    s["writing_tasks"] = record["writing_tasks"]
    s["answer_key"] = record.get("answer_key")
    s["order"] = [q["number"] for q in record["questions"]]
    s["state"] = "await_answer_key"

    n = len(record["questions"])
    w = len(record["writing_tasks"])
    lib_note = (
        "🗄 Kutubxonaga saqlandi (yangi eng yaxshi versiya)."
        if replaced else
        "🗄 Kutubxonada bu testning tanishroq versiyasi bor edi — o'sha ishlatiladi."
    )
    text = (
        f"✅ Tahlil qilindi!\n\nHSK daraja: {level}\nImtihon kodi: {code}\n"
        f"Aniqlangan savollar: {n} ta\nYozma topshiriqlar: {w} ta\n{lib_note}\n"
    )

    if s["answer_key"]:
        text += "\n✅ Javoblar kaliti kutubxonada allaqachon mavjud — avtomatik ishlatiladi."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➡️ Davom etish", callback_data="skip_key"))
        bot.edit_message_text(text, chat_id, wait.message_id)
        bot.send_message(chat_id, "Davom etish uchun:", reply_markup=kb)
    else:
        text += (
            "\nAgar rasmiy javoblar kaliti PDF faylingiz bo'lsa, hozir yuboring — "
            "ball chiqaraman va kutubxonaga saqlayman.\nBo'lmasa, /skip yozing."
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➡️ Javoblarsiz davom etish", callback_data="skip_key"))
        bot.edit_message_text(text, chat_id, wait.message_id)
        bot.send_message(chat_id, "Davom etish uchun tanlang:", reply_markup=kb)


def process_answer_key_upload(message, wait, answers):
    chat_id = message.chat.id
    s = get_session(chat_id)

    if s["state"] == "await_answer_key" and s.get("code"):
        s["answer_key"] = answers
        db.attach_answer_key(s["level"], s["code"], answers)
        bot.edit_message_text(
            f"✅ Javoblar kaliti o'qildi ({len(answers)} ta javob) va kutubxonaga saqlandi.",
            chat_id, wait.message_id,
        )
        offer_mode_choice(chat_id, s["level"], s["code"])
        return

    if is_admin(message.from_user.id):
        missing = [e for e in db.list_exams() if not e.get("answer_key")]
        if not missing:
            bot.edit_message_text(
                f"Javoblar kaliti o'qildi ({len(answers)} ta), lekin biriktiriladigan "
                f"test yo'q.", chat_id, wait.message_id,
            )
            return
        pending_answer_keys[chat_id] = answers
        kb = types.InlineKeyboardMarkup()
        for e in missing[:30]:
            kb.add(types.InlineKeyboardButton(
                f"HSK{e['level']} {e['code']}", callback_data=f"attachkey:{e['level']}:{e['code']}",
            ))
        bot.edit_message_text(
            f"✅ Javoblar kaliti o'qildi ({len(answers)} ta). Qaysi testga biriktiraman?",
            chat_id, wait.message_id, reply_markup=kb,
        )
        return

    bot.edit_message_text(
        "Bu javoblar kaliti fayliga o'xshaydi. Avval imtihon PDF faylini yuboring.",
        chat_id, wait.message_id,
    )


# --------------------------------------------------------------------
# /tests browsing -> mode choice (solo / room)
# --------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("lvl:"))
@safe_handler
def cb_level(call):
    level = call.data.split(":", 1)[1]
    exams = db.list_exams(level)
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    if not exams:
        bot.edit_message_text("Bu darajada hali test yo'q.", chat_id, call.message.message_id)
        return
    kb = types.InlineKeyboardMarkup()
    for e in exams:
        mark = "✅" if e.get("answer_key") else ""
        kb.add(types.InlineKeyboardButton(
            f"{e['code']} ({len(e['questions'])} ta savol) {mark}",
            callback_data=f"exam:{e['level']}:{e['code']}",
        ))
    bot.edit_message_text(f"HSK{level} testlari:", chat_id, call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("exam:"))
@safe_handler
def cb_exam(call):
    _, level, code = call.data.split(":", 2)
    record = db.get_exam(level, code)
    chat_id = call.message.chat.id
    if not record:
        bot.answer_callback_query(call.id, "Topilmadi")
        return
    bot.answer_callback_query(call.id)
    offer_mode_choice(
        chat_id, level, code, call.message.message_id,
        text=f"HSK{level} {code} ({len(record['questions'])} ta savol)\n\nQanday o'ynaymiz?",
    )


@bot.callback_query_handler(func=lambda c: c.data == "skip_key")
@safe_handler
def cb_skip_key(call):
    chat_id = call.message.chat.id
    s = get_session(chat_id)
    bot.answer_callback_query(call.id)
    offer_mode_choice(chat_id, s["level"], s["code"], call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("solo:"))
@safe_handler
def cb_solo(call):
    _, level, code = call.data.split(":", 2)
    record = db.get_exam(level, code)
    chat_id = call.message.chat.id
    if not record:
        bot.answer_callback_query(call.id, "Topilmadi")
        return
    s = get_session(chat_id)
    s["level"] = level
    s["code"] = code
    s["questions"] = record["questions"]
    s["writing_tasks"] = record["writing_tasks"]
    s["answer_key"] = record.get("answer_key")
    s["order"] = [q["number"] for q in record["questions"]]
    s["room_id"] = None
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    start_quiz(chat_id)


# --------------------------------------------------------------------
# Rooms — play together with friends via a shareable link
# --------------------------------------------------------------------
def gen_room_id():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def add_participant(room_id, user, chat_id):
    rooms[room_id]["participants"][user.id] = {
        "chat_id": chat_id,
        "name": user.first_name or user.username or "Foydalanuvchi",
        "finished": False,
        "score": None,
        "graded": None,
        "duration": None,
    }


def render_lobby(room_id, message_id=None, chat_id=None):
    room = rooms[room_id]
    names = [p["name"] for p in room["participants"].values()]
    text = (
        f"👥 Xona ochildi!\n\n"
        f"Test: HSK{room['level']} {room['code']} ({len(room['questions'])} ta savol)\n\n"
        f"Do'stlaringizga shu havolani yuboring:\n{room_link(room_id)}\n\n"
        f"Qo'shilganlar ({len(names)}): {', '.join(names)}\n\n"
        f"Hammasi tayyor bo'lsa, pastdagi tugmani bosing."
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 Yangilash", callback_data=f"roomrefresh:{room_id}"))
    kb.add(types.InlineKeyboardButton("🚀 Boshlash", callback_data=f"roomstart:{room_id}"))
    chat_id = chat_id or room["lobby_chat_id"]
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            room["lobby_message_id"] = message_id
            return
        except Exception:
            pass
    msg = bot.send_message(chat_id, text, reply_markup=kb)
    room["lobby_message_id"] = msg.message_id


@bot.callback_query_handler(func=lambda c: c.data.startswith("room:"))
@safe_handler
def cb_room(call):
    _, level, code = call.data.split(":", 2)
    record = db.get_exam(level, code)
    if not record:
        bot.answer_callback_query(call.id, "Topilmadi")
        return
    room_id = gen_room_id()
    while room_id in rooms:
        room_id = gen_room_id()
    rooms[room_id] = {
        "id": room_id, "level": level, "code": code,
        "questions": record["questions"], "writing_tasks": record["writing_tasks"],
        "answer_key": record.get("answer_key"),
        "order": [q["number"] for q in record["questions"]],
        "creator_id": call.from_user.id,
        "lobby_chat_id": call.message.chat.id,
        "lobby_message_id": None,
        "state": "waiting",
        "participants": {},
    }
    add_participant(room_id, call.from_user, call.message.chat.id)
    bot.answer_callback_query(call.id)
    render_lobby(room_id, message_id=call.message.message_id)


def join_room(message, room_id):
    room = rooms.get(room_id)
    chat_id = message.chat.id
    if not room:
        bot.reply_to(message, "Bu xona topilmadi yoki muddati o'tgan. /tests orqali yangi test tanlang.")
        return
    if room["state"] != "waiting":
        bot.reply_to(message, "Bu xonada test allaqachon boshlangan yoki tugagan.")
        return
    add_participant(room_id, message.from_user, chat_id)
    bot.reply_to(
        message,
        f"✅ Xonaga qo'shildingiz!\nTest: HSK{room['level']} {room['code']}\n"
        f"Xona egasi boshlashini kuting...",
    )
    if room["lobby_message_id"]:
        render_lobby(room_id, message_id=room["lobby_message_id"], chat_id=room["lobby_chat_id"])


@bot.callback_query_handler(func=lambda c: c.data.startswith("roomrefresh:"))
@safe_handler
def cb_room_refresh(call):
    room_id = call.data.split(":", 1)[1]
    if room_id not in rooms:
        bot.answer_callback_query(call.id, "Xona topilmadi")
        return
    bot.answer_callback_query(call.id)
    render_lobby(room_id, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("roomstart:"))
@safe_handler
def cb_room_start(call):
    room_id = call.data.split(":", 1)[1]
    room = rooms.get(room_id)
    if not room:
        bot.answer_callback_query(call.id, "Xona topilmadi")
        return
    if call.from_user.id != room["creator_id"]:
        bot.answer_callback_query(call.id, "Faqat xona egasi boshlay oladi")
        return
    if room["state"] != "waiting":
        bot.answer_callback_query(call.id, "Allaqachon boshlangan")
        return
    bot.answer_callback_query(call.id)
    room["state"] = "running"
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    # time.sleep ishlatiladi — pollingni bloklamaslik uchun alohida threadda
    threading.Thread(target=start_room_countdown, args=(room_id,), daemon=True).start()


def start_room_countdown(room_id):
    room = rooms[room_id]
    countdown_msg_ids = {}
    for uid, p in room["participants"].items():
        try:
            msg = bot.send_message(p["chat_id"], "Tayyor bo'ling! 🔥")
            countdown_msg_ids[uid] = msg.message_id
        except Exception:
            log.exception("countdown boshlanmadi: %s", uid)

    for step in ["3️⃣", "2️⃣", "1️⃣", "🚀 START!"]:
        time.sleep(1)
        for uid, p in room["participants"].items():
            if uid not in countdown_msg_ids:
                continue
            try:
                bot.edit_message_text(step, p["chat_id"], countdown_msg_ids[uid])
            except Exception:
                pass

    for uid, p in room["participants"].items():
        chat_id = p["chat_id"]
        s = get_session(chat_id)
        s["level"] = room["level"]
        s["code"] = room["code"]
        s["questions"] = room["questions"]
        s["writing_tasks"] = room["writing_tasks"]
        s["answer_key"] = room["answer_key"]
        s["order"] = room["order"][:]
        s["room_id"] = room_id
        start_quiz(chat_id)


def room_standings(room):
    def sort_key(p):
        if not p["finished"]:
            return (1, 0, 0)
        return (0, -(p["score"] or 0), p["duration"] or 9e9)
    return sorted(room["participants"].values(), key=sort_key)


def format_standings(standings):
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, p in enumerate(standings):
        prefix = medals[i] if i < 3 and p["finished"] else f"{i + 1}."
        if p["finished"]:
            score_txt = f"{p['score']}/{p['graded']}" if p.get("graded") else "—"
            lines.append(f"{prefix} {p['name']}: {score_txt}, ⏱ {fmt_duration(p['duration'])}")
        else:
            lines.append(f"{prefix} {p['name']}: hali yechyapti...")
    return "\n".join(lines)


def report_room_finish(room_id, chat_id, correct, graded, duration):
    room = rooms.get(room_id)
    if not room:
        return
    uid = next((u for u, p in room["participants"].items() if p["chat_id"] == chat_id), None)
    if uid is None:
        return
    p = room["participants"][uid]
    p.update(finished=True, score=correct, graded=graded, duration=duration)

    standings = room_standings(room)
    all_done = all(pp["finished"] for pp in room["participants"].values())
    header = "🎉 Barcha ishtirokchilar tugatdi! Yakuniy reyting:" if all_done else f"🏁 {p['name']} testni tugatdi! Joriy reyting:"
    text = header + "\n\n" + format_standings(standings)
    for pp in room["participants"].values():
        try:
            bot.send_message(pp["chat_id"], text)
        except Exception:
            pass
    if all_done:
        room["state"] = "finished"


# --------------------------------------------------------------------
# Admin panel callbacks
# --------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("delexam:"))
@safe_handler
def cb_delete_exam(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    _, level, code = call.data.split(":", 2)
    db.delete_exam(level, code)
    bot.answer_callback_query(call.id, "O'chirildi")
    send_admin_panel(call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("attachkey:"))
@safe_handler
def cb_attach_key(call):
    chat_id = call.message.chat.id
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q")
        return
    _, level, code = call.data.split(":", 2)
    answers = pending_answer_keys.pop(chat_id, None)
    if not answers:
        bot.answer_callback_query(call.id, "Muddati o'tgan, qayta yuboring")
        return
    db.attach_answer_key(level, code, answers)
    bot.answer_callback_query(call.id, "Biriktirildi")
    bot.edit_message_text(
        f"✅ Javoblar kaliti HSK{level} {code} testiga biriktirildi.",
        chat_id, call.message.message_id,
    )


# --------------------------------------------------------------------
# Quiz flow (shared by solo play and room participants)
# --------------------------------------------------------------------
def start_quiz(chat_id):
    s = get_session(chat_id)
    s["state"] = "quiz"
    s["pos"] = 0
    s["user_answers"] = {}
    s["start_time"] = time.time()
    msg = bot.send_message(chat_id, "Test boshlanmoqda...")
    s["quiz_message_id"] = msg.message_id
    send_question(chat_id)


def question_by_number(questions, number):
    for q in questions:
        if q["number"] == number:
            return q
    return None


def format_question(q, pos, total):
    lines = [f"📝 Savol {pos + 1}/{total}  (№{q['number']}, {q['section']})", ""]
    if q["passage"]:
        lines.append(q["passage"])
        lines.append("")
    if q["stem"]:
        lines.append(q["stem"])
        lines.append("")
    for letter in "ABCD":
        lines.append(f"{letter}. {q['options'][letter]}")
    return "\n".join(lines)[:4000]


def send_question(chat_id):
    s = get_session(chat_id)
    total = len(s["order"])

    if s["pos"] >= total:
        finish_quiz(chat_id)
        return

    number = s["order"][s["pos"]]
    q = question_by_number(s["questions"], number)
    text = format_question(q, s["pos"], total)

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
@safe_handler
def cb_answer(call):
    chat_id = call.message.chat.id
    s = get_session(chat_id)
    if s["state"] != "quiz":
        bot.answer_callback_query(call.id)
        return

    _, number_s, letter = call.data.split(":")
    number = int(number_s)

    if s["pos"] >= len(s["order"]) or s["order"][s["pos"]] != number:
        bot.answer_callback_query(call.id, "Bu savol allaqachon o'tildi")
        return

    s["user_answers"][number] = letter
    bot.answer_callback_query(call.id, text=f"Tanlandi: {letter}")
    s["pos"] += 1
    send_question(chat_id)


def finish_quiz(chat_id):
    s = get_session(chat_id)
    answer_key = s["answer_key"]
    duration = time.time() - (s.get("start_time") or time.time())
    s["state"] = "idle"

    lines = ["🏁 Test yakunlandi!", f"⏱ Vaqt: {fmt_duration(duration)}\n"]

    correct = graded = None
    if answer_key:
        correct = graded = 0
        detail = []
        for number in s["order"]:
            user_letter = s["user_answers"].get(number)
            correct_letter = answer_key.get(number)
            if correct_letter is None:
                continue
            graded += 1
            ok = user_letter == correct_letter
            correct += ok
            mark = "✅" if ok else "❌"
            detail.append(
                f"{mark} №{number}: sizniki {user_letter or '—'}"
                + ("" if ok else f", to'g'risi {correct_letter}")
            )
        pct = round(100 * correct / graded) if graded else 0
        lines.append(f"Natija: {correct}/{graded} to'g'ri ({pct}%)\n")
        lines.append("\n".join(detail))
    else:
        lines.append("Javoblar kaliti berilmagani uchun ball hisoblanmadi. Tanlovlaringiz:\n")
        lines.append("\n".join(f"№{n}: {s['user_answers'].get(n, '—')}" for n in s["order"]))

    if s["writing_tasks"]:
        lines.append(f"\n\n✍️ Yozma bo'limda {len(s['writing_tasks'])} ta topshiriq bor edi.")

    full_text = "\n".join(lines)
    for i in range(0, len(full_text), 4000):
        bot.send_message(chat_id, full_text[i:i + 4000])

    room_id = s.get("room_id")
    s["room_id"] = None
    if room_id:
        report_room_finish(room_id, chat_id, correct, graded, duration)
    else:
        bot.send_message(chat_id, "Yangi test uchun /tests yoki /start yozing.")


if __name__ == "__main__":
    bot.remove_webhook()
    log.info("Bot polling boshlandi (admin_id=%s)", ADMIN_ID)
    bot.infinity_polling()
