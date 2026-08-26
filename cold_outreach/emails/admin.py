# cold_outreach/emails/admin.py
from django.contrib import admin

from cold_outreach.emails.models import (
    DeliveryEvent,
    FolderCoverage,
    Mailbox,
    Message,
    Thread,
)


@admin.register(Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    list_display = ("from_address", "host", "port", "daily_limit", "sent_today", "paused_today")
    search_fields = ("from_address", "username")


class DeliveryEventInline(admin.TabularInline):
    """What the world said about this send, in the order it said it."""

    model = DeliveryEvent
    fk_name = "message"
    extra = 0
    can_delete = False
    readonly_fields = ("status", "response", "smtp_code", "enhanced_status",
                       "detail", "queue_id", "reported_by", "occurred_at")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    """The log, read-only.

    Read-only on purpose: the record is what happened, and an editable record is
    a record of what somebody last thought. To change a *reading*, bump
    ``classify.CLASSIFIER_VERSION`` and let the pass re-run over the bytes.
    """

    list_display = ("sent_at", "direction", "kind", "from_address", "to_address",
                    "subject", "processed_at")
    list_filter = ("direction", "kind", "mailbox", "classifier_version")
    search_fields = ("message_id", "from_address", "to_address", "subject")
    date_hierarchy = "sent_at"
    raw_id_fields = ("thread", "owner")
    inlines = [DeliveryEventInline]
    readonly_fields = tuple(
        field.name for field in Message._meta.fields if field.name != "id")


@admin.register(Thread)
class ThreadAdmin(admin.ModelAdmin):
    list_display = ("pk", "mailbox", "created_at", "message_count")
    raw_id_fields = ("mailbox",)

    @admin.display(description="messages")
    def message_count(self, thread):
        return thread.messages.count()


@admin.register(FolderCoverage)
class FolderCoverageAdmin(admin.ModelAdmin):
    list_display = ("mailbox", "folder", "last_uid", "uidvalidity", "synced_at")
    list_filter = ("mailbox", "folder")
