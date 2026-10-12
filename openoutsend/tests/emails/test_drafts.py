"""Drafts checked before they go: placeholders filled, unknown ones and banned phrases redrafted, then held."""
import pytest

from openoutsend.core.agents.outreach import OutreachDecision
from openoutsend.core.conf import banned_phrases
from openoutsend.emails.steps.drafts import check, fill, write_checked
from openoutsend.errors import OutsendError

FIELDS = {"first_name": "Asha", "order_url": "https://buy.example.com/412"}


def _message(text: str) -> OutreachDecision:
    return OutreachDecision(action="send_message", message=text)


@pytest.fixture
def banned(tmp_path, monkeypatch):
    path = tmp_path / "banned.txt"
    path.write_text("# never these\n\nCircle back\nno-brainer\n", encoding="utf-8")
    monkeypatch.setenv("OUTSEND_BANNED_PHRASES", str(path))
    return path


def test_a_placeholder_is_filled_from_the_piped_fields():
    assert fill("Order here: {order_url}", FIELDS) == "Order here: https://buy.example.com/412"


def test_a_placeholder_naming_no_field_is_a_failure():
    assert check(None, "See {price}.", FIELDS) == ["unknown placeholder {price}"]


def test_a_banned_phrase_is_found_whatever_its_case(banned):
    assert check("s", "Happy to CIRCLE BACK, {first_name}.", FIELDS) == ['banned phrase "Circle back"']


def test_no_banned_phrases_without_the_setting(monkeypatch):
    monkeypatch.delenv("OUTSEND_BANNED_PHRASES", raising=False)

    assert banned_phrases() == ()


def test_a_banned_phrases_path_that_cannot_be_read_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTSEND_BANNED_PHRASES", str(tmp_path / "missing.txt"))

    with pytest.raises(OutsendError, match="OUTSEND_BANNED_PHRASES"):
        banned_phrases()


def test_comments_and_blank_lines_are_not_phrases(banned):
    assert banned_phrases() == ("Circle back", "no-brainer")


def test_a_failed_draft_is_redrafted_once_with_the_failure_named():
    asked = []
    drafts = iter([_message("See {price}."), _message("Order at {order_url}.")])

    def write(prompt):
        asked.append(prompt)
        return next(drafts)

    decision, failures = write_checked(write, "Write it.", FIELDS)

    assert failures == []
    assert decision.message == "Order at https://buy.example.com/412."
    assert len(asked) == 2
    assert "unknown placeholder {price}" in asked[1]


def test_a_second_failure_is_returned_not_filled():
    decision, failures = write_checked(lambda prompt: _message("See {price}."), "Write it.", FIELDS)

    assert failures == ["unknown placeholder {price}"]


def test_a_decision_that_is_not_a_message_is_not_checked():
    ended = OutreachDecision(action="mark_completed", outcome="wrong_fit")

    assert write_checked(lambda prompt: ended, "Write it.", FIELDS) == (ended, [])
