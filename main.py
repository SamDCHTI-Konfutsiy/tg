import os
import time
import random
import string
import logging
import threading
import urllib.parse
from datetime import date

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

sessions = {}            # chat_id -> quiz session (private chats)
rooms = {}               # room_id -> private-link room
group_quizzes = {}       # group_chat_id -> group quiz state
pending_answer_keys = {} # admin javob kaliti biriktirish uchun
admin_state = {}         # admin kutish holati: {"await": "..."}
user_state = {}          # user kutish holati (support xabari va h.k.)
_bot_username_cache = None

SEC_LABEL = {"听力": "🎧 Tinglash", "阅读": "📖 O'qish", "书写": "✍️ Yozish"}


# ============================ helpers ============================
def new_session():
    return {"state": "idle", "level": None, "code": None, "questions": [],
            "answer_key": None, "order": [], "pos": 0, "answers": {},
            "msg_id": None, "room_id": None, "start": None, "deadline": None,
            "mistake_mode": False, "cost_paid": 0}


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
                bot.send_message(chat_id, "⚠️ Kutilmagan xatolik. /start yozib qayta urinib ko'ring.")
            except Exception:
                pass
    return wrapper


def is_admin(uid):
    return uid == ADMIN_ID


def fmt_dur(sec):
    sec = max(0, int(sec or 0)); m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def bot_username():
    global _bot_username_cache
    if _bot_username_cache is None:
        _bot_username_cache = bot.get_me().username
    return _bot_username_cache


def ensure_user(msg_or_user):
    u = msg_or_user.from_user if hasattr(msg_or_user, "from_user") else msg_or_user
    name = u.first_name or u.username or "Foydalanuvchi"
    return db.get_user(u.id, name)


def validate_questions(questions):
    """Shubhali savollarni ajratadi: bo'sh yoki g'ayrioddiy uzun variantlar."""
    good, bad = [], []
    for q in questions:
        opts = q.get("options", {})
        suspicious = (
            any(not (opts.get(L) or "").strip() for L in "ABCD")
            or any(len(opts.get(L, "")) > 120 for L in "ABCD")
            or len(q.get("stem", "")) > 2000
        )
        (bad if suspicious else good).append(q)
    return good, bad


def notify_admin(text, kb=None):
    try:
        bot.send_message(ADMIN_ID, text, reply_markup=kb)
    except Exception:
        log.exception("adminni xabardor qilib bo'lmadi")


