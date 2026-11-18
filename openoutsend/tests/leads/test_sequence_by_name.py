"""The migration that turns a deal's recorded folder path into the folder's name."""
from importlib import import_module

import pytest
from django.apps import apps

from openoutsend.leads.models import Deal
from openoutsend.tests.factories import LeadFactory

pytestmark = pytest.mark.django_db

by_name = import_module("openoutsend.leads.migrations.0006_deal_sequence_by_name").by_name

OLD_PATH = "/app/openoutreach-hub/openoutreach_pipeline/sequences/relaunch"


def _deal(sequence: str, lead=None) -> Deal:
    """A deal's row as the migration met it: the sequence it records, and no machine yet.

    Not started, because starting a deal reads its sequence's folder, and neither the
    old path nor `relaunch` is a folder here.
    """
    return Deal.objects.create(lead=lead or LeadFactory(), sequence=sequence)


def test_a_path_becomes_the_folders_name():
    deal = _deal(OLD_PATH)

    by_name(apps, None)

    deal.refresh_from_db()
    assert deal.sequence == "relaunch"


def test_names_and_cold_are_left_alone():
    named, cold = _deal("relaunch"), _deal("cold")

    by_name(apps, None)

    assert {Deal.objects.get(pk=named.pk).sequence, Deal.objects.get(pk=cold.pk).sequence} == {
        "relaunch", "cold"}


def test_a_lead_already_holding_the_name_keeps_both_deals_as_they_were():
    lead = LeadFactory()
    by_path = _deal(OLD_PATH, lead)
    _deal("relaunch", lead)

    by_name(apps, None)

    by_path.refresh_from_db()
    assert by_path.sequence == OLD_PATH
