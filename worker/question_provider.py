from __future__ import annotations

import csv
import hashlib
import random
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from apps.trivia.models import DIFFICULTY_SCORE_MAP, Question, QuestionDifficulty, QuestionType
from apps.trivia.services.opentdb import CATEGORIES


CSV_TYPE_BY_INTERNAL = {
    QuestionType.MULTIPLE_CHOICE: "multiple",
    QuestionType.TRUE_FALSE: "boolean",
    QuestionType.ESTIMATE: "estimate",
}

CATEGORY_CSV_FILENAME_BY_NAME = {
    "Books": "Entertainment - Books",
    "Film": "Entertainment - Film",
    "Music": "Entertainment - Music",
    "Musicals & Theatres": "Entertainment - Musicals & Theatres",
    "Television": "Entertainment - Television",
    "Video Games": "Entertainment - Video Games",
    "Board Games": "Entertainment - Board Games",
    "Computers": "Science - Computers",
    "Mathematics": "Science - Mathematics",
    "Comics": "Entertainment - Comics",
    "Gadgets": "Science - Gadgets",
    "Anime & Manga": "Entertainment - Japanese Anime & Manga",
    "Cartoon & Animations": "Entertainment - Cartoon & Animations",
}

INTERNAL_TYPE_BY_CSV_TYPE = {
    "multiple": QuestionType.MULTIPLE_CHOICE,
    "boolean": QuestionType.TRUE_FALSE,
    "estimate": QuestionType.ESTIMATE,
}

CSV_DATA_DIR = Path(__file__).resolve().parent.parent / "opentdb-scrape" / "data"

TEST_MODE_QUESTIONS = [
    {
        "text": "What do sailors call the back of a boat?",
        "correct_answer": "Stern",
        "options": ["Stern", "Bow", "Starboard", "Port"],
        "question_type": QuestionType.MULTIPLE_CHOICE,
        "source_difficulty": QuestionDifficulty.EASY,
        "category": "General Knowledge",
    },
    {
        "text": "Which type of cutlery is most suited for eating soup?",
        "correct_answer": "Spoon",
        "options": ["Spoon", "Fork", "Knife", "Chopsticks"],
        "question_type": QuestionType.MULTIPLE_CHOICE,
        "source_difficulty": QuestionDifficulty.EASY,
        "category": "General Knowledge",
    },
    {
        "text": "How many moons does the Earth have?",
        "correct_answer": "1",
        "options": ["1", "0", "2", "3"],
        "question_type": QuestionType.MULTIPLE_CHOICE,
        "source_difficulty": QuestionDifficulty.EASY,
        "category": "Science & Nature",
    },
    {
        "text": "Who is the lead singer of Pearl Jam?",
        "correct_answer": "Eddie Vedder",
        "options": ["Eddie Vedder", "Ozzy Osbourne", "Stone Gossard", "Kurt Cobain"],
        "question_type": QuestionType.MULTIPLE_CHOICE,
        "source_difficulty": QuestionDifficulty.MEDIUM,
        "category": "Music",
    },
    {
        "text": "Which Pacific Island country is ruled by a constitutional monarchy?",
        "correct_answer": "Tonga",
        "options": ["Tonga", "Palau", "Fiji", "Kiribati"],
        "question_type": QuestionType.MULTIPLE_CHOICE,
        "source_difficulty": QuestionDifficulty.MEDIUM,
        "category": "Politics",
    },
    {
        "text": "What simple machine uses a stiff arm that rotates around a fixed point?",
        "correct_answer": "Lever",
        "options": ["Lever", "Wedge", "Screw", "Pulley"],
        "question_type": QuestionType.MULTIPLE_CHOICE,
        "source_difficulty": QuestionDifficulty.EASY,
        "category": "Gadgets",
    },
    {
        "text": "Portal 2 was released in 2011.",
        "correct_answer": "True",
        "options": ["True", "False"],
        "question_type": QuestionType.TRUE_FALSE,
        "source_difficulty": QuestionDifficulty.EASY,
        "category": "Video Games",
    },
    {
        "text": "The second book in 'A Song of Ice and Fire' is 'A Clash of Kings'.",
        "correct_answer": "True",
        "options": ["True", "False"],
        "question_type": QuestionType.TRUE_FALSE,
        "source_difficulty": QuestionDifficulty.MEDIUM,
        "category": "Books",
    },
    {
        "text": "Canada stopped producing the penny in 2012.",
        "correct_answer": "True",
        "options": ["True", "False"],
        "question_type": QuestionType.TRUE_FALSE,
        "source_difficulty": QuestionDifficulty.EASY,
        "category": "General Knowledge",
    },
    {
        "text": "A castle icon should be visible on each claimed base.",
        "correct_answer": "True",
        "options": ["True", "False"],
        "question_type": QuestionType.TRUE_FALSE,
        "source_difficulty": QuestionDifficulty.EASY,
        "category": "General Knowledge",
    },
]


