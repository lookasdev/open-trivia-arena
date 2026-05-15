import hashlib
import html
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from apps.trivia.models import QuestionDifficulty, QuestionType


OPENTDB_API_URL = "https://opentdb.com/api.php"

CATEGORY_DISPLAY_NAMES = {
    "Entertainment: Books": "Books",
    "Entertainment: Film": "Film",
    "Entertainment: Music": "Music",
    "Entertainment: Musicals & Theatres": "Musicals & Theatres",
    "Entertainment: Television": "Television",
    "Entertainment: Video Games": "Video Games",
    "Entertainment: Board Games": "Board Games",
    "Science: Computers": "Computers",
    "Science: Mathematics": "Mathematics",
    "Entertainment: Comics": "Comics",
    "Science: Gadgets": "Gadgets",
    "Entertainment: Japanese Anime & Manga": "Anime & Manga",
    "Entertainment: Cartoon & Animations": "Cartoon & Animations",
}

CATEGORIES = {
    "Any Category": None,
    "General Knowledge": 9,
    "Books": 10,
    "Film": 11,
    "Music": 12,
    "Musicals & Theatres": 13,
    "Television": 14,
    "Video Games": 15,
    "Board Games": 16,
    "Science & Nature": 17,
    "Computers": 18,
    "Mathematics": 19,
    "Mythology": 20,
    "Sports": 21,
    "Geography": 22,
    "History": 23,
    "Politics": 24,
    "Art": 25,
    "Celebrities": 26,
    "Animals": 27,
    "Vehicles": 28,
    "Comics": 29,
    "Gadgets": 30,
    "Anime & Manga": 31,
    "Cartoon & Animations": 32,
}

DIFFICULTIES = {
    "Any Difficulty": None,
    "Easy": QuestionDifficulty.EASY,
    "Medium": QuestionDifficulty.MEDIUM,
    "Hard": QuestionDifficulty.HARD,
}

QUESTION_TYPES = {
    "Any Type": None,
    "True/False": "boolean",
    "Multiple Choice": "multiple",
}


@dataclass(frozen=True)
class OpenTDBQuestionPayload:
    text: str
    correct_answer: str
    options: list[str]
    question_type: str
    source_difficulty: str
    category: str
    source_id: str


def build_opentdb_url(amount: int, category: int | None = None, difficulty: str | None = None, question_type: str | None = None) -> str:
    query: dict[str, Any] = {"amount": amount}
    if category is not None:
        query["category"] = category
    if difficulty is not None:
        query["difficulty"] = difficulty
    if question_type is not None:
        query["type"] = question_type
    return f"{OPENTDB_API_URL}?{urlencode(query)}"


def fetch_opentdb_questions(amount: int, category: int | None = None, difficulty: str | None = None, question_type: str | None = None) -> list[OpenTDBQuestionPayload]:
    url = build_opentdb_url(amount=amount, category=category, difficulty=difficulty, question_type=question_type)
    with urlopen(url, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    response_code = payload.get("response_code")
    if response_code != 0:
        raise ValueError(f"OpenTDB returned response_code={response_code}")

    return [normalize_opentdb_question(item) for item in payload.get("results", [])]


def normalize_opentdb_question(item: dict[str, Any]) -> OpenTDBQuestionPayload:
    text = html.unescape(item["question"]).strip()
    correct_answer = html.unescape(item["correct_answer"]).strip()
    incorrect_answers = [html.unescape(answer).strip() for answer in item.get("incorrect_answers", [])]
    raw_type = item["type"]
    question_type = QuestionType.TRUE_FALSE if raw_type == "boolean" else QuestionType.MULTIPLE_CHOICE
    options = [correct_answer, *incorrect_answers]
    options = sorted(set(options), key=options.index)
    if question_type == QuestionType.TRUE_FALSE:
        options = ["True", "False"]

    raw_category = html.unescape(item.get("category", "")).strip()
    category = CATEGORY_DISPLAY_NAMES.get(raw_category, raw_category)
    source_key = f"{raw_category}|{item.get('difficulty', '')}|{text}|{correct_answer}"
    source_id = hashlib.sha256(source_key.encode("utf-8")).hexdigest()

    return OpenTDBQuestionPayload(
        text=text,
        correct_answer=correct_answer,
        options=options,
        question_type=question_type,
        source_difficulty=item["difficulty"],
        category=category,
        source_id=source_id,
    )