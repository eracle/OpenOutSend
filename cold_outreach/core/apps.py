# cold_outreach/core/apps.py
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "cold_outreach.core"
    label = "core"
    default_auto_field = "django.db.models.BigAutoField"
