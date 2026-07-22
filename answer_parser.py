"""Parser for official HSK answer-key PDFs.

The official Hanban answer sheet lays answers out as small tables:
one row of option letters (A-D) followed by a row of question
numbers underneath them, repeated across the page, e.g.:

    B B A C D
    1. 2. 3. 4. 5.

This module pairs each letter with the number below it.
"""
import re
import pdfplumber

LETTER_ROW_RE = re.compile(r"^[ABCDАВСД](\s+[ABCDАВСД]){0,4}$")
NUMBER_ROW_RE = re.compile(r"^(\d{1,3}\s*\.?\s*){1,5}$")
LETTER_NORM = {"А": "A", "В": "B", "С": "C", "Д": "D"}


def parse_answer_key(path):
    answers = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines = [ln.strip() for ln in (page.extract_text() or "").split("\n")]
            i = 0
            while i < len(lines) - 1:
                letters_line, numbers_line = lines[i], lines[i + 1]
                if LETTER_ROW_RE.match(letters_line) and NUMBER_ROW_RE.match(numbers_line):
                    letters = letters_line.split()
                    numbers = [int(n) for n in re.findall(r"\d{1,3}", numbers_line)]
                    for num, letter in zip(numbers, letters):
                        answers[num] = LETTER_NORM.get(letter, letter)
                    i += 2
                else:
                    i += 1
    return answers
