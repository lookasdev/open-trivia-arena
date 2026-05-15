from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


DIFFICULTY_SCORE_MAP = {
    "easy": 0.2,
    "medium": 0.5,
    "hard": 0.8,
}


class QuestionType(models.TextChoices):
    MULTIPLE_CHOICE = "multiple_choice", "Multiple Choice"
    TRUE_FALSE = "true_false", "True / False"
    ESTIMATE = "estimate", "Estimate"


class QuestionDifficulty(models.TextChoices):
    EASY = "easy", "Easy"
    MEDIUM = "medium", "Medium"
    HARD = "hard", "Hard"


class Question(models.Model):
    text = models.TextField()
    correct_answer = models.CharField(max_length=255)
    options = models.JSONField(default=list, blank=True)
    difficulty_score = models.FloatField(
        default=0.5,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    question_type = models.CharField(
        max_length=32,
        choices=QuestionType.choices,
        default=QuestionType.MULTIPLE_CHOICE,
    )
    source_difficulty = models.CharField(
        max_length=16,
        choices=QuestionDifficulty.choices,
        blank=True,
    )
    numeric_answer = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    category = models.CharField(max_length=128, blank=True)
    source = models.CharField(max_length=64, default="manual")
    source_id = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["source", "source_id"], name="unique_question_source_id"),
        ]

    def clean(self) -> None:
        super().clean()
        if self.question_type == QuestionType.ESTIMATE and self.numeric_answer is None:
            raise ValidationError({"numeric_answer": "Estimate questions require numeric_answer."})
        if self.question_type in {QuestionType.MULTIPLE_CHOICE, QuestionType.TRUE_FALSE} and not self.options:
            raise ValidationError({"options": "Choice-based questions require options."})
        if self.question_type == QuestionType.TRUE_FALSE and sorted(self.options) != ["False", "True"]:
            raise ValidationError({"options": "True / False questions must use ['False', 'True'] options."})

    def save(self, *args, **kwargs):
        if self.source_difficulty:
            self.difficulty_score = DIFFICULTY_SCORE_MAP[self.source_difficulty]
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.text[:80]