# ============================ main menu ============================
def main_menu_kb(uid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📚 Testlar", callback_data="menu:tests"))
    kb.add(types.InlineKeyboardButton("📕 Xatolar daftari", callback_data="menu:mistakes"),
           types.InlineKeyboardButton("👤 Profil", callback_data="menu:profile"))
    kb.add(types.InlineKeyboardButton("🆘 Adminga murojaat", callback_data="menu:support"))
    if is_admin(uid):
        kb.add(types.InlineKeyboardButton("🛠 Admin panel", callback_data="adm:home"))
    return kb


@bot.message_handler(commands=["start"])
@safe_handler
def cmd_start(message):
    user, created = ensure_user(message)
    s = db.get_settings()
    parts = message.text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else None

    if payload and payload.startswith("room_"):
        join_room(message, payload[5:]); return

    if payload and payload.startswith("ref_") and created:
        try:
            ref_id = int(payload[4:])
            if ref_id != message.from_user.id:
                db.update_user(message.from_user.id, referred_by=ref_id)
                ref_u, _ = db.get_user(ref_id)
                db.update_user(ref_id, ref_count=ref_u.get("ref_count", 0) + 1)
                db.add_points(ref_id, s["referral_points"])
                try:
                    bot.send_message(ref_id, f"🎉 Sizning havolangiz orqali yangi foydalanuvchi qo'shildi! +{s['referral_points']} ball")
                except Exception:
                    pass
        except ValueError:
            pass

    daily_note = ""
    if db.claim_daily(message.from_user.id, date.today().isoformat(), s["daily_points"]):
        daily_note = f"\n🎁 Kunlik bonus: +{s['daily_points']} ball!"

    if message.chat.type in ("group", "supergroup"):
        bot.reply_to(message, "Guruh rejimi: /tests yozing va testni tanlang — hamma birga yechadi!")
        return

    u, _ = db.get_user(message.from_user.id)
    sessions[message.chat.id] = new_session()
    bot.send_message(
        message.chat.id,
        f"Salom, {user['name']}! 👋{daily_note}\n\n"
        f"💰 Ballingiz: {u['balance']}\n\n"
        f"📄 HSK imtihon PDF yuboring yoki quyidagi menyudan tanlang:",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@bot.message_handler(commands=["tests"])
@safe_handler
def cmd_tests(message):
    ensure_user(message)
    show_levels(message.chat.id, group=message.chat.type in ("group", "supergroup"))


@bot.callback_query_handler(func=lambda c: c.data == "menu:tests")
@safe_handler
def cb_menu_tests(call):
    bot.answer_callback_query(call.id)
    show_levels(call.message.chat.id, message_id=call.message.message_id)


def show_levels(chat_id, message_id=None, group=False):
    levels = sorted({e["level"] for e in db.list_exams()})
    if not levels:
        txt = "Kutubxonada hali testlar yo'q."
        if message_id: bot.edit_message_text(txt, chat_id, message_id)
        else: bot.send_message(chat_id, txt)
        return
    kb = types.InlineKeyboardMarkup(row_width=3)
    prefix = "glvl" if group else "lvl"
    kb.add(*[types.InlineKeyboardButton(f"HSK{l}", callback_data=f"{prefix}:{l}") for l in levels])
    if not group:
        kb.add(types.InlineKeyboardButton("⬅️ Menyu", callback_data="menu:home"))
    txt = "Qaysi HSK darajasi?"
    if message_id:
        bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb)
    else:
        bot.send_message(chat_id, txt, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "menu:home")
@safe_handler
def cb_menu_home(call):
    bot.answer_callback_query(call.id)
    u, _ = db.get_user(call.from_user.id)
    bot.edit_message_text(
        f"💰 Ballingiz: {u['balance']}\n\nMenyudan tanlang:",
        call.message.chat.id, call.message.message_id,
        reply_markup=main_menu_kb(call.from_user.id))


@bot.callback_query_handler(func=lambda c: c.data == "menu:profile")
@safe_handler
def cb_profile(call):
    bot.answer_callback_query(call.id)
    u, _ = db.get_user(call.from_user.id)
    ref_link = f"https://t.me/{bot_username()}?start=ref_{call.from_user.id}"
    s = db.get_settings()
    kb = types.InlineKeyboardMarkup()
    share = urllib.parse.quote(f"HSK testlarini birga yechamiz! {ref_link}")
    kb.add(types.InlineKeyboardButton("📤 Havolani ulashish", url=f"https://t.me/share/url?url={share}"))
    kb.add(types.InlineKeyboardButton("⬅️ Menyu", callback_data="menu:home"))
    bot.edit_message_text(
        f"👤 {u['name']}\n\n💰 Ball: {u['balance']}\n"
        f"✅ Yechilgan testlar: {u.get('tests_done', 0)}\n"
        f"👥 Takliflaringiz: {u.get('ref_count', 0)} ta (har biri +{s['referral_points']} ball)\n\n"
        f"🔗 Referal havolangiz:\n{ref_link}",
        call.message.chat.id, call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "menu:support")
@safe_handler
def cb_support(call):
    bot.answer_callback_query(call.id)
    user_state[call.from_user.id] = {"await": "support"}
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Bekor qilish", callback_data="menu:home"))
    bot.edit_message_text(
        "🆘 Muammoingiz yoki taklifingizni yozing — adminga yetkazaman:",
        call.message.chat.id, call.message.message_id, reply_markup=kb)


# ============================ documents ============================
@bot.message_handler(content_types=["document"])
@safe_handler
def handle_document(message):
    ensure_user(message)
    chat_id = message.chat.id
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        bot.reply_to(message, "Iltimos, faqat PDF fayl yuboring."); return

    file_info = bot.get_file(doc.file_id)
    raw = bot.download_file(file_info.file_path)
    tmp = f"/tmp/{doc.file_unique_id}.pdf"
    with open(tmp, "wb") as f:
        f.write(raw)

    wait = bot.reply_to(message, "📄 Fayl tahlil qilinmoqda...")
    try:
        exam = parse_exam(tmp)
    except Exception:
        log.exception("exam parse failed")
        exam = {"questions": [], "writing_tasks": [], "level": None, "code": None}

    if len(exam["questions"]) >= 5:
        process_exam_upload(message, wait, exam); return

    try:
        answers = parse_answer_key(tmp)
    except Exception:
        answers = {}
    if answers:
        process_answer_key_upload(message, wait, answers); return

    bot.edit_message_text("Bu fayldan na savollar, na javoblar kaliti topilmadi.",
                          chat_id, wait.message_id)


def process_exam_upload(message, wait, exam):
    chat_id = message.chat.id
    uid = message.from_user.id
    level = db.normalize_level(exam["level"])
    code = exam["code"] or f"NOCODE-{int(time.time())%100000}"
    good, bad = validate_questions(exam["questions"])

    admin_up = is_admin(uid)
    record, replaced = db.upsert_exam(level, code, good, exam["writing_tasks"],
                                      uploaded_by=uid, approved=admin_up)

    s = get_session(chat_id)
    s.update(level=level, code=code, questions=record["questions"],
             answer_key=record.get("answer_key"), state="await_answer_key")

    text = (f"✅ Tahlil qilindi!\n\nHSK daraja: {level}\nKod: {code}\n"
            f"Yaxshi savollar: {len(good)} ta\n")
    if bad:
        text += f"⚠️ Shubhali savollar: {len(bad)} ta — chiqarib tashlandi, admin xabardor qilindi.\n"
        notify_admin(f"⚠️ HSK{level} {code}: {len(bad)} ta shubhali savol "
                     f"(№: {', '.join(str(q['number']) for q in bad[:15])}). Yuklagan: {uid}")
    if admin_up:
        text += "🗄 Kutubxonaga saqlandi." if replaced else "🗄 Kutubxonada yaxshiroq versiya bor edi."
    else:
        text += ("📥 Test admin tasdiqlashi uchun yuborildi — tasdiqlangach umumiy "
                 "kutubxonada chiqadi. O'zingiz hozir yechishingiz mumkin.")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"appr:{level}:{code}"),
               types.InlineKeyboardButton("❌ Rad etish", callback_data=f"rej:{level}:{code}"))
        notify_admin(f"📥 Yangi test yuklandi: HSK{level} {code} ({len(good)} ta savol)\n"
                     f"Yuklagan: {message.from_user.first_name} (id {uid})", kb)

    if s["answer_key"]:
        text += "\n✅ Javoblar kaliti mavjud."
    else:
        text += "\nJavoblar kaliti PDF bo'lsa hozir yuboring, bo'lmasa davom eting."
    kb2 = types.InlineKeyboardMarkup()
    kb2.add(types.InlineKeyboardButton("➡️ Davom etish", callback_data=f"pick:{level}:{code}"))
    bot.edit_message_text(text, chat_id, wait.message_id, reply_markup=kb2)