def ensure_test_mode_questions() -> list[Question]:
    questions: list[Question] = []
    for index, item in enumerate(TEST_MODE_QUESTIONS, start=1):
        question, _ = Question.objects.update_or_create(
            source="test_mode",
            source_id=f"test-mode-{index}",
            defaults={
                "text": item["text"],
                "correct_answer": item["correct_answer"],
                "options": item["options"],
                "question_type": item["question_type"],
                "source_difficulty": item["source_difficulty"],
                "difficulty_score": DIFFICULTY_SCORE_MAP[item["source_difficulty"]],
                "category": item["category"],
            },
        )
        questions.append(question)
    return questions


def fetch_runtime_questions(
    *,
    amount: int,
    categories: list[str] | None,
    question_types: list[str] | None,
    difficulties: list[str] | None,
    batch_index: int,
) -> list[Question]:
    del batch_index

    selected_categories = [category for category in (categories or []) if category in CATEGORIES and CATEGORIES[category] is not None]
    selected_difficulties = [difficulty for difficulty in (difficulties or []) if difficulty in QuestionDifficulty.values]
    selected_question_types = {CSV_TYPE_BY_INTERNAL[value] for value in (question_types or []) if value in CSV_TYPE_BY_INTERNAL}

    category_pool = selected_categories or _available_csv_categories()

    payloads = []
    for category_name in category_pool:
        payloads.extend(_load_csv_questions(category_name))

    if selected_difficulties:
        payloads = [payload for payload in payloads if payload.source_difficulty in selected_difficulties]
    if selected_question_types:
        payloads = [payload for payload in payloads if payload.csv_question_type in selected_question_types]

    if not payloads:
        raise Question.DoesNotExist("No CSV runtime questions available for this match.")

    sample_size = min(amount, len(payloads))
    payloads = random.SystemRandom().sample(payloads, sample_size)

    questions: list[Question] = []
    for payload in payloads:
        question, _ = Question.objects.update_or_create(
            source="csv_runtime",
            source_id=payload.source_id,
            defaults={
                "text": payload.text,
                "correct_answer": payload.correct_answer,
                "options": payload.options,
                "question_type": payload.question_type,
                "source_difficulty": payload.source_difficulty,
                "difficulty_score": DIFFICULTY_SCORE_MAP.get(payload.source_difficulty, 0.5),
                "numeric_answer": payload.numeric_answer,
                "category": payload.category,
            },
        )
        questions.append(question)
    return questions


