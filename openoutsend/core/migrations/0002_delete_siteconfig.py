"""Drop the config table: what a human answers now comes from `OUTSEND_*` every run.

Nothing is carried forward. The row held the model, its key, and the campaign's prose —
values an operator supplied once and this program has no business remembering. An
existing install re-supplies them as environment variables, and `check_ready()` names
every one that is missing before a pass starts rather than after.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [("outsend_core", "0001_initial")]

    operations = [migrations.DeleteModel(name="SiteConfig")]