def process_answer_key_upload(message, wait, answers):
    chat_id = message.chat.id
    s = get_session(chat_id)
    if s["state"] == "await_answer_key" and s.get("code"):
        s["answer_key"] = answers
        db.attach_answer_key(s["level"], s["code"], answers)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➡️ Davom etish", callback_data=f"pick:{s['level']}:{s['code']}"))
        bot.edit_message_text(f"✅ Javoblar kaliti o'qildi ({len(answers)} ta) va saqlandi.",
                              chat_id, wait.message_id, reply_markup=kb)
        return
    if is_admin(message.from_user.id):
        missing = [e for e in db.list_exams() + db.list_pending() if not e.get("answer_key")]
        if not missing:
            bot.edit_message_text("Javoblar o'qildi, lekin biriktiriladigan test yo'q.",
                                  chat_id, wait.message_id); return
        pending_answer_keys[chat_id] = answers
        kb = types.InlineKeyboardMarkup()
        for e in missing[:30]:
            kb.add(types.InlineKeyboardButton(f"HSK{e['level']} {e['code']}",
                                              callback_data=f"attachkey:{e['level']}:{e['code']}"))
        bot.edit_message_text(f"✅ Javoblar o'qildi ({len(answers)} ta). Qaysi testga?",
                              chat_id, wait.message_id, reply_markup=kb)
        return
    bot.edit_message_text("Bu javoblar fayli. Avval imtihon PDF yuboring.", chat_id, wait.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("attachkey:"))
