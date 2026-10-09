from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("outsend_emails", "0002_mailbox_measured_on"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="prompt",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="message",
            name="prompt_path",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="message",
            name="step",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
