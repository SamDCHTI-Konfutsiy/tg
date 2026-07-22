"""Universal HSK exam PDF parser.

Works with future HSK exam PDFs (levels 1-6) that follow the standard
Hanban layout: numbered multiple-choice questions with options A-D,
grouped reading passages, and a non-multiple-choice writing section.
"""
import re
import bisect
import pdfplumber

LETTERS = "ABCD"
# Some scanned/converted HSK PDFs substitute Cyrillic look-alikes for
# certain Latin letters (most commonly B -> В). Accept both.
LETTER_CHARS = {"A": "AА", "B": "BВ", "C": "CС", "D": "D"}


def _find_column_split(page, header_frac=0.22, footer_frac=0.95, min_gap_frac=0.03):
    """Look for a genuine vertical whitespace gap near the middle of
    the page (a two-column layout) and return its x-midpoint, or None
    if the page is single-column. Ignores the header/footer bands so
    centered titles and page numbers don't interfere."""
    words = page.extract_words()
    if not words:
        return None
    width, height = page.width, page.height
    body_words = [
        w for w in words
        if height * header_frac < w["top"] < height * footer_frac
    ]
    if len(body_words) < 6:
        return None
    intervals = sorted((w["x0"], w["x1"]) for w in body_words)
    merged = []
    for x0, x1 in intervals:
        if merged and x0 <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], x1))
        else:
            merged.append((x0, x1))
    lo_bound, hi_bound = width * 0.30, width * 0.70
    best_gap, best_mid = 0, None
    for (a0, a1), (b0, b1) in zip(merged, merged[1:]):
        gap_start, gap_end = a1, b0
        mid = (gap_start + gap_end) / 2
        if lo_bound < mid < hi_bound:
            gap_width = gap_end - gap_start
            if gap_width > best_gap:
                best_gap, best_mid = gap_width, mid
    if best_mid is not None and best_gap > width * min_gap_frac:
        return best_mid
    return None


FOOTER_LINE_RE = re.compile(r"(?m)^\s*[A-Z]?\d{4,6}\s*[-−]\s*\d{1,3}\s*$")
FURNITURE_LINE_RE = re.compile(
    r"(?m)^\s*(第[一二三四五六七八九十]+部分"
    r"|第\s*\d+\s*[-−]\s*\d+\s*题[^\n]*"
    r"|[一二三]\s*、\s*(?:听\s*力|阅\s*读|书\s*写))\s*$"
)


def _clean(t):
    t = FOOTER_LINE_RE.sub("", t)
    t = FURNITURE_LINE_RE.sub("", t)
    return t


def _page_text(page):
    split = _find_column_split(page)
    if split is not None:
        w, h = page.width, page.height
        left = page.crop((0, 0, split, h)).extract_text() or ""
        right = page.crop((split, 0, w, h)).extract_text() or ""
        raw = left + "\n" + right
    else:
        raw = page.extract_text() or ""
    return _clean(raw)


SECTION_RE = re.compile(r"(?m)^\s*[一二三]\s*、\s*(听\s*力|阅\s*读|书\s*写)")


def _norm_section(raw):
    return re.sub(r"\s+", "", raw)


def _page_sections(pdf):
    """Section name (听力/阅读/书写) each page belongs to, using the
    page's normal (non-column-split) text so a heading that visually
    spans the full page width is read as one clean line."""
    sections, current = [], ""
    for page in pdf.pages:
        t = page.extract_text() or ""
        found = SECTION_RE.findall(t)
        if found:
            current = _norm_section(found[-1])
        sections.append(current)
    return sections


def _full_text_with_offsets(path):
    parts, offsets = [], []
    with pdfplumber.open(path) as pdf:
        page_secs = _page_sections(pdf)
        cum = 0
        for page in pdf.pages:
            t = _page_text(page)
            offsets.append(cum)
            parts.append(t)
            cum += len(t) + 1
    return "\n".join(parts), offsets, page_secs


