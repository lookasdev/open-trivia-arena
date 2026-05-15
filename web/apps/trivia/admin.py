from django.contrib import admin

from .models import Question


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("id", "question_type", "source_difficulty", "category", "difficulty_score", "source")
    list_filter = ("question_type", "source_difficulty", "category", "source")
    search_fields = ("text", "correct_answer", "source_id")
    readonly_fields = ("difficulty_score",)

