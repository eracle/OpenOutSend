"""**prompt lines** — the part of the opener's prompt that varies, and the log that
records which one was used.

The recording is the point. There is no scoring yet, so what these tests protect is the
property the scoring will one day need: that every opener says which line wrote it, and
that an edited line is not silently pooled with the version before it.
"""
from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from cold_outreach.core import prompt_lines
from cold_outreach.core.prompt_lines import PromptLineError, choose, load_all


def _write(directory, name, *, id=None, when="Any lead.", prompt="Do the thing."):
    path = directory / f"{name}.toml"
    path.write_text(
        f'id = "{id or name}"\nwhen = "{when}"\nprompt = """\n{prompt}\n"""\n',
        encoding="utf-8",
    )
    return path


class TestShippedLines:
    def test_every_shipped_file_parses(self):
        """A malformed file would take out the send pass, not just itself."""
        assert load_all(), "no prompt lines ship — the opener has nothing to vary"

    def test_ids_match_their_filenames(self):
        """Two names for one thing is how a log stops being readable."""
        for line in load_all().values():
            assert line.id == line.source.stem

    def test_none_is_a_mail_merge_template(self):
        """`Hi {{first_name}}` is what Instantly does; a line here describes the move."""
        for line in load_all().values():
            assert "{{" not in line.prompt, f"{line.id} carries a merge tag"


class TestIdentity:
    def test_the_digest_follows_the_text(self, tmp_path):
        """An edited line must not be pooled with the version before it."""
        _write(tmp_path, "one", prompt="First wording.")
        with patch.object(prompt_lines, "operator_dir", return_value=tmp_path):
            before = load_all()["one"].digest
            _write(tmp_path, "one", prompt="Second wording.")
            after = load_all()["one"].digest

        assert before != after

    def test_the_digest_is_stable_for_the_same_text(self, tmp_path):
        _write(tmp_path, "one", prompt="Same wording.")
        with patch.object(prompt_lines, "operator_dir", return_value=tmp_path):
            assert load_all()["one"].digest == load_all()["one"].digest


class TestResolution:
    def test_an_operator_copy_overrides_a_shipped_one(self, tmp_path):
        shipped = next(iter(sorted(load_all())))
        _write(tmp_path, shipped, prompt="Mine, not yours.")

        with patch.object(prompt_lines, "operator_dir", return_value=tmp_path):
            assert load_all()[shipped].prompt == "Mine, not yours."

    def test_a_named_line_is_returned(self):
        name = next(iter(sorted(load_all())))
        assert choose(name).id == name

    def test_an_unknown_name_is_an_error_listing_what_there_is(self):
        """Ambiguity is answered, never guessed at — the house rule everywhere else."""
        with pytest.raises(PromptLineError) as exc:
            choose("does-not-exist")

        assert "does-not-exist" in str(exc.value)
        assert next(iter(sorted(load_all()))) in str(exc.value)

    def test_a_file_missing_a_key_names_the_file_and_the_key(self, tmp_path):
        (tmp_path / "broken.toml").write_text('id = "broken"\nwhen = "x"\n', encoding="utf-8")

        with patch.object(prompt_lines, "operator_dir", return_value=tmp_path):
            with pytest.raises(PromptLineError) as exc:
                load_all()

        assert "broken.toml" in str(exc.value)
        assert "prompt" in str(exc.value)

    def test_no_lines_at_all_is_not_an_error(self, tmp_path):
        """An emptied directory must still be able to send — the opener works without one."""
        with patch.object(prompt_lines, "SHIPPED_DIR", tmp_path), \
                patch.object(prompt_lines, "operator_dir", return_value=tmp_path):
            assert choose() is None

    def test_the_draw_spreads_over_the_set(self):
        """Not a distribution test — just that it is not pinned to one line."""
        drawn = {choose(rng=random.Random(seed)).id for seed in range(40)}
        assert len(drawn) > 1
