from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matches", "0003_lobby_match_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="lobby",
            name="test_mode",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="match",
            name="current_state",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("base_selection", "Base Selection"),
                    ("expansion", "Expansion"),
                    ("duel", "Duel"),
                    ("base_siege", "Base Siege"),
                    ("finished", "Finished"),
                ],
                default="queued",
                max_length=32,
            ),
        ),
    ]