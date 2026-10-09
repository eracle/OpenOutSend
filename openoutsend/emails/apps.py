# openoutsend/emails/apps.py
from django.apps import AppConfig


class EmailsConfig(AppConfig):
    name = "openoutsend.emails"
    label = "outsend_emails"
    default_auto_field = "django.db.models.BigAutoField"
