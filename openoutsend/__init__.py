"""OpenOutSend: the sending half of OpenOutreach, as a command (`outsend`) and as a library.

A program that calls it in its own process (`leads.ingest.ingest`,
`send_pass.run_send_pass`, the steps in `machines/`) calls `setup()` once first.
"""
import os
import sys


def setup(*, migrate: bool = True) -> None:
    """Start Django on this package's settings and bring the store up to date.

    The settings are ``openoutsend.settings`` unless ``DJANGO_SETTINGS_MODULE`` already
    names others: a host with a Django project of its own lists these apps in it.

    Migrations run on every call and are a no-op once applied, so a fresh install is
    an ingest that works rather than a traceback about a missing table. Their narration
    goes to stderr, as everything narrated does; a migration that *fails* still raises,
    because that is a broken install and not an answer. ``migrate=False`` leaves the
    tables to a host that migrates its own database.
    """
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openoutsend.settings")
    django.setup()
    if not migrate:
        return

    from django.core.management import call_command

    call_command("migrate", verbosity=0, stdout=sys.stderr)
