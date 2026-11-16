"""The migration that moves each deal's state from its column into the deal machine."""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("outsend_leads", "0007_lead_timezone"), ("agentic_fsm", "0001_initial")]
AFTER = [("outsend_leads", "0008_deal_state_to_the_deal_machine")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


def _deal(apps, lead_id: str, state: str):
    Lead = apps.get_model("outsend_leads", "Lead")
    Deal = apps.get_model("outsend_leads", "Deal")
    return Deal.objects.create(lead=Lead.objects.create(lead_id=lead_id), state=state)


@pytest.fixture
def before():
    """The schema just before the move; migrated back to the latest afterwards."""
    yield _migrate(BEFORE)
    _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


def test_every_deal_keeps_its_state_and_logs_where_it_came_from(before):
    ready, emailed = _deal(before, "1", "Ready to Email"), _deal(before, "2", "Emailed")

    apps = _migrate(AFTER)

    CurrentState = apps.get_model("agentic_fsm", "CurrentState")
    LogEntry = apps.get_model("agentic_fsm", "LogEntry")
    assert set(CurrentState.objects.values_list("machine", "entity", "state", "version")) == {
        ("deal", str(ready.pk), "Ready to Email", 1),
        ("deal", str(emailed.pk), "Emailed", 1),
    }
    assert LogEntry.objects.filter(machine="deal", from_state=None, applied=True).count() == 2


def test_reversed_it_puts_the_states_back_in_the_column(before):
    completed = _deal(before, "1", "Completed")
    _migrate(AFTER)

    apps = _migrate(BEFORE)

    assert apps.get_model("outsend_leads", "Deal").objects.get(pk=completed.pk).state == "Completed"
    assert not apps.get_model("agentic_fsm", "CurrentState").objects.exists()
