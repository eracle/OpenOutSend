from __future__ import annotations

import pytest

from cold_outreach.defaults import _env_flag, app_settings


class TestEnvFlag:
    def test_unset_keeps_the_default(self, monkeypatch):
        monkeypatch.delenv("SOME_FLAG", raising=False)
        assert _env_flag("SOME_FLAG") is True
        assert _env_flag("SOME_FLAG", default=False) is False

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "NO", "  no  "])
    def test_falsy_spellings_turn_it_off(self, monkeypatch, value):
        monkeypatch.setenv("SOME_FLAG", value)
        assert _env_flag("SOME_FLAG") is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
    def test_anything_else_leaves_it_on(self, monkeypatch, value):
        monkeypatch.setenv("SOME_FLAG", value)
        assert _env_flag("SOME_FLAG") is True


class TestAppSettings:
    def test_the_two_send_guards_default_on(self, monkeypatch):
        monkeypatch.delenv("OUTSEND_ENFORCE_WORK_HOURS", raising=False)
        monkeypatch.delenv("OUTSEND_ENFORCE_WEEKEND_PAUSE", raising=False)
        settings = app_settings()
        assert settings["OUTSEND_ENFORCE_WORK_HOURS"] is True
        assert settings["OUTSEND_ENFORCE_WEEKEND_PAUSE"] is True

    def test_either_can_be_switched_off_independently(self, monkeypatch):
        monkeypatch.setenv("OUTSEND_ENFORCE_WEEKEND_PAUSE", "false")
        monkeypatch.delenv("OUTSEND_ENFORCE_WORK_HOURS", raising=False)
        settings = app_settings()
        assert settings["OUTSEND_ENFORCE_WORK_HOURS"] is True
        assert settings["OUTSEND_ENFORCE_WEEKEND_PAUSE"] is False
