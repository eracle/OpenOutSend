"""Sequences — a folder of step files, checked when it loads, rendered strictly."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openoutsend.core import sequences
from openoutsend.core.sequences import SequenceError, load

OPENER = "{# R1 #}\nSubject: Ten leads, free\n\nHi,\n\nStop here: {{ unsubscribe_url }}\n"


def _folder(tmp_path, **files):
    """A sequence folder holding *files* (name → body)."""
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    return tmp_path


class TestLoading:
    def test_steps_come_in_numeric_order_with_their_delays(self, tmp_path):
        folder = _folder(tmp_path, **{
            f"E{n}-1d-step.prompt.j2": "x" for n in range(1, 11)
        }, **{"E0-first.prompt.j2": "x"})

        sequence = load(folder, "s")

        assert [step.number for step in sequence.steps] == list(range(11))
        assert sequence.first.days == 0
        assert sequence.step(10).days == 1

    def test_the_delay_the_name_and_the_kind_are_read_from_the_filename(self, tmp_path):
        sequence = load(_folder(tmp_path, **{"E0-2d-hello.prompt.j2": "x"}), "s")

        assert (sequence.first.days, sequence.first.name, sequence.first.kind) == (
            2, "hello", sequences.PROMPT)

    def test_after_the_last_step_there_is_none(self, tmp_path):
        sequence = load(_folder(tmp_path, **{"E0-a.prompt.j2": "x", "E1-3d-b.prompt.j2": "y"}),
                        "s")

        assert sequence.after(0).number == 1
        assert sequence.after(1) is None

    def test_a_readme_and_other_files_are_not_steps(self, tmp_path):
        sequence = load(_folder(tmp_path, **{
            "E0-a.prompt.j2": "x", "README.md": "notes", "leads.jsonl": "{}"}), "s")

        assert len(sequence.steps) == 1

    def test_prompt_and_email_steps_mix_in_one_sequence(self, tmp_path):
        sequence = load(_folder(tmp_path, **{
            "E0-a.email.j2": OPENER, "E1-2d-b.prompt.j2": "Write again."}), "s")

        assert [step.is_email for step in sequence.steps] == [True, False]

    @pytest.mark.parametrize("files, complaint", [
        ({"E0-a.prompt.j2": "x", "intro.j2": "y"}, "not a step file name"),
        ({"E0-a.prompt.j2": "x", "E0-b.prompt.j2": "y"}, "a second E0"),
        ({"E0-a.prompt.j2": "x", "E0-a.email.j2": OPENER}, "a second E0"),
        ({"E0-a.prompt.j2": "x", "E2-3d-c.prompt.j2": "y"}, "E1 is missing"),
        ({"E1-3d-a.prompt.j2": "x"}, "E0 is missing"),
        ({"E0-a.prompt.j2": "x", "E1-b.prompt.j2": "y"}, "needs a delay"),
        ({"E0-a.prompt.j2": "x", "E1-0d-b.prompt.j2": "y"}, "needs a delay"),
        ({"E0-a.prompt.j2": "{% if %}"}, "E0-a.prompt.j2:1"),
        ({"E0-a.md": "x"}, "rename it E0-a.prompt.j2"),
        ({"E0-a.email.j2": "Hi,\n\nNo subject."}, "starts with a Subject: line"),
        ({"E0-a.email.j2": OPENER, "E1-2d-b.email.j2": OPENER}, "drop its Subject: line"),
    ])
    def test_a_malformed_folder_fails_naming_the_file(self, tmp_path, files, complaint):
        with pytest.raises(SequenceError, match=complaint):
            load(_folder(tmp_path, **files), "s")

    def test_an_empty_folder_fails(self, tmp_path):
        with pytest.raises(SequenceError, match="no step files"):
            load(tmp_path, "s")

    def test_a_missing_folder_fails(self, tmp_path):
        with pytest.raises(SequenceError, match="no such sequence folder"):
            load(tmp_path / "nope", "s")


class TestRendering:
    def test_fields_are_filled_in(self, tmp_path):
        step = load(_folder(tmp_path, **{"E0-a.prompt.j2": "Write to {{ first_name }}."}),
                    "s").first

        assert step.render(first_name="Asha") == "Write to Asha."

    def test_a_missing_field_is_an_error_not_a_blank(self, tmp_path):
        step = load(_folder(tmp_path, **{"E0-a.prompt.j2": "Write to {{ first_name }}."}),
                    "s").first

        with pytest.raises(SequenceError, match="first_name"):
            step.render()

    def test_only_a_prompt_naming_the_prompt_line_draws_one(self, tmp_path):
        folder = _folder(tmp_path, **{
            "E0-a.prompt.j2": "{{ prompt_line }}", "E1-2d-b.prompt.j2": "Plain."})
        sequence = load(folder, "s")

        assert sequence.first.uses_prompt_line
        assert not sequence.step(1).uses_prompt_line


class TestEmailSteps:
    def test_an_opener_splits_into_its_subject_and_body(self, tmp_path):
        step = load(_folder(tmp_path, **{"E0-a.email.j2": OPENER}), "s").first

        subject, body = step.opening(unsubscribe_url="https://x/u/1")

        assert subject == "Ten leads, free"
        assert body == "Hi,\n\nStop here: https://x/u/1"

    def test_a_later_step_is_all_body(self, tmp_path):
        folder = _folder(tmp_path, **{
            "E0-a.email.j2": OPENER, "E1-2d-b.email.j2": "Hi again,\n\n{{ unsubscribe_url }}"})
        step = load(folder, "s").step(1)

        assert step.written(unsubscribe_url="u") == ("", "Hi again,\n\nu")

    def test_a_later_step_opening_a_thread_needs_a_subject(self, tmp_path):
        folder = _folder(tmp_path, **{
            "E0-a.prompt.j2": "x", "E1-2d-b.email.j2": "Hi again."})
        step = load(folder, "s").step(1)

        with pytest.raises(SequenceError, match="needs a Subject: line"):
            step.opening()

    def test_an_email_never_draws_a_prompt_line(self, tmp_path):
        folder = _folder(tmp_path, **{"E0-a.email.j2": "Subject: s\n\n{{ prompt_line }}"})

        assert not load(folder, "s").first.uses_prompt_line


class TestConversation:
    """The one file that says how a folder's conversation is held."""

    def test_a_folder_without_the_file_has_none(self, tmp_path):
        assert load(_folder(tmp_path, **{"E0-a.prompt.j2": "x"}), "s").conversation is None

    def test_the_file_loads_beside_the_steps_with_no_gap_of_its_own(self, tmp_path):
        sequence = load(_folder(tmp_path, **{
            "E0-a.prompt.j2": "x", "conversation.prompt.j2": "Answer {{ first_name }}."}), "s")

        assert [step.name for step in sequence.steps] == ["a"]
        assert sequence.conversation.path == tmp_path / "conversation.prompt.j2"
        assert sequence.conversation.source == "Answer {{ first_name }}."
        assert sequence.conversation.days is None

    def test_the_gap_is_read_from_the_filename(self, tmp_path):
        sequence = load(_folder(tmp_path, **{
            "E0-a.prompt.j2": "x", "conversation-7d.prompt.j2": "Answer them."}), "s")

        assert sequence.conversation.path.name == "conversation-7d.prompt.j2"
        assert sequence.conversation.days == 7

    @pytest.mark.parametrize("files, complaint", [
        ({"conversation.prompt.j2": "y", "conversation-7d.prompt.j2": "z"},
         r"conversation\.prompt\.j2: a second conversation prompt"),
        ({"conversation-0d.prompt.j2": "y"}, r"conversation-0d\.prompt\.j2: .*at least 1d"),
        ({"conversation-7d.prompt.j2": "{% if %}"}, r"conversation-7d\.prompt\.j2:1"),
        ({"conversation.email.j2": "y"}, r"conversation\.email\.j2: not a step file name"),
        ({"conversation-7.prompt.j2": "y"}, r"conversation-7\.prompt\.j2: not a step file name"),
        ({"reply.prompt.j2": "y"}, r"reply\.prompt\.j2: not a step file name"),
    ])
    def test_a_malformed_one_fails_naming_the_file(self, tmp_path, files, complaint):
        with pytest.raises(SequenceError, match=complaint):
            load(_folder(tmp_path, **{"E0-a.prompt.j2": "x"}, **files), "s")

    def test_it_is_not_a_step_so_a_folder_holding_only_it_has_none(self, tmp_path):
        with pytest.raises(SequenceError, match="no step files"):
            load(_folder(tmp_path, **{"conversation.prompt.j2": "Answer them."}), "s")

    def test_fields_are_filled_in(self, tmp_path):
        conversation = load(_folder(tmp_path, **{
            "E0-a.prompt.j2": "x", "conversation.prompt.j2": "Answer {{ first_name }}.\n"}),
            "s").conversation

        assert conversation.render(first_name="Asha") == "Answer Asha."

    def test_a_field_the_deal_lacks_is_an_error_not_a_blank(self, tmp_path):
        conversation = load(_folder(tmp_path, **{
            "E0-a.prompt.j2": "x", "conversation-7d.prompt.j2": "Answer about {{ product }}."}),
            "s").conversation

        with pytest.raises(SequenceError, match=r"conversation-7d\.prompt\.j2: .*product"):
            conversation.render(first_name="Asha")


