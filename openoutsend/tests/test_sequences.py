"""Sequences — a folder of step files, checked when it loads, rendered strictly."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from openoutsend.core import sequences
from openoutsend.core.sequences import SequenceError, load


def _folder(tmp_path, **files):
    """A sequence folder holding *files* (name → body)."""
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    return tmp_path


class TestLoading:
    def test_steps_come_in_numeric_order_with_their_delays(self, tmp_path):
        folder = _folder(tmp_path, **{
            f"E{n}-1d-step.md": "x" for n in range(1, 11)
        }, **{"E0-first.md": "x"})

        sequence = load(folder, "s")

        assert [step.number for step in sequence.steps] == list(range(11))
        assert sequence.first.days == 0
        assert sequence.step(10).days == 1

    def test_the_delay_and_the_name_are_read_from_the_filename(self, tmp_path):
        sequence = load(_folder(tmp_path, **{"E0-2d-hello.md": "x"}), "s")

        assert (sequence.first.days, sequence.first.name) == (2, "hello")

    def test_after_the_last_step_there_is_none(self, tmp_path):
        sequence = load(_folder(tmp_path, **{"E0-a.md": "x", "E1-3d-b.md": "y"}), "s")

        assert sequence.after(0).number == 1
        assert sequence.after(1) is None

    def test_a_readme_and_other_files_are_not_steps(self, tmp_path):
        sequence = load(_folder(tmp_path, **{
            "E0-a.md": "x", "README.md": "notes", "leads.jsonl": "{}"}), "s")

        assert len(sequence.steps) == 1

    @pytest.mark.parametrize("files, complaint", [
        ({"E0-a.md": "x", "intro.md": "y"}, "not a step file name"),
        ({"E0-a.md": "x", "E0-b.md": "y"}, "a second E0"),
        ({"E0-a.md": "x", "E2-3d-c.md": "y"}, "E1 is missing"),
        ({"E1-3d-a.md": "x"}, "E0 is missing"),
        ({"E0-a.md": "x", "E1-b.md": "y"}, "needs a delay"),
        ({"E0-a.md": "x", "E1-0d-b.md": "y"}, "needs a delay"),
        ({"E0-a.md": "{% if %}"}, "E0-a.md:1"),
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
        step = load(_folder(tmp_path, **{"E0-a.md": "Write to {{ first_name }}."}), "s").first

        assert step.render(first_name="Asha") == "Write to Asha."

    def test_a_missing_field_is_an_error_not_a_blank(self, tmp_path):
        step = load(_folder(tmp_path, **{"E0-a.md": "Write to {{ first_name }}."}), "s").first

        with pytest.raises(SequenceError, match="first_name"):
            step.render()

    def test_only_a_step_naming_the_prompt_line_draws_one(self, tmp_path):
        folder = _folder(tmp_path, **{"E0-a.md": "{{ prompt_line }}", "E1-2d-b.md": "Plain."})
        sequence = load(folder, "s")

        assert sequence.first.uses_prompt_line
        assert not sequence.step(1).uses_prompt_line


class TestCold:
    def test_the_shipped_one_is_a_single_opener(self, tmp_path):
        with patch.object(sequences, "operator_dir", return_value=tmp_path):
            cold = sequences.cold()

        assert [step.path.name for step in cold.steps] == ["E0-opener.md"]

    def test_an_operator_copy_wins_whole(self, tmp_path):
        (tmp_path / "cold").mkdir()
        _folder(tmp_path / "cold", **{"E0-mine.md": "Mine.", "E1-2d-also.md": "Also mine."})

        with patch.object(sequences, "operator_dir", return_value=tmp_path):
            cold = sequences.cold()

        assert [step.name for step in cold.steps] == ["mine", "also"]


class TestKey:
    """What `--sequence` turns into: the name a deal records and a later run loads."""

    def test_no_folder_is_cold(self):
        assert sequences.key(None) == sequences.COLD

    def test_a_folder_is_its_absolute_path(self, tmp_path, monkeypatch):
        _folder(tmp_path, **{"E0-a.md": "x"})
        monkeypatch.chdir(tmp_path.parent)

        assert sequences.key(tmp_path.name) == str(tmp_path.resolve())

    def test_a_bad_folder_fails_when_it_is_named(self, tmp_path):
        _folder(tmp_path, **{"E0-a.md": "x", "E2-1d-b.md": "y"})

        with pytest.raises(SequenceError, match="E1 is missing"):
            sequences.key(str(tmp_path))

    def test_a_missing_folder_fails_when_it_is_named(self, tmp_path):
        with pytest.raises(SequenceError, match="no such sequence folder"):
            sequences.key(str(tmp_path / "nope"))
