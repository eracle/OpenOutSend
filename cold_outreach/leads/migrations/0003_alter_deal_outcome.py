from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leads", "0002_alter_deal_state"),
    ]

    operations = [
        migrations.AlterField(
            model_name="deal",
            name="outcome",
            field=models.CharField(
                blank=True,
                choices=[
                    ("converted", "Converted"),
                    ("not_interested", "Not Interested"),
                    ("wrong_fit", "Wrong Fit"),
                    ("no_budget", "No Budget"),
                    ("has_solution", "Has Solution"),
                    ("bad_timing", "Bad Timing"),
                    ("unresponsive", "Unresponsive"),
                    ("unsubscribed", "Unsubscribed"),
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