class TestCold:
    def test_the_shipped_one_is_a_single_opener(self, tmp_path):
        with patch.object(sequences, "operator_dir", return_value=tmp_path):
            cold = sequences.cold()

        assert [step.path.name for step in cold.steps] == ["E0-opener.prompt.j2"]

    def test_the_shipped_one_holds_its_conversation_the_built_in_way(self, tmp_path):
        with patch.object(sequences, "operator_dir", return_value=tmp_path):
            assert sequences.cold().conversation is None

    def test_an_operator_copy_wins_whole(self, tmp_path):
        (tmp_path / "cold").mkdir()
        _folder(tmp_path / "cold", **{
            "E0-mine.prompt.j2": "Mine.", "E1-2d-also.prompt.j2": "Also mine."})

        with patch.object(sequences, "operator_dir", return_value=tmp_path):
            cold = sequences.cold()

        assert [step.name for step in cold.steps] == ["mine", "also"]


class TestKey:
    """What `--sequence` turns into: the name a deal records and a later run loads."""

    def test_no_folder_is_cold(self):
        assert sequences.key(None) == sequences.COLD

    def test_a_folder_is_its_name_wherever_it_sits(self, tmp_path, monkeypatch):
        _folder(tmp_path, **{"E0-a.prompt.j2": "x"})
        monkeypatch.chdir(tmp_path.parent)

        assert sequences.key(tmp_path.name) == tmp_path.name

    def test_the_same_sequence_moved_keeps_its_name(self, tmp_path):
        """A rebuilt image puts the folder elsewhere; its deals must still be its deals."""
        old, new = tmp_path / "old" / "relaunch", tmp_path / "new" / "relaunch"
        for folder in (old, new):
            folder.mkdir(parents=True)
            _folder(folder, **{"E0-a.prompt.j2": "x"})

        before = sequences.key(str(old))
        sequences._FOLDERS.clear()  # the next run, in a fresh process

        assert sequences.key(str(new)) == before == "relaunch"

    def test_two_folders_under_one_name_in_one_run_are_refused(self, tmp_path):
        for parent in ("a", "b"):
            (tmp_path / parent / "relaunch").mkdir(parents=True)
            _folder(tmp_path / parent / "relaunch", **{"E0-a.prompt.j2": "x"})
        sequences.key(str(tmp_path / "a" / "relaunch"))

        with pytest.raises(SequenceError, match="already named"):
            sequences.key(str(tmp_path / "b" / "relaunch"))

    def test_a_folder_named_cold_is_refused(self, tmp_path):
        (tmp_path / "cold").mkdir()
        _folder(tmp_path / "cold", **{"E0-a.prompt.j2": "x"})

        with pytest.raises(SequenceError, match="built-in"):
            sequences.key(str(tmp_path / "cold"))

    def test_a_bad_folder_fails_when_it_is_named(self, tmp_path):
        _folder(tmp_path, **{"E0-a.prompt.j2": "x", "E2-1d-b.prompt.j2": "y"})

        with pytest.raises(SequenceError, match="E1 is missing"):
            sequences.key(str(tmp_path))

    def test_a_missing_folder_fails_when_it_is_named(self, tmp_path):
        with pytest.raises(SequenceError, match="no such sequence folder"):
            sequences.key(str(tmp_path / "nope"))


