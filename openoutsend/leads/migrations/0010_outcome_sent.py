"""Add the `sent` outcome: a deal whose every step went, in an install that holds no conversations."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("outsend_leads", "0009_deal_state_is_the_sequence_step"),
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
                    ("undeliverable", "Undeliverable"),
                    ("missed", "Missed"),
                    ("stopped", "Stopped"),
                    ("sent", "Sent"),
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