@safe_handler
def cb_attach_key(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q"); return
    _, level, code = call.data.split(":", 2)
    answers = pending_answer_keys.pop(call.message.chat.id, None)
    if not answers:
        bot.answer_callback_query(call.id, "Muddati o'tgan"); return
    db.attach_answer_key(level, code, answers)
    bot.answer_callback_query(call.id, "Biriktirildi")
    bot.edit_message_text(f"✅ Kalit HSK{level} {code} ga biriktirildi.",
                          call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("appr:") or c.data.startswith("rej:"))
@safe_handler
def cb_approve(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q"); return
    action, level, code = call.data.split(":", 2)
    if action == "appr":
        rec = db.approve_pending(level, code)
        bot.answer_callback_query(call.id, "Tasdiqlandi")
        bot.edit_message_text(f"✅ HSK{level} {code} kutubxonaga qo'shildi "
                              f"({len(rec['questions']) if rec else 0} ta savol).",
                              call.message.chat.id, call.message.message_id)
    else:
        db.reject_pending(level, code)
        bot.answer_callback_query(call.id, "Rad etildi")
        bot.edit_message_text(f"❌ HSK{level} {code} rad etildi.",
                              call.message.chat.id, call.message.message_id)


# ============================ test tanlash ============================
@bot.callback_query_handler(func=lambda c: c.data.startswith("lvl:"))
@safe_handler
def cb_level(call):
    level = call.data.split(":", 1)[1]
    exams = db.list_exams(level)
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    for e in exams:
        mark = "✅" if e.get("answer_key") else ""
        kb.add(types.InlineKeyboardButton(f"{e['code']} ({len(e['questions'])} ta) {mark}",
                                          callback_data=f"pick:{e['level']}:{e['code']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="menu:tests"))
    bot.edit_message_text(f"HSK{level} testlari:", call.message.chat.id,
                          call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("pick:"))
@safe_handler
def cb_pick(call):
    _, level, code = call.data.split(":", 2)
    record = db.get_exam(level, code, include_pending=True)
    if not record:
        bot.answer_callback_query(call.id, "Topilmadi"); return
    bot.answer_callback_query(call.id)
    st = db.get_settings()
    cost = st["cost"].get(level, 10)
    tmin = st["time_min"].get(level, 60)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🙋 Yolg'iz", callback_data=f"secsel:{level}:{code}"))
    kb.add(types.InlineKeyboardButton("👥 Do'stlar bilan (xona)", callback_data=f"room:{level}:{code}"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data=f"lvl:{level}"))
    bot.edit_message_text(
        f"HSK{level} {code} — {len(record['questions'])} ta savol\n"
        f"💰 Narxi: {cost} ball  |  ⏱ Vaqt: {tmin} daqiqa\n\nQanday o'ynaymiz?",
        call.message.chat.id, call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("secsel:"))
@safe_handler
def cb_section_select(call):
    _, level, code = call.data.split(":", 2)
    record = db.get_exam(level, code, include_pending=True)
    if not record:
        bot.answer_callback_query(call.id, "Topilmadi"); return
    bot.answer_callback_query(call.id)
    secs = sorted({q["section"] for q in record["questions"] if q.get("section")})
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📚 Hammasi", callback_data=f"solo:{level}:{code}:all"))
    for sec in secs:
        kb.add(types.InlineKeyboardButton(SEC_LABEL.get(sec, sec),
                                          callback_data=f"solo:{level}:{code}:{sec}"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data=f"pick:{level}:{code}"))
    bot.edit_message_text("Qaysi bo'limni yechasiz?", call.message.chat.id,
                          call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("solo:"))
@safe_handler
def cb_solo(call):
    _, level, code, sec = call.data.split(":", 3)
    record = db.get_exam(level, code, include_pending=True)
    chat_id = call.message.chat.id
    uid = call.from_user.id
    if not record:
        bot.answer_callback_query(call.id, "Topilmadi"); return

    st = db.get_settings()
    cost = st["cost"].get(level, 10)
    u, _ = db.get_user(uid)
    if u["balance"] < cost:
        bot.answer_callback_query(call.id, f"Ball yetarli emas ({u['balance']}/{cost})", show_alert=True)
        return
    db.add_points(uid, -cost)
    bot.answer_callback_query(call.id, f"-{cost} ball")

    qs = record["questions"]
    if sec != "all":
        qs = [q for q in qs if q.get("section") == sec]
    s = get_session(chat_id)
    s.update(level=level, code=code, questions=qs,
             answer_key=record.get("answer_key"),
             order=[q["number"] for q in qs], room_id=None,
             mistake_mode=False, cost_paid=cost)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    start_quiz(chat_id, uid, time_limit_min=st["time_min"].get(level, 60))


# ============================ quiz (nav bilan) ============================
def start_quiz(chat_id, user_id, time_limit_min=60):
    s = get_session(chat_id)
    s.update(state="quiz", pos=0, answers={}, start=time.time(),
             deadline=time.time() + time_limit_min * 60, quiz_user=user_id)
    msg = bot.send_message(chat_id, "Test boshlanmoqda...")
    s["msg_id"] = msg.message_id
    render_question(chat_id)
    threading.Timer(time_limit_min * 60 + 2, _auto_finish, args=(chat_id, s["start"])).start()


def _auto_finish(chat_id, start_marker):
    s = sessions.get(chat_id)
    if s and s["state"] == "quiz" and s["start"] == start_marker:
        try:
            bot.send_message(chat_id, "⏰ Vaqt tugadi!")
            finish_quiz(chat_id)
        except Exception:
            log.exception("auto finish xatosi")


def q_by_num(questions, number):
    for q in questions:
        if q["number"] == number:
            return q
    return None


def render_question(chat_id):
    s = get_session(chat_id)
    total = len(s["order"])
    if total == 0:
        finish_quiz(chat_id); return
    s["pos"] = max(0, min(s["pos"], total - 1))
    number = s["order"][s["pos"]]
    q = q_by_num(s["questions"], number)
    remain = fmt_dur(s["deadline"] - time.time()) if s.get("deadline") else "--"
    chosen = s["answers"].get(number)
    answered = len(s["answers"])

    lines = [f"📝 {s['pos'] + 1}/{total}  (№{number}, {q.get('section','')})  "
             f"⏱ {remain}  ✅ {answered}/{total}", ""]
    if q.get("passage"):
        lines += [q["passage"], ""]
    if q.get("stem"):
        lines += [q["stem"], ""]
    for L in "ABCD":
        lines.append(f"{L}. {q['options'][L]}")
    text = "\n".join(lines)[:4000]

    kb = types.InlineKeyboardMarkup(row_width=4)
    kb.add(*[types.InlineKeyboardButton(("🔘 " if chosen == L else "") + L,
                                        callback_data=f"ans:{number}:{L}") for L in "ABCD"])
    kb.add(types.InlineKeyboardButton("⬅️", callback_data="nav:prev"),
           types.InlineKeyboardButton(f"{s['pos']+1}/{total}", callback_data="nav:none"),
           types.InlineKeyboardButton("➡️", callback_data="nav:next"))
    kb.add(types.InlineKeyboardButton("🏁 Yakunlash", callback_data="nav:finish"))
    try:
        bot.edit_message_text(text, chat_id, s["msg_id"], reply_markup=kb)
    except Exception:
        msg = bot.send_message(chat_id, text, reply_markup=kb)
        s["msg_id"] = msg.message_id


@bot.callback_query_handler(func=lambda c: c.data.startswith("nav:"))
@safe_handler
def cb_nav(call):
    chat_id = call.message.chat.id
    s = get_session(chat_id)
    if s["state"] != "quiz":
        bot.answer_callback_query(call.id); return
    action = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)
    if action == "prev":
        s["pos"] -= 1; render_question(chat_id)
    elif action == "next":
        s["pos"] += 1; render_question(chat_id)
    elif action == "finish":
        finish_quiz(chat_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ans:"))
@safe_handler
def cb_answer(call):
    chat_id = call.message.chat.id
    if chat_id in group_quizzes:
        group_answer(call); return
    s = get_session(chat_id)
    if s["state"] != "quiz":
        bot.answer_callback_query(call.id); return
    if s.get("deadline") and time.time() > s["deadline"]:
        bot.answer_callback_query(call.id, "⏰ Vaqt tugadi")
        finish_quiz(chat_id); return
    _, num_s, L = call.data.split(":")
    number = int(num_s)
    s["answers"][number] = L
    bot.answer_callback_query(call.id, f"Tanlandi: {L}")
    if s["pos"] < len(s["order"]) - 1:
        s["pos"] += 1
    render_question(chat_id)


def finish_quiz(chat_id):
    s = get_session(chat_id)
    if s["state"] != "quiz":
        return
    s["state"] = "idle"
    uid = s.get("quiz_user")
    duration = time.time() - (s.get("start") or time.time())
    key = s["answer_key"]
    st = db.get_settings()

    lines = ["🏁 Test yakunlandi!", f"⏱ Vaqt: {fmt_dur(duration)}\n"]
    correct = graded = None
    wrong_items = []
    if key:
        correct = graded = 0
        detail = []
        for n in s["order"]:
            ul, cl = s["answers"].get(n), key.get(n)
            if cl is None:
                continue
            graded += 1
            ok = ul == cl
            correct += ok
            detail.append(f"{'✅' if ok else '❌'} №{n}: {ul or '—'}" + ("" if ok else f" → {cl}"))
            if not ok and not s.get("mistake_mode"):
                wrong_items.append({"level": s["level"], "code": s["code"], "number": n})
        pct = round(100 * correct / graded) if graded else 0
        lines.append(f"Natija: {correct}/{graded} ({pct}%)\n")
        lines.append("\n".join(detail))

        if uid:
            if wrong_items:
                db.add_mistakes(uid, wrong_items)
            if s.get("mistake_mode"):
                fixed = [{"level": s["level"], "code": s["code"], "number": n}
                         for n in s["order"] if s["answers"].get(n) == key.get(n)]
                if fixed:
                    db.remove_mistakes(uid, fixed)
                    lines.append(f"\n📕 {len(fixed)} ta xato daftardan o'chirildi!")
            exam_key = f"{s['level']}::{s['code']}"
            if (not s.get("mistake_mode") and pct >= st["bonus_threshold"]
                    and db.mark_bonus_awarded(uid, exam_key)):
                db.add_points(uid, st["bonus_points"])
                lines.append(f"\n🎉 {st['bonus_threshold']}%+ aniqlik! Bonus: +{st['bonus_points']} ball "
                             f"(bu test uchun bir martalik)")
    else:
        lines.append("Javoblar kaliti yo'q — ball hisoblanmadi.")

    if uid:
        u, _ = db.get_user(uid)
        db.update_user(uid, tests_done=u.get("tests_done", 0) + 1)
        u2, _ = db.get_user(uid)
        lines.append(f"\n💰 Ballingiz: {u2['balance']}")

    full = "\n".join(lines)
    for i in range(0, len(full), 4000):
        bot.send_message(chat_id, full[i:i+4000])

    if correct is not None:
        res_text = (f"Men HSK{s['level']} {s['code']} testida {correct}/{graded} "
                    f"natija qildim ({fmt_dur(duration)})! Sen ham sinab ko'r: "
                    f"https://t.me/{bot_username()}?start=ref_{uid}")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            "📤 Natijani ulashish",
            url=f"https://t.me/share/url?url={urllib.parse.quote(res_text)}"))
        kb.add(types.InlineKeyboardButton("📚 Yana test", callback_data="menu:tests"))
        bot.send_message(chat_id, "Natijangizni do'stlaringizga ulashing:", reply_markup=kb)

    room_id = s.get("room_id"); s["room_id"] = None
    if room_id:
        report_room_finish(room_id, chat_id, correct, graded, duration)


# ============================ xatolar daftari ============================
@bot.callback_query_handler(func=lambda c: c.data == "menu:mistakes")
@safe_handler
def cb_mistakes(call):
    bot.answer_callback_query(call.id)
    u, _ = db.get_user(call.from_user.id)
    ms = u.get("mistakes", [])
    kb = types.InlineKeyboardMarkup()
    if ms:
        kb.add(types.InlineKeyboardButton(f"▶️ Mashq qilish ({len(ms)} ta)", callback_data="mist:start"))
    kb.add(types.InlineKeyboardButton("⬅️ Menyu", callback_data="menu:home"))
    bot.edit_message_text(
        f"📕 Xatolar daftari: {len(ms)} ta savol.\n"
        f"To'g'ri yechsangiz — daftardan o'chadi.",
        call.message.chat.id, call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "mist:start")
@safe_handler
def cb_mist_start(call):
    chat_id = call.message.chat.id
    uid = call.from_user.id
    u, _ = db.get_user(uid)
    ms = u.get("mistakes", [])
    if not ms:
        bot.answer_callback_query(call.id, "Daftar bo'sh"); return
    questions, answer_key = [], {}
    for m in ms[:50]:
        rec = db.get_exam(m["level"], m["code"], include_pending=True)
        if not rec:
            continue
        q = q_by_num(rec["questions"], m["number"])
        if q:
            questions.append(q)
            if rec.get("answer_key"):
                answer_key[m["number"]] = rec["answer_key"].get(m["number"])
    if not questions:
        bot.answer_callback_query(call.id, "Savollar topilmadi"); return
    bot.answer_callback_query(call.id)
    s = get_session(chat_id)
    lvl0, code0 = ms[0]["level"], ms[0]["code"]
    s.update(level=lvl0, code=code0, questions=questions,
             answer_key={k: v for k, v in answer_key.items() if v},
             order=[q["number"] for q in questions], room_id=None,
             mistake_mode=True, cost_paid=0)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    start_quiz(chat_id, uid, time_limit_min=30)


# ============================ xonalar (private link) ============================
def gen_room_id():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def room_link(rid):
    return f"https://t.me/{bot_username()}?start=room_{rid}"


@bot.callback_query_handler(func=lambda c: c.data.startswith("room:"))
@safe_handler
def cb_room(call):
    _, level, code = call.data.split(":", 2)
    record = db.get_exam(level, code, include_pending=True)
    if not record:
        bot.answer_callback_query(call.id, "Topilmadi"); return
    rid = gen_room_id()
    while rid in rooms:
        rid = gen_room_id()
    rooms[rid] = {"id": rid, "level": level, "code": code,
                  "questions": record["questions"],
                  "answer_key": record.get("answer_key"),
                  "order": [q["number"] for q in record["questions"]],
                  "creator": call.from_user.id, "lobby_chat": call.message.chat.id,
                  "lobby_msg": None, "state": "waiting", "parts": {}}
    rooms[rid]["parts"][call.from_user.id] = {
        "chat_id": call.message.chat.id,
        "name": call.from_user.first_name or "Foydalanuvchi",
        "finished": False, "score": None, "graded": None, "duration": None}
    bot.answer_callback_query(call.id)
    render_lobby(rid, call.message.message_id)


def render_lobby(rid, message_id=None):
    room = rooms[rid]
    names = ", ".join(p["name"] for p in room["parts"].values())
    st = db.get_settings()
    text = (f"👥 Xona: HSK{room['level']} {room['code']} "
            f"({len(room['questions'])} ta savol, ⏱ {st['time_min'].get(room['level'],60)} daqiqa)\n\n"
            f"Havola:\n{room_link(rid)}\n\nQo'shilganlar ({len(room['parts'])}): {names}")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 Yangilash", callback_data=f"rref:{rid}"),
           types.InlineKeyboardButton("🚀 Boshlash", callback_data=f"rstart:{rid}"))
    if message_id:
        try:
            bot.edit_message_text(text, room["lobby_chat"], message_id, reply_markup=kb)
            room["lobby_msg"] = message_id; return
        except Exception:
            pass
    msg = bot.send_message(room["lobby_chat"], text, reply_markup=kb)
    room["lobby_msg"] = msg.message_id


def join_room(message, rid):
    room = rooms.get(rid)
    if not room:
        bot.reply_to(message, "Xona topilmadi yoki muddati o'tgan."); return
    if room["state"] != "waiting":
        bot.reply_to(message, "Bu xonada test allaqachon boshlangan."); return
    ensure_user(message)
    room["parts"][message.from_user.id] = {
        "chat_id": message.chat.id,
        "name": message.from_user.first_name or "Foydalanuvchi",
        "finished": False, "score": None, "graded": None, "duration": None}
    bot.reply_to(message, f"✅ Xonaga qo'shildingiz! HSK{room['level']} {room['code']}. "
                          f"Ega boshlashini kuting...")
    if room["lobby_msg"]:
        render_lobby(rid, room["lobby_msg"])


@bot.callback_query_handler(func=lambda c: c.data.startswith("rref:"))
@safe_handler
def cb_rref(call):
    rid = call.data.split(":", 1)[1]
    if rid not in rooms:
        bot.answer_callback_query(call.id, "Topilmadi"); return
    bot.answer_callback_query(call.id)
    render_lobby(rid, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("rstart:"))
@safe_handler
def cb_rstart(call):
    rid = call.data.split(":", 1)[1]
    room = rooms.get(rid)
    if not room:
        bot.answer_callback_query(call.id, "Topilmadi"); return
    if call.from_user.id != room["creator"]:
        bot.answer_callback_query(call.id, "Faqat xona egasi"); return
    if room["state"] != "waiting":
        bot.answer_callback_query(call.id, "Boshlangan"); return
    st = db.get_settings()
    cost = st["cost"].get(room["level"], 10)
    poor = []
    for uid2 in room["parts"]:
        u, _ = db.get_user(uid2)
        if u["balance"] < cost:
            poor.append(u["name"])
    if poor:
        bot.answer_callback_query(call.id, f"Ball yetmaydi: {', '.join(poor)}", show_alert=True)
        return
    for uid2 in room["parts"]:
        db.add_points(uid2, -cost)
    bot.answer_callback_query(call.id)
    room["state"] = "running"
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    threading.Thread(target=room_countdown, args=(rid,), daemon=True).start()


def room_countdown(rid):
    room = rooms[rid]
    st = db.get_settings()
    tmin = st["time_min"].get(room["level"], 60)
    cds = {}
    for uid2, p in room["parts"].items():
        try:
            cds[uid2] = bot.send_message(p["chat_id"], "Tayyor bo'ling! 🔥").message_id
        except Exception:
            pass
    for step in ["3️⃣", "2️⃣", "1️⃣", "🚀 START!"]:
        time.sleep(1)
        for uid2, p in room["parts"].items():
            if uid2 in cds:
                try: bot.edit_message_text(step, p["chat_id"], cds[uid2])
                except Exception: pass
    for uid2, p in room["parts"].items():
        cid = p["chat_id"]
        s = get_session(cid)
        s.update(level=room["level"], code=room["code"], questions=room["questions"],
                 answer_key=room["answer_key"], order=room["order"][:],
                 room_id=rid, mistake_mode=False)
        start_quiz(cid, uid2, time_limit_min=tmin)


def report_room_finish(rid, chat_id, correct, graded, duration):
    room = rooms.get(rid)
    if not room:
        return
    uid = next((u for u, p in room["parts"].items() if p["chat_id"] == chat_id), None)
    if uid is None:
        return
    room["parts"][uid].update(finished=True, score=correct, graded=graded, duration=duration)
    standings = sorted(room["parts"].values(),
                       key=lambda p: (not p["finished"], -(p["score"] or 0), p["duration"] or 9e9))
    medals = ["🥇", "🥈", "🥉"]
    rows = []
    for i, p in enumerate(standings):
        pre = medals[i] if i < 3 and p["finished"] else f"{i+1}."
        rows.append(f"{pre} {p['name']}: " + (
            f"{p['score']}/{p['graded']}, ⏱ {fmt_dur(p['duration'])}" if p["finished"]
            else "hali yechyapti..."))
    all_done = all(p["finished"] for p in room["parts"].values())
    head = "🎉 Hamma tugatdi! Yakuniy reyting:" if all_done else "🏁 Joriy reyting:"
    txt = head + "\n\n" + "\n".join(rows)
    for p in room["parts"].values():
        try: bot.send_message(p["chat_id"], txt)
        except Exception: pass
    if all_done:
        room["state"] = "finished"


# ============================ GURUH rejimi ============================
@bot.callback_query_handler(func=lambda c: c.data.startswith("glvl:"))
@safe_handler
def cb_glvl(call):
    level = call.data.split(":", 1)[1]
    exams = db.list_exams(level)
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    for e in exams:
        kb.add(types.InlineKeyboardButton(f"{e['code']} ({len(e['questions'])} ta)",
                                          callback_data=f"gstart:{e['level']}:{e['code']}"))
    bot.edit_message_text(f"HSK{level} — guruh uchun test tanlang:",
                          call.message.chat.id, call.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("gstart:"))
@safe_handler
def cb_gstart(call):
    _, level, code = call.data.split(":", 2)
    gid = call.message.chat.id
    if gid in group_quizzes and group_quizzes[gid]["state"] == "running":
        bot.answer_callback_query(call.id, "Guruhda test ketmoqda"); return
    record = db.get_exam(level, code)
    if not record:
        bot.answer_callback_query(call.id, "Topilmadi"); return
    bot.answer_callback_query(call.id)
    st = db.get_settings()
    tmin = st["time_min"].get(level, 60)
    group_quizzes[gid] = {
        "level": level, "code": code, "questions": record["questions"],
        "answer_key": record.get("answer_key") or {},
        "order": [q["number"] for q in record["questions"]],
        "posted": 0, "answers": {}, "names": {},
        "state": "countdown", "deadline": None, "starter": call.from_user.id}
    try:
        bot.edit_message_text(f"HSK{level} {code} boshlanmoqda! ⏱ {tmin} daqiqa", gid,
                              call.message.message_id)
    except Exception:
        pass
    threading.Thread(target=group_countdown, args=(gid, tmin), daemon=True).start()


def group_countdown(gid, tmin):
    g = group_quizzes[gid]
    msg = bot.send_message(gid, "Tayyor bo'ling! 🔥")
    for step in ["3️⃣", "2️⃣", "1️⃣", "🚀 START!"]:
        time.sleep(1)
        try: bot.edit_message_text(step, gid, msg.message_id)
        except Exception: pass
    g["state"] = "running"
    g["deadline"] = time.time() + tmin * 60
    post_group_question(gid)
    threading.Timer(tmin * 60 + 2, group_finish, args=(gid,)).start()


def post_group_question(gid):
    g = group_quizzes.get(gid)
    if not g or g["state"] != "running" or g["posted"] >= len(g["order"]):
        return
    idx = g["posted"]
    number = g["order"][idx]
    q = q_by_num(g["questions"], number)
    lines = [f"📝 Savol {idx+1}/{len(g['order'])}  (№{number})", ""]
    if q.get("passage"): lines += [q["passage"], ""]
    if q.get("stem"): lines += [q["stem"], ""]
    for L in "ABCD":
        lines.append(f"{L}. {q['options'][L]}")
    kb = types.InlineKeyboardMarkup(row_width=4)
    kb.add(*[types.InlineKeyboardButton(L, callback_data=f"ans:{number}:{L}") for L in "ABCD"])
    bot.send_message(gid, "\n".join(lines)[:4000], reply_markup=kb)
    g["posted"] = idx + 1


def group_answer(call):
    gid = call.message.chat.id
    g = group_quizzes.get(gid)
    if not g or g["state"] != "running":
        bot.answer_callback_query(call.id); return
    if time.time() > (g["deadline"] or 0):
        bot.answer_callback_query(call.id, "⏰ Vaqt tugadi")
        group_finish(gid); return
    _, num_s, L = call.data.split(":")
    number = int(num_s)
    uid = call.from_user.id
    g["names"][uid] = call.from_user.first_name or "Foydalanuvchi"
    ua = g["answers"].setdefault(uid, {})
    if number in ua:
        bot.answer_callback_query(call.id, "Javob berilgan"); return
    ua[number] = L
    bot.answer_callback_query(call.id, f"Qabul: {L}")
    # eski savol saqlanib qoladi — yangi savol PASTIDAN chiqadi
    q_index = g["order"].index(number)
    if q_index == g["posted"] - 1 and g["posted"] < len(g["order"]):
        post_group_question(gid)
    if all(len(a) >= len(g["order"]) for a in g["answers"].values()) and g["answers"]:
        group_finish(gid)


def group_finish(gid):
    g = group_quizzes.get(gid)
    if not g or g["state"] == "finished":
        return
    g["state"] = "finished"
    key = g["answer_key"]
    rows = []
    for uid, ans in g["answers"].items():
        correct = sum(1 for n, L in ans.items() if key.get(n) == L) if key else 0
        rows.append((g["names"].get(uid, "?"), correct, len(ans)))
    rows.sort(key=lambda r: (-r[1], -r[2]))
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏁 Guruh testi yakunlandi!\n"]
    for i, (name, c, a) in enumerate(rows):
        pre = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{pre} {name}: {c} to'g'ri ({a} ta javob)")
    if not rows:
        lines.append("Hech kim javob bermadi 😅")
    bot.send_message(gid, "\n".join(lines))
    group_quizzes.pop(gid, None)


# ============================ ADMIN PANEL ============================
def adm_kb_home():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📚 Testlar", callback_data="adm:tests"),
           types.InlineKeyboardButton("📥 Tasdiqlash", callback_data="adm:pending"))
    kb.add(types.InlineKeyboardButton("⚙️ Sozlamalar", callback_data="adm:settings"),
           types.InlineKeyboardButton("📊 Statistika", callback_data="adm:stats"))
    kb.add(types.InlineKeyboardButton("🏆 Leaderboard", callback_data="adm:top"),
           types.InlineKeyboardButton("📢 Xabar yuborish", callback_data="adm:bcast"))
    return kb


