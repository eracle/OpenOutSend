"""A message's sequence by its folder's name, as its deal now records it."""

from pathlib import PurePath

from django.db import migrations


def by_name(apps, schema_editor):
    Message = apps.get_model("outsend_emails", "Message")
    for message in Message.objects.filter(sequence__startswith="/"):
        message.sequence = PurePath(message.sequence).name
        message.save(update_fields=["sequence"])


class Migration(migrations.Migration):

    dependencies = [
        ("outsend_emails", "0005_resend_transport"),
    ]

    operations = [
        migrations.RunPython(by_name, migrations.RunPython.noop),
    ]
