from django.db import migrations, models


def mark_openers_as_step_zero(apps, schema_editor):
    """Every deal already written to has sent cold's only step, E0: say so on both rows.

    The planner counts follow-ups from the newest send that carries a step number, so
    an opener sent before steps existed must carry ``0`` or its thread would never be
    followed up or closed.
    """
    Deal = apps.get_model("outsend_leads", "Deal")
    Message = apps.get_model("outsend_emails", "Message")

    for deal in Deal.objects.filter(thread__isnull=False):
        opener = (
            Message.objects
            .filter(thread_id=deal.thread_id, direction="out", kind="outbound")
            .order_by("sent_at", "pk")
            .first()
        )
        if opener is None:
            continue
        Message.objects.filter(pk=opener.pk).update(step=0)
        Deal.objects.filter(pk=deal.pk).update(step=0)


class Migration(migrations.Migration):

    dependencies = [
        ("outsend_leads", "0002_pendingdraft"),
        ("outsend_emails", "0003_message_prompt_step"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="deal",
            name="chat_summary",
        ),
        migrations.AddField(
            model_name="deal",
            name="sequence",
            field=models.CharField(default="cold", max_length=500),
        ),
        migrations.AddField(
            model_name="deal",
            name="step",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(mark_openers_as_step_zero, migrations.RunPython.noop),
    ]
