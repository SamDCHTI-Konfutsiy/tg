"""JSON-file-backed persistent store: exams, users, points, settings.

Set DB_PATH to a file inside a mounted Volume for persistence across
deploys (e.g. /data/exams_db.json on Railway).
"""
import json
import os
import time
import threading

DB_PATH = os.environ.get("DB_PATH", "exams_db.json")
_lock = threading.Lock()

LEVEL_MAP = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
}

DEFAULT_SETTINGS = {
    # har test boshlashda ayriladigan ball (HSK darajasi bo'yicha)
    "cost": {"1": 4, "2": 6, "3": 8, "4": 10, "5": 12, "6": 14},
    # test uchun umumiy vaqt, daqiqa (HSK darajasi bo'yicha)
    "time_min": {"1": 35, "2": 50, "3": 85, "4": 100, "5": 120, "6": 135},
    "bonus_threshold": 80,   # % aniqlik — bonus olish chegarasi
    "bonus_points": 15,      # bonus miqdori
    "daily_points": 20,      # kunlik kirish bonusi
    "referral_points": 10,   # har taklif uchun ball
    "start_balance": 50,     # yangi foydalanuvchi boshlang'ich ball
}


def normalize_level(raw):
    return LEVEL_MAP.get((raw or "").strip(), "?")


def _load():
    if not os.path.exists(DB_PATH):
        return {"exams": {}, "pending": {}, "users": {}, "settings": {}}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    for k in ("exams", "pending", "users", "settings"):
        data.setdefault(k, {})
    return data


def _save(data):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, DB_PATH)


def _key(level, code):
    return f"{level}::{code}"


# ------------------------- settings -------------------------
def get_settings():
    data = _load()
    s = dict(DEFAULT_SETTINGS)
    for k, v in data["settings"].items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            merged = dict(s[k]); merged.update(v); s[k] = merged
        else:
            s[k] = v
    return s


def set_setting(key, value, sub=None):
    with _lock:
        data = _load()
        if sub is not None:
            cur = data["settings"].get(key)
            if not isinstance(cur, dict):
                cur = {}
            cur[str(sub)] = value
            data["settings"][key] = cur
        else:
            data["settings"][key] = value
        _save(data)


# ------------------------- users -------------------------
def get_user(user_id, name=None):
    uid = str(user_id)
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        created = False
        if u is None:
            s = get_settings()
            u = {
                "name": name or "", "balance": s["start_balance"],
                "last_daily": "", "referred_by": None, "ref_count": 0,
                "bonus_awarded": [], "mistakes": [], "tests_done": 0,
                "joined": int(time.time()),
            }
            data["users"][uid] = u
            _save(data)
            created = True
        elif name and u.get("name") != name:
            u["name"] = name
            _save(data)
        return dict(u), created


def update_user(user_id, **fields):
    uid = str(user_id)
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if u is None:
            return None
        u.update(fields)
        _save(data)
        return dict(u)


def add_points(user_id, delta):
    uid = str(user_id)
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if u is None:
            return None
        u["balance"] = int(u.get("balance", 0)) + int(delta)
        _save(data)
        return u["balance"]


def claim_daily(user_id, today_str, points):
    """Bugungi kunlik bonus olinmagan bo'lsa beradi. True=berildi."""
    uid = str(user_id)
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if u is None or u.get("last_daily") == today_str:
            return False
        u["last_daily"] = today_str
        u["balance"] = int(u.get("balance", 0)) + int(points)
        _save(data)
        return True


def mark_bonus_awarded(user_id, exam_key):
    uid = str(user_id)
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if u is None or exam_key in u.get("bonus_awarded", []):
            return False
        u.setdefault("bonus_awarded", []).append(exam_key)
        _save(data)
        return True


def add_mistakes(user_id, items):
    """items: list of {level, code, number}"""
    uid = str(user_id)
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if u is None:
            return
        existing = {(m["level"], m["code"], m["number"]) for m in u.get("mistakes", [])}
        for it in items:
            t = (it["level"], it["code"], it["number"])
            if t not in existing:
                u.setdefault("mistakes", []).append(it)
                existing.add(t)
        _save(data)


def remove_mistakes(user_id, items):
    uid = str(user_id)
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if u is None:
            return
        rm = {(m["level"], m["code"], m["number"]) for m in items}
        u["mistakes"] = [m for m in u.get("mistakes", [])
                         if (m["level"], m["code"], m["number"]) not in rm]
        _save(data)


def all_user_ids():
    return [int(k) for k in _load()["users"].keys()]


def top_users(n=10):
    users = _load()["users"]
    rows = [(int(uid), u.get("name", ""), int(u.get("balance", 0)), u.get("tests_done", 0))
            for uid, u in users.items()]
    rows.sort(key=lambda r: -r[2])
    return rows[:n]


def user_count():
    return len(_load()["users"])


# ------------------------- exams -------------------------
def upsert_exam(level, code, questions, writing_tasks, uploaded_by, approved):
    """Approved kutubxonaga (yoki pending'ga) qo'shish.
    Bir xil test bo'lsa ko'proq savolli versiya qoladi."""
    with _lock:
        data = _load()
        bucket = "exams" if approved else "pending"
        key = _key(level, code)
        existing = data[bucket].get(key)
        if existing is None or len(questions) > len(existing["questions"]):
            record = {
                "level": level, "code": code, "questions": questions,
                "writing_tasks": writing_tasks,
                "answer_key": existing.get("answer_key") if existing else None,
                "uploaded_by": uploaded_by,
            }
            data[bucket][key] = record
            _save(data)
            return record, True
        return existing, False


def approve_pending(level, code):
    with _lock:
        data = _load()
        key = _key(level, code)
        rec = data["pending"].pop(key, None)
        if rec is None:
            return None
        existing = data["exams"].get(key)
        if existing is None or len(rec["questions"]) > len(existing["questions"]):
            if existing and existing.get("answer_key") and not rec.get("answer_key"):
                rec["answer_key"] = existing["answer_key"]
            data["exams"][key] = rec
        _save(data)
        return data["exams"][key]


def reject_pending(level, code):
    with _lock:
        data = _load()
        removed = data["pending"].pop(_key(level, code), None)
        if removed is not None:
            _save(data)
        return removed is not None


def list_pending():
    return sorted(_load()["pending"].values(), key=lambda e: (e["level"], e["code"]))


def attach_answer_key(level, code, answer_key):
    with _lock:
        data = _load()
        key = _key(level, code)
        target = None
        if key in data["exams"]:
            target = data["exams"][key]
        elif key in data["pending"]:
            target = data["pending"][key]
        if target is None:
            return False
        target["answer_key"] = {str(k): v for k, v in answer_key.items()}
        _save(data)
        return True


def list_exams(level=None):
    exams = list(_load()["exams"].values())
    if level:
        exams = [e for e in exams if e["level"] == level]
    exams.sort(key=lambda e: (e["level"], e["code"]))
    return exams


def get_exam(level, code, include_pending=False):
    data = _load()
    key = _key(level, code)
    record = data["exams"].get(key)
    if record is None and include_pending:
        record = data["pending"].get(key)
    if record and record.get("answer_key"):
        record = dict(record)
        record["answer_key"] = {int(k): v for k, v in record["answer_key"].items()}
    return record


def delete_exam(level, code):
    with _lock:
        data = _load()
        key = _key(level, code)
        if key in data["exams"]:
            del data["exams"][key]
            _save(data)
            return True
        return False
