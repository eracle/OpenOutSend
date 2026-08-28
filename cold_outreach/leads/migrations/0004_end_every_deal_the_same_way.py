"""Two terminal states become one, with the outcome saying which.

`UNSUBSCRIBED` and `UNDELIVERABLE` were endings kept in the state column while the
other seven lived in `outcome`, so "why did this end" was answered by reading one
column for some deals and the other for the rest. The rows move before the choices
narrow, or a deal in a state the model no longer knows would be left behind.
"""
from django.db import migrations, models

_MOVED = {
    "Unsubscribed": "unsubscribed",
    "Undeliverable": "undeliverable",
}


def end_them_as_completed(apps, schema_editor):
    """Move the two terminal states onto `Completed`, keeping why in the outcome.

    An outcome already on the row is left alone: a deal that says why it ended has
    the better answer, and these two states were only ever set on deals that had none.
    """
    Deal = apps.get_model("leads", "Deal")
    for state, outcome in _MOVED.items():
        Deal.objects.filter(state=state, outcome="").update(state="Completed", outcome=outcome)
        Deal.objects.filter(state=state).update(state="Completed")


def split_them_back(apps, schema_editor):
    """Reverse: the outcome that named a terminal state becomes that state again."""
    Deal = apps.get_model("leads", "Deal")
    for state, outcome in _MOVED.items():
        Deal.objects.filter(state="Completed", outcome=outcome).update(state=state, outcome="")


class Migration(migrations.Migration):

    dependencies = [
        ("leads", "0003_alter_deal_outcome"),
    ]

    operations = [
        migrations.RunPython(end_them_as_completed, split_them_back),
        migrations.AlterField(
            model_name="deal",
            name="state",
            field=models.CharField(
                choices=[
                    ("Ready to Email", "Ready"),
                    ("Emailed", "Emailed"),
                    ("Completed", "Completed"),
                ],
                default="Ready to Email",
                max_length=32,
            ),
        ),
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
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