def _full_text(path):
    text, _, _ = _full_text_with_offsets(path)
    return text


QNUM_RE = re.compile(r"(?m)^\s*(\d{1,3})\s*[.、]")
GROUP_HEADER_RE = re.compile(r"(?m)^\s*(\d{1,3})\s*-\s*(\d{1,3})\s*[.．]?\s*$")


def _split_options(block):
    """Split a question's raw text block into stem + options A-D."""
    # Build a regex that finds each option marker as a standalone token.
    marker_positions = []
    for letter in LETTERS:
        chars = LETTER_CHARS[letter]
        for m in re.finditer(rf"(?:(?<=\s)|^)[{chars}](?=[\.\s])", block):
            marker_positions.append((m.start(), letter))
    if len(marker_positions) < 4:
        return None  # not a multiple-choice question
    marker_positions.sort()
    # keep the first occurrence of each letter, in order A,B,C,D
    seen = {}
    for pos, letter in marker_positions:
        if letter not in seen:
            seen[letter] = pos
    if list(seen.keys())[:4] != list(LETTERS) and set(LETTERS) - seen.keys():
        return None
    try:
        a, b, c, d = seen["A"], seen["B"], seen["C"], seen["D"]
    except KeyError:
        return None
    if not (a < b < c < d):
        return None
    def option_text(raw):
        # Options are always a single line in the official HSK layout;
        # capping here also guards against a neighbouring block leaking
        # in when a question number fails to match (e.g. a damaged
        # glyph in the source PDF).
        return raw.lstrip(". \n").strip().split("\n")[0].strip()

    stem = block[:a].strip()
    opt_a = option_text(block[a + 1:b])
    opt_b = option_text(block[b + 1:c])
    opt_c = option_text(block[c + 1:d])
    opt_d = option_text(block[d + 1:])
    return stem, {"A": opt_a, "B": opt_b, "C": opt_c, "D": opt_d}


def parse_exam(path):
    text, offsets, page_secs = _full_text_with_offsets(path)

    level_m = re.search(r"HSK\s*[（(]?\s*([1-6一二三四五六])\s*级?", text)
    code_m = re.search(r"\bH\d{5}\b", text)

    def section_for(pos):
        idx = max(0, bisect.bisect_right(offsets, pos) - 1)
        return page_secs[idx] if idx < len(page_secs) else ""

    # shared passages for grouped questions like "46-48."
    group_passage_for = {}
    group_headers = list(GROUP_HEADER_RE.finditer(text))
    qnum_all = list(QNUM_RE.finditer(text))
    for gm in group_headers:
        lo, hi = int(gm.group(1)), int(gm.group(2))
        passage_start = gm.end()
        # passage ends where the first question number of the group begins
        next_q = next((q for q in qnum_all if int(q.group(1)) == lo and q.start() >= passage_start), None)
        passage_end = next_q.start() if next_q else passage_start
        passage = text[passage_start:passage_end].strip()
        for n in range(lo, hi + 1):
            group_passage_for[n] = passage

    questions = []
    writing_tasks = []
    matches = list(QNUM_RE.finditer(text))
    for i, m in enumerate(matches):
        num = int(m.group(1))
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[block_start:block_end]
        result = _split_options(block)
        sec = section_for(m.start())
        if result is None:
            stem_only = block.strip()
            if stem_only and sec == "书写":
                writing_tasks.append({"number": num, "text": stem_only})
            continue
        stem, options = result
        passage = group_passage_for.get(num, "")
        questions.append({
            "number": num,
            "section": sec,
            "passage": passage,
            "stem": stem,
            "options": options,
        })

    # de-duplicate by number, keep first, sort ascending
    dedup = {}
    for q in questions:
        dedup.setdefault(q["number"], q)
    questions = [dedup[n] for n in sorted(dedup)]

    return {
        "level": level_m.group(1) if level_m else None,
        "code": code_m.group(0) if code_m else None,
        "questions": questions,
        "writing_tasks": writing_tasks,
    }
