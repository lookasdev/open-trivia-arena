from django.core.management.base import BaseCommand, CommandError

from apps.trivia.models import Question
from apps.trivia.services.opentdb import CATEGORIES, DIFFICULTIES, QUESTION_TYPES, fetch_opentdb_questions


class Command(BaseCommand):
    help = "Fetch questions from OpenTDB and persist them locally."

    def add_arguments(self, parser):
        parser.add_argument("--amount", type=int, default=10)
        parser.add_argument("--category", type=int)
        parser.add_argument("--difficulty", choices=[value for value in DIFFICULTIES.values() if value is not None])
        parser.add_argument("--type", dest="question_type", choices=[value for value in QUESTION_TYPES.values() if value is not None])

    def handle(self, *args, **options):
        amount = options["amount"]
        category = options.get("category")
        difficulty = options.get("difficulty")
        question_type = options.get("question_type")

        valid_categories = {value for value in CATEGORIES.values() if value is not None}
        if category is not None and category not in valid_categories:
            raise CommandError("Unsupported category id.")

        questions = fetch_opentdb_questions(
            amount=amount,
            category=category,
            difficulty=difficulty,
            question_type=question_type,
        )

        created_count = 0
        updated_count = 0
        for question_payload in questions:
            _, created = Question.objects.update_or_create(
                source="opentdb",
                source_id=question_payload.source_id,
                defaults={
                    "text": question_payload.text,
                    "correct_answer": question_payload.correct_answer,
                    "options": question_payload.options,
                    "question_type": question_payload.question_type,
                    "source_difficulty": question_payload.source_difficulty,
                    "category": question_payload.category,
                },
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(questions)} questions from OpenTDB ({created_count} created, {updated_count} updated)."
            )
        )