@bot.message_handler(commands=["admin"])
@safe_handler
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return
    bot.send_message(message.chat.id, "🛠 Admin panel", reply_markup=adm_kb_home())


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:"))
@safe_handler
def cb_admin(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q"); return
    action = call.data.split(":", 1)[1]
    chat_id, mid = call.message.chat.id, call.message.message_id
    bot.answer_callback_query(call.id)

    if action == "home":
        bot.edit_message_text("🛠 Admin panel", chat_id, mid, reply_markup=adm_kb_home())

    elif action == "tests":
        exams = db.list_exams()
        kb = types.InlineKeyboardMarkup()
        for e in exams[:40]:
            flag = "✅" if e.get("answer_key") else "➖"
            kb.add(types.InlineKeyboardButton(
                f"{flag} HSK{e['level']} {e['code']} ({len(e['questions'])}) 🗑",
                callback_data=f"delexam:{e['level']}:{e['code']}"))
        kb.add(types.InlineKeyboardButton("⬅️", callback_data="adm:home"))
        bot.edit_message_text(f"📚 Kutubxona: {len(exams)} ta test. 🗑 = o'chirish",
                              chat_id, mid, reply_markup=kb)

    elif action == "pending":
        pend = db.list_pending()
        kb = types.InlineKeyboardMarkup()
        for e in pend[:20]:
            kb.add(types.InlineKeyboardButton(
                f"HSK{e['level']} {e['code']} ({len(e['questions'])}) — id {e['uploaded_by']}",
                callback_data="adm:none"))
            kb.add(types.InlineKeyboardButton("✅", callback_data=f"appr:{e['level']}:{e['code']}"),
                   types.InlineKeyboardButton("❌", callback_data=f"rej:{e['level']}:{e['code']}"))
        kb.add(types.InlineKeyboardButton("⬅️", callback_data="adm:home"))
        bot.edit_message_text(f"📥 Tasdiqlanmagan testlar: {len(pend)} ta",
                              chat_id, mid, reply_markup=kb)

    elif action == "settings":
        st = db.get_settings()
        kb = types.InlineKeyboardMarkup(row_width=3)
        kb.add(*[types.InlineKeyboardButton(f"💰HSK{l}: {st['cost'].get(l)}",
                                            callback_data=f"aset:cost:{l}") for l in "123456"])
        kb.add(*[types.InlineKeyboardButton(f"⏱HSK{l}: {st['time_min'].get(l)}d",
                                            callback_data=f"aset:time_min:{l}") for l in "123456"])
        kb.add(types.InlineKeyboardButton(f"🎯 Bonus chegara: {st['bonus_threshold']}%",
                                          callback_data="aset:bonus_threshold:-"),
               types.InlineKeyboardButton(f"🎁 Bonus: {st['bonus_points']}",
                                          callback_data="aset:bonus_points:-"))
        kb.add(types.InlineKeyboardButton(f"📅 Kunlik: {st['daily_points']}",
                                          callback_data="aset:daily_points:-"),
               types.InlineKeyboardButton(f"👥 Referal: {st['referral_points']}",
                                          callback_data="aset:referral_points:-"))
        kb.add(types.InlineKeyboardButton(f"🆕 Boshlang'ich: {st['start_balance']}",
                                          callback_data="aset:start_balance:-"))
        kb.add(types.InlineKeyboardButton("⬅️", callback_data="adm:home"))
        bot.edit_message_text(
            "⚙️ Sozlamalar — o'zgartirish uchun tugmani bosing:\n"
            "💰 = test narxi (ball), ⏱ = vaqt (daqiqa)",
            chat_id, mid, reply_markup=kb)

    elif action == "stats":
        exams = db.list_exams()
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️", callback_data="adm:home"))
        bot.edit_message_text(
            f"📊 Statistika\n\n👥 Foydalanuvchilar: {db.user_count()}\n"
            f"📚 Testlar: {len(exams)}\n📥 Kutilmoqda: {len(db.list_pending())}",
            chat_id, mid, reply_markup=kb)

    elif action == "top":
        rows = db.top_users(15)
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 Leaderboard (ball bo'yicha):\n"]
        for i, (uid, name, bal, done) in enumerate(rows):
            pre = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{pre} {name or uid}: {bal} ball, {done} test")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️", callback_data="adm:home"))
        bot.edit_message_text("\n".join(lines), chat_id, mid, reply_markup=kb)

    elif action == "bcast":
        admin_state[chat_id] = {"await": "broadcast"}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Bekor", callback_data="adm:home"))
        bot.edit_message_text(
            "📢 Barcha foydalanuvchilarga yuboriladigan xabarni yozing "
            "(matn/rasm/video bo'lishi mumkin):", chat_id, mid, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("aset:"))
