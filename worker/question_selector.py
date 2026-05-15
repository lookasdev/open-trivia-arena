import math

from apps.trivia.models import Question, QuestionDifficulty, QuestionType


def target_difficulty_for_elo(average_elo: int) -> str:
    if average_elo < 1150:
        return QuestionDifficulty.EASY
    if average_elo < 1450:
        return QuestionDifficulty.MEDIUM
    return QuestionDifficulty.HARD


def fallback_difficulty_score(average_elo: int) -> float:
    normalized = max(0.0, min(1.0, (average_elo - 800) / 1000))
    return round(normalized, 2)


def select_question(
    average_elo: int,
    exclude_ids: set[int] | None = None,
    categories: list[str] | None = None,
    question_types: list[str] | None = None,
    difficulties: list[str] | None = None,
) -> Question:
    exclude_ids = exclude_ids or set()
    queryset = Question.objects.exclude(id__in=exclude_ids)
    if categories:
        queryset = queryset.filter(category__in=categories)
    if question_types:
        queryset = queryset.filter(question_type__in=question_types)

    difficulty_pool = [difficulty for difficulty in (difficulties or []) if difficulty in QuestionDifficulty.values]
    if not difficulty_pool:
        difficulty_pool = [target_difficulty_for_elo(average_elo)]

    exact = queryset.filter(source_difficulty__in=difficulty_pool).order_by("id").first()
    if exact is not None:
        return exact

    if difficulties:
        queryset = queryset.filter(source_difficulty__in=difficulty_pool)

    target_score = fallback_difficulty_score(average_elo)
    questions = list(queryset.order_by("id")[:50])
    if not questions:
        raise Question.DoesNotExist("No questions available for game worker")
    return min(questions, key=lambda question: math.fabs(question.difficulty_score - target_score))
