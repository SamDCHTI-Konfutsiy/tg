"""Simple JSON-file-backed persistent store for parsed HSK exams.

Exams are keyed by (level, code). When two uploads turn out to be the
same exam (same level+code), the version with more successfully
parsed questions is kept — this is what makes the library get better
over time as more people upload the same official PDF.

NOTE: on Railway (and most PaaS), the filesystem is ephemeral unless
a persistent Volume is attached. Without one, this file resets on
every redeploy. Set DB_PATH to a path inside a mounted Volume for
real persistence across deploys.
"""
import json
import os
import threading

DB_PATH = os.environ.get("DB_PATH", "exams_db.json")
_lock = threading.Lock()

LEVEL_MAP = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
}


def normalize_level(raw):
    return LEVEL_MAP.get((raw or "").strip(), "?")


def _load():
    if not os.path.exists(DB_PATH):
        return {"exams": {}}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"exams": {}}


def _save(data):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_PATH)


def _key(level, code):
    return f"{level}::{code}"


def upsert_exam(level, code, questions, writing_tasks, uploaded_by, file_unique_id=None):
    """Add or update an exam. Returns (record, was_replaced)."""
    with _lock:
        data = _load()
        key = _key(level, code)
        existing = data["exams"].get(key)
        if existing is None or len(questions) > len(existing["questions"]):
            record = {
                "level": level,
                "code": code,
                "questions": questions,
                "writing_tasks": writing_tasks,
                "answer_key": existing["answer_key"] if existing else None,
                "uploaded_by": uploaded_by,
                "file_unique_id": file_unique_id,
            }
            data["exams"][key] = record
            _save(data)
            return record, True
        return existing, False


def attach_answer_key(level, code, answer_key):
    with _lock:
        data = _load()
        key = _key(level, code)
        if key not in data["exams"]:
            return False
        data["exams"][key]["answer_key"] = {str(k): v for k, v in answer_key.items()}
        _save(data)
        return True


def list_exams(level=None):
    data = _load()
    exams = list(data["exams"].values())
    if level:
        exams = [e for e in exams if e["level"] == level]
    exams.sort(key=lambda e: (e["level"], e["code"]))
    return exams


def get_exam(level, code):
    data = _load()
    record = data["exams"].get(_key(level, code))
    if record and record.get("answer_key"):
        # JSON object keys are always strings; convert back to int
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
