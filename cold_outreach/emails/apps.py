# cold_outreach/emails/apps.py
from django.apps import AppConfig


class EmailsConfig(AppConfig):
    name = "cold_outreach.emails"
    label = "outsend_emails"
    default_auto_field = "django.db.models.BigAutoField"
