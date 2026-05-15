from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("matches", "0006_lobby_name_not_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="lobby",
            name="timer_seconds",
            field=models.PositiveSmallIntegerField(
                default=20,
                validators=[django.core.validators.MinValueValidator(10), django.core.validators.MaxValueValidator(30)],
            ),
        ),
    ]