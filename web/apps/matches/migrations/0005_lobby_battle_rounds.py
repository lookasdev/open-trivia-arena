from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("matches", "0004_lobby_test_mode_and_match_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="lobby",
            name="battle_rounds",
            field=models.PositiveSmallIntegerField(
                default=12,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(50),
                ],
            ),
        ),
    ]