"""A deal's sequence by its folder's name, not the path a run once found it at.

A deal enrolled before `core/sequences.key` recorded names carries the folder's absolute
path. Each becomes the folder's name, unless the lead already holds a deal under that
name: then it is left as it was, since merging two deals' histories is not a rewrite.
"""

from pathlib import PurePath

from django.db import migrations


def by_name(apps, schema_editor):
    Deal = apps.get_model("outsend_leads", "Deal")
    for deal in Deal.objects.filter(sequence__startswith="/"):
        name = PurePath(deal.sequence).name
        if Deal.objects.filter(lead_id=deal.lead_id, sequence=name).exists():
            continue
        deal.sequence = name
        deal.save(update_fields=["sequence"])


class Migration(migrations.Migration):

    dependencies = [
        ("outsend_leads", "0005_remove_deal_held_reply_reported_at"),
    ]

    operations = [
        migrations.RunPython(by_name, migrations.RunPython.noop),
    ]
