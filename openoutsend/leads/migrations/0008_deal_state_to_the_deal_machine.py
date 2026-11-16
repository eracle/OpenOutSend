"""Move each deal's state out of `Deal.state` into the deal machine's tables.

Every deal gets its current-state row (version 1) and one log row saying where it was
carried over from, then the column goes. The stored values are the column's own, so no
state is renamed. Reversed, the column comes back filled from the current-state rows,
and the machine's rows for deals are deleted.
"""
from django.db import migrations
from django.utils import timezone

MACHINE = "deal"
CARRIED_OVER = "carried over from the outsend_leads_deal.state column"


def carry_states_over(apps, schema_editor):
    Deal = apps.get_model("outsend_leads", "Deal")
    CurrentState = apps.get_model("agentic_fsm", "CurrentState")
    LogEntry = apps.get_model("agentic_fsm", "LogEntry")
    now = timezone.now()
    deals = list(Deal.objects.order_by("pk").values_list("pk", "state"))
    CurrentState.objects.bulk_create(
        CurrentState(machine=MACHINE, entity=str(pk), state=state, version=1, updated_at=now)
        for pk, state in deals
    )
    LogEntry.objects.bulk_create(
        LogEntry(machine=MACHINE, entity=str(pk), step=None, from_state=None, to_state=state,
                 applied=True, decided_by="code", decision=None, reason=CARRIED_OVER, at=now)
        for pk, state in deals
    )


def carry_states_back(apps, schema_editor):
    Deal = apps.get_model("outsend_leads", "Deal")
    CurrentState = apps.get_model("agentic_fsm", "CurrentState")
    LogEntry = apps.get_model("agentic_fsm", "LogEntry")
    for entity, state in CurrentState.objects.filter(machine=MACHINE).values_list("entity", "state"):
        Deal.objects.filter(pk=int(entity)).update(state=state)
    CurrentState.objects.filter(machine=MACHINE).delete()
    LogEntry.objects.filter(machine=MACHINE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("outsend_leads", "0007_lead_timezone"),
        ("agentic_fsm", "0001_initial"),
    ]
    # It writes `version`, `applied` and `reason`, which agentic_fsm's 0002 drops.
    run_before = [("agentic_fsm", "0002_only_real_moves")]

    operations = [
        migrations.RunPython(carry_states_over, carry_states_back),
        migrations.RemoveIndex(model_name="deal", name="deal_state_idx"),
        migrations.RemoveField(model_name="deal", name="state"),
    ]
