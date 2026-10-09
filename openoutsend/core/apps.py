# openoutsend/core/apps.py
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "openoutsend.core"
    # Namespaced: OpenOutreach hosts this app alongside OpenOutFind's own `core`
    # in one registry, and two apps cannot share a label.
    label = "outsend_core"
    default_auto_field = "django.db.models.BigAutoField"
