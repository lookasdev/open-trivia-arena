from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("matches", "0005_lobby_battle_rounds"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lobby",
            name="name",
            field=models.CharField(max_length=64),
        ),
    ]