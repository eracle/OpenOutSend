from __future__ import annotations

import pytest

from django.core.exceptions import ImproperlyConfigured

from openoutsend.defaults import app_settings, database, env_flag


class TestEnvFlag:
    def test_unset_keeps_the_default(self, monkeypatch):
        monkeypatch.delenv("SOME_FLAG", raising=False)
        assert env_flag("SOME_FLAG") is True
        assert env_flag("SOME_FLAG", default=False) is False

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "NO", "  no  ", "off", "OFF"])
    def test_falsy_spellings_turn_it_off(self, monkeypatch, value):
        monkeypatch.setenv("SOME_FLAG", value)
        assert env_flag("SOME_FLAG") is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything"])
    def test_anything_else_leaves_it_on(self, monkeypatch, value):
        monkeypatch.setenv("SOME_FLAG", value)
        assert env_flag("SOME_FLAG") is True


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


class TestDatabase:
    def test_unset_it_is_the_sqlite_file_under_the_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OUTSEND_DATABASE_URL", raising=False)
        monkeypatch.delenv("OUTSEND_DB", raising=False)
        monkeypatch.setenv("OUTSEND_HOME", str(tmp_path))

        db = database()

        assert db["ENGINE"] == "django.db.backends.sqlite3"
        assert db["NAME"] == str(tmp_path / "data" / "db.sqlite3")

    def test_a_url_puts_it_in_postgres(self, monkeypatch):
        monkeypatch.setenv("OUTSEND_DATABASE_URL",
                           "postgres://openoutreach:p%40ss@10.0.0.5:5432/outsend?sslmode=require")

        assert database() == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "outsend",
            "USER": "openoutreach",
            "PASSWORD": "p@ss",
            "HOST": "10.0.0.5",
            "PORT": "5432",
            "OPTIONS": {"sslmode": "require"},
        }

    @pytest.mark.parametrize("url", [
        "mysql://u:p@host/outsend",       # not Postgres
        "postgres://u:p@host/",           # no database name
        "postgres:///outsend",            # no host
    ])
    def test_a_url_it_cannot_use_is_refused(self, monkeypatch, url):
        monkeypatch.setenv("OUTSEND_DATABASE_URL", url)

        with pytest.raises(ImproperlyConfigured, match="OUTSEND_DATABASE_URL"):
            database()
