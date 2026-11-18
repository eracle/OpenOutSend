"""Drop `Deal.step`, and add the `stopped` outcome.

Which step a deal is owed is its state in its sequence's machine
(`leads/lifecycle.py`), so the column goes. No deal is moved here: a store that held
deals under the three-state machine (`Ready to Email`, `Emailed`, `Completed`) has
them placed in the new states by hand before it is run again.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("outsend_leads", "0008_deal_state_to_the_deal_machine"),
    ]

    operations = [
        migrations.RemoveField(model_name="deal", name="step"),
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
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
