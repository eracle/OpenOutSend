"""The migrations that move each deal's state from its column into the deal machine, and
then drop the step column once the state says which step a deal is owed."""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("outsend_leads", "0007_lead_timezone"), ("agentic_fsm", "0001_initial")]
AFTER = [("outsend_leads", "0008_deal_state_to_the_deal_machine")]

# The step column goes in 0009, with the library's tables already as they are today.
WITH_STEP = [("outsend_leads", "0008_deal_state_to_the_deal_machine"), ("agentic_fsm", "0002_only_real_moves")]
WITHOUT_STEP = [("outsend_leads", "0009_deal_state_is_the_sequence_step"), ("agentic_fsm", "0002_only_real_moves")]


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


# ── Dropping the step column ──


@pytest.fixture
def with_step():
    """The schema just before the step column goes; migrated back to the latest afterwards."""
    yield _migrate(WITH_STEP)
    _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


def _stepped_deal(apps, lead_id: str, state: str, step: int | None):
    """A deal of the three-state machine at *step*, with its current-state and log rows."""
    Lead = apps.get_model("outsend_leads", "Lead")
    Deal = apps.get_model("outsend_leads", "Deal")
    deal = Deal.objects.create(lead=Lead.objects.create(lead_id=lead_id), step=step)
    now = timezone.now()
    apps.get_model("agentic_fsm", "CurrentState").objects.create(
        machine="deal", entity=str(deal.pk), state=state, updated_at=now)
    apps.get_model("agentic_fsm", "LogEntry").objects.create(
        machine="deal", entity=str(deal.pk), step=None, from_state=None, to_state=state,
        decided_by="code", at=now)
    return deal


def _stored_states(apps) -> set:
    return set(apps.get_model("agentic_fsm", "CurrentState").objects.values_list("machine", "entity", "state"))


def _logged_moves(apps) -> list:
    return list(apps.get_model("agentic_fsm", "LogEntry").objects.order_by("pk").values_list(
        "machine", "entity", "step", "from_state", "to_state"))


def test_the_step_column_goes(with_step):
    assert "step" in {field.name for field in with_step.get_model("outsend_leads", "Deal")._meta.get_fields()}
    deal = _stepped_deal(with_step, "1", "Emailed", 1)

    apps = _migrate(WITHOUT_STEP)

    Deal = apps.get_model("outsend_leads", "Deal")
    assert "step" not in {field.name for field in Deal._meta.get_fields()}
    with connection.cursor() as cursor:
        described = connection.introspection.get_table_description(cursor, Deal._meta.db_table)
    columns = {column.name for column in described}
    assert "step" not in columns and "outcome" in columns
    assert Deal.objects.get(pk=deal.pk).sequence == "cold"


def test_dropping_the_step_column_moves_no_deals_state(with_step):
    """A store that held deals under the three-state machine has them placed by hand."""
    ready = _stepped_deal(with_step, "1", "Ready to Email", None)
    emailed = _stepped_deal(with_step, "2", "Emailed", 0)
    completed = _stepped_deal(with_step, "3", "Completed", 2)
    states, moves = _stored_states(with_step), _logged_moves(with_step)

    apps = _migrate(WITHOUT_STEP)

    assert _stored_states(apps) == states == {
        ("deal", str(ready.pk), "Ready to Email"),
        ("deal", str(emailed.pk), "Emailed"),
        ("deal", str(completed.pk), "Completed"),
    }
    assert _logged_moves(apps) == moves and len(moves) == 3


def test_a_deal_can_end_stopped_once_the_column_is_gone(with_step):
    Outcome = dict(with_step.get_model("outsend_leads", "Deal")._meta.get_field("outcome").choices)
    assert "stopped" not in Outcome

    apps = _migrate(WITHOUT_STEP)

    assert "stopped" in dict(apps.get_model("outsend_leads", "Deal")._meta.get_field("outcome").choices)