@lru_cache(maxsize=1)
def build_csv_lobby_metadata() -> dict:
    category_rows = []
    category_type_counts: dict[str, dict[str, int]] = {}
    for category_name in _available_csv_categories():
        payloads = _load_csv_questions(category_name)
        type_counts = {"multiple": 0, "boolean": 0, "estimate": 0}
        for payload in payloads:
            type_counts[payload.csv_question_type] = type_counts.get(payload.csv_question_type, 0) + 1
        category_type_counts[category_name] = type_counts
        category_rows.append(
            {
                "value": category_name,
                "label": f"{category_name} ({len(payloads)})",
                "count": len(payloads),
            }
        )

    category_rows.sort(key=lambda row: (-row["count"], row["value"]))

    question_type_info = {}
    for csv_type, internal_type in INTERNAL_TYPE_BY_CSV_TYPE.items():
        sorted_rows = sorted(
            (
                {"label": row["value"], "count": category_type_counts[row["value"]].get(csv_type, 0)}
                for row in category_rows
            ),
            key=lambda row: (-row["count"], row["label"]),
        )
        total = sum(row["count"] for row in sorted_rows)
        lines = [f"{row['label']}: {row['count']} matches" for row in sorted_rows]
        lines.append(f"Total {csv_type} matches: {total}")
        question_type_info[internal_type] = "\n".join(lines)

    return {
        "categories": category_rows,
        "question_type_info": question_type_info,
    }


def _available_csv_categories() -> list[str]:
    return [category for category, category_id in CATEGORIES.items() if category_id is not None and _category_csv_path(category).exists()]


def _category_csv_path(category_name: str) -> Path:
    csv_name = CATEGORY_CSV_FILENAME_BY_NAME.get(category_name, category_name)
    return CSV_DATA_DIR / f"{csv_name}.csv"


@lru_cache(maxsize=None)
def _load_csv_questions(category_name: str) -> tuple[QuestionPayload, ...]:
    csv_path = _category_csv_path(category_name)
    if not csv_path.exists():
        return tuple()

    payloads: list[QuestionPayload] = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            normalized_difficulty = (row.get("Difficulty") or "").strip().lower()
            normalized_type = (row.get("Type") or "").strip().lower()

            correct_answer = (row.get("Correct Answer") or "").strip()
            text = (row.get("Question") or "").strip()
            if not text or not correct_answer:
                continue

            options = [
                correct_answer,
                (row.get("Wrong 1") or "").strip(),
                (row.get("Wrong 2") or "").strip(),
                (row.get("Wrong 3") or "").strip(),
            ]
            options = [option for option in options if option]
            numeric_answer = None
            if normalized_type == "estimate":
                numeric_answer = _parse_decimal_answer(correct_answer)
                if numeric_answer is None:
                    continue
                options = []
            elif normalized_type == "boolean":
                options = ["True", "False"]

            if normalized_type == "boolean":
                question_type_value = QuestionType.TRUE_FALSE
            elif normalized_type == "estimate":
                question_type_value = QuestionType.ESTIMATE
            else:
                question_type_value = QuestionType.MULTIPLE_CHOICE
            source_key = f"{category_name}|{normalized_difficulty}|{normalized_type}|{text}|{correct_answer}"
            payloads.append(
                QuestionPayload(
                    text=text,
                    correct_answer=correct_answer,
                    options=options,
                    numeric_answer=numeric_answer,
                    question_type=question_type_value,
                    csv_question_type=normalized_type,
                    source_difficulty=normalized_difficulty if normalized_difficulty in QuestionDifficulty.values else "",
                    category=category_name,
                    source_id=hashlib.sha256(source_key.encode("utf-8")).hexdigest(),
                )
            )

    return tuple(payloads)


def _parse_decimal_answer(value: str) -> Decimal | None:
    normalized = value.strip().replace(",", "")
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


class QuestionPayload:
    def __init__(self, *, text: str, correct_answer: str, options: list[str], numeric_answer, question_type: str, csv_question_type: str, source_difficulty: str, category: str, source_id: str):
        self.text = text
        self.correct_answer = correct_answer
        self.options = options
        self.numeric_answer = numeric_answer
        self.question_type = question_type
        self.csv_question_type = csv_question_type
        self.source_difficulty = source_difficulty
        self.category = category
        self.source_id = source_id