@safe_handler
def cb_aset(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q"); return
    _, key, sub = call.data.split(":", 2)
    admin_state[call.message.chat.id] = {"await": f"set:{key}:{sub}"}
    bot.answer_callback_query(call.id)
    label = f"{key}" + (f" (HSK{sub})" if sub != "-" else "")
    bot.send_message(call.message.chat.id, f"✏️ {label} uchun yangi qiymatni raqam bilan yuboring:")


@bot.callback_query_handler(func=lambda c: c.data.startswith("delexam:"))
@safe_handler
def cb_delexam(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q"); return
    _, level, code = call.data.split(":", 2)
    db.delete_exam(level, code)
    bot.answer_callback_query(call.id, "O'chirildi")
    call.data = "adm:tests"; cb_admin(call)


def do_broadcast(src_message):
    ids = db.all_user_ids()
    sent = failed = 0
    for uid in ids:
        try:
            bot.copy_message(uid, src_message.chat.id, src_message.message_id)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)  # Telegram limitlariga rioya
    bot.send_message(src_message.chat.id,
                     f"📢 Yuborildi: {sent} ta, xato: {failed} ta (jami {len(ids)}).")


# ============================ matn xabarlari (state machine) ============================
@bot.message_handler(content_types=["text", "photo", "video", "audio", "voice"])
@safe_handler
def handle_text(message):
    uid = message.from_user.id
    chat_id = message.chat.id

    # Admin holatlari
    if is_admin(uid) and chat_id in admin_state:
        st = admin_state.pop(chat_id)
        aw = st.get("await", "")
        if aw == "broadcast":
            bot.reply_to(message, "📢 Yuborilmoqda...")
            threading.Thread(target=do_broadcast, args=(message,), daemon=True).start()
            return
        if aw.startswith("set:"):
            _, key, sub = aw.split(":", 2)
            try:
                val = int((message.text or "").strip())
            except (ValueError, AttributeError):
                bot.reply_to(message, "❌ Raqam yuboring. Bekor qilindi."); return
            db.set_setting(key, val, sub=None if sub == "-" else sub)
            bot.reply_to(message, f"✅ Saqlandi: {key}" + (f" HSK{sub}" if sub != "-" else "") + f" = {val}")
            return
        if aw.startswith("reply:"):
            target = int(aw.split(":", 1)[1])
            try:
                bot.copy_message(target, chat_id, message.message_id)
                bot.reply_to(message, "✅ Foydalanuvchiga yuborildi.")
            except Exception:
                bot.reply_to(message, "❌ Yuborib bo'lmadi (bloklagan bo'lishi mumkin).")
            return

    # User support holati
    if user_state.get(uid, {}).get("await") == "support":
        user_state.pop(uid, None)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Javob berish", callback_data=f"reply:{uid}"))
        try:
            bot.forward_message(ADMIN_ID, chat_id, message.message_id)
            notify_admin(f"🆘 Murojaat: {message.from_user.first_name} (id {uid})", kb)
            bot.reply_to(message, "✅ Murojaatingiz adminga yetkazildi. Tez orada javob beriladi.")
        except Exception:
            bot.reply_to(message, "❌ Yuborishda xato. Keyinroq urinib ko'ring.")
        return


@bot.callback_query_handler(func=lambda c: c.data.startswith("reply:"))
@safe_handler
def cb_admin_reply(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q"); return
    target = call.data.split(":", 1)[1]
    admin_state[call.message.chat.id] = {"await": f"reply:{target}"}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"✏️ id {target} ga javobingizni yozing:")


if __name__ == "__main__":
    bot.remove_webhook()
    log.info("Bot polling boshlandi (admin=%s)", ADMIN_ID)
    bot.infinity_polling()
