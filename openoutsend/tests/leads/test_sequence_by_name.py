"""The migration that turns a deal's recorded folder path into the folder's name."""
from importlib import import_module

import pytest
from django.apps import apps

from openoutsend.leads.models import Deal
from openoutsend.tests.factories import DealFactory, LeadFactory

pytestmark = pytest.mark.django_db

by_name = import_module("openoutsend.leads.migrations.0006_deal_sequence_by_name").by_name

OLD_PATH = "/app/openoutreach-hub/openoutreach_pipeline/sequences/relaunch"


def test_a_path_becomes_the_folders_name():
    deal = DealFactory(sequence=OLD_PATH)

    by_name(apps, None)

    deal.refresh_from_db()
    assert deal.sequence == "relaunch"


def test_names_and_cold_are_left_alone():
    named, cold = DealFactory(sequence="relaunch"), DealFactory(sequence="cold")

    by_name(apps, None)

    assert {Deal.objects.get(pk=named.pk).sequence, Deal.objects.get(pk=cold.pk).sequence} == {
        "relaunch", "cold"}


def test_a_lead_already_holding_the_name_keeps_both_deals_as_they_were():
    lead = LeadFactory()
    by_path = DealFactory(lead=lead, sequence=OLD_PATH)
    DealFactory(lead=lead, sequence="relaunch")

    by_name(apps, None)

    by_path.refresh_from_db()
    assert by_path.sequence == OLD_PATH