class TestForDeal:
    """A deal's name back to its folder, on a later run."""

    def test_the_folder_this_run_named_wins(self, tmp_path):
        folder = _folder(tmp_path, **{"E0-a.prompt.j2": "x"})
        name = sequences.key(str(folder))

        assert sequences.for_deal(SimpleNamespace(sequence=name)).first.name == "a"

    def test_a_deal_is_looked_up_by_the_name_it_records(self, tmp_path):
        name = sequences.key(str(_folder(tmp_path, **{"E0-a.prompt.j2": "x"})))

        assert sequences.for_deal(SimpleNamespace(sequence=name)) == sequences.named(name)
        assert sequences.named(sequences.COLD).name == sequences.COLD

    def test_an_unnamed_run_finds_it_in_the_operator_folder(self, tmp_path):
        (tmp_path / "relaunch").mkdir()
        _folder(tmp_path / "relaunch", **{"E0-kept.prompt.j2": "x"})

        with patch.object(sequences, "operator_dir", return_value=tmp_path):
            sequence = sequences.for_deal(SimpleNamespace(sequence="relaunch"))

        assert sequence.first.name == "kept"

    def test_a_name_with_no_folder_says_to_name_it(self, tmp_path):
        with patch.object(sequences, "operator_dir", return_value=tmp_path), \
                pytest.raises(SequenceError, match="--sequence"):
            sequences.for_deal(SimpleNamespace(sequence="relaunch"))
