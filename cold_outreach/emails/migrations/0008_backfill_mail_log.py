# cold_outreach/emails/migrations/0008_backfill_mail_log.py
"""Rebuild the mail log from the conversation it is replacing.

Every id needed is already stored — the opener's Message-ID on the deal and each
turn's on ``ChatMessage.external_id`` — so no history is lost in the move. What
cannot be recovered is raw bytes for mail received before the mirror existed;
those rows carry their text and nothing else, and the classifier is told never to
re-read a row it has no bytes for.

Forward-only. Reversing it would mean rebuilding a table this design deleted.
"""
from django.db import migrations

# The classifier version these rows are stamped with. They were classified by the
# reader that wrote them — an outgoing row is a send, an incoming one is a reply
# somebody wrote — and with no bytes kept there is nothing for a later version to
# re-read, so stamping them current keeps a re-run from overwriting a true verdict
# with a guess.
CLASSIFIER_VERSION = 1


def backfill(apps, schema_editor):
    ChatMessage = apps.get_model("chat", "ChatMessage")
    Deal = apps.get_model("crm", "Deal")
    Message = apps.get_model("emails", "Message")
    Thread = apps.get_model("emails", "Thread")

    deals = Deal.objects.exclude(mailbox=None).order_by("pk")
    for deal in deals.iterator():
        turns = list(ChatMessage.objects.filter(deal=deal).order_by("creation_date", "pk"))
        if not turns:
            continue

        thread = Thread.objects.create(mailbox_id=deal.mailbox_id)
        for turn in turns:
            message_id = (turn.external_id or "").strip().strip("<>").strip()
            # A row with no usable id still becomes a message; it is keyed on its
            # own primary key so the unique constraint holds and the turn survives.
            message_id = message_id or f"backfill:chatmessage:{turn.pk}"
            if Message.objects.filter(
                    mailbox_id=deal.mailbox_id, message_id=message_id).exists():
                continue
            Message.objects.create(
                mailbox_id=deal.mailbox_id,
                thread=thread,
                direction="out" if turn.is_outgoing else "in",
                message_id=message_id,
                from_address="" if turn.is_outgoing else (deal.lead.email or "").lower(),
                to_address=(deal.lead.email or "").lower() if turn.is_outgoing else "",
                subject=deal.email_subject or "",
                references_ids=[],
                body_text=turn.content or "",
                sent_at=turn.creation_date,
                received_at=None if turn.is_outgoing else turn.creation_date,
                kind="outbound" if turn.is_outgoing else "human_reply",
                classified_at=turn.creation_date,
                classifier_version=CLASSIFIER_VERSION,
                processed_at=turn.creation_date,
                owner_id=turn.owner_id,
            )
        deal.thread = thread
        deal.save(update_fields=["thread"])


class Migration(migrations.Migration):

    dependencies = [
        ("emails", "0007_deliveryevent_foldercoverage_message_thread_and_more"),
        ("crm", "0021_remove_deal_email_message_id_deal_thread"),
        ("chat", "0005_alter_chatmessage_external_id"),
    ]

    # The conversation table is only dropped once its contents are in the log.
    run_before = [("chat", "0006_remove_chatmessage_uniq_deal_external_id_and_more")]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
