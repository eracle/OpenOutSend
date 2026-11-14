from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("outsend_leads", "0006_deal_sequence_by_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="timezone",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
