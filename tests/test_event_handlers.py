"""
Tests for the repeat-TTE check.

These run against the Canvas SDK's own test harness (pytest-canvas): real Django
models in a throwaway SQLite database, populated with the SDK's factories, with
each test rolled back afterward. No Canvas instance is needed.
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest.mock import Mock

from django.utils import timezone

from canvas_sdk.effects import EffectType
from canvas_sdk.events import EventType
from canvas_sdk.test_utils.factories import (
    ImagingOrderFactory,
    ImagingReportFactory,
    NoteFactory,
    NoteTypeFactory,
    PatientFactory,
    StaffFactory,
)
from canvas_sdk.v1.data.appointment import Appointment, AppointmentProgressStatus
from canvas_sdk.v1.data.common import OrderStatus

from echo_auc_check.handlers.event_handlers import CARD_KEY, RepeatEchoCheck, is_tte

TTE_TEXT = "Echocardiogram, transthoracic, complete with Doppler (93306)"


# --- helpers ----------------------------------------------------------------


def _event(note, image_text: str = TTE_TEXT, comment: str = "", with_patient: bool = True) -> Mock:
    """Build a mock IMAGING_ORDER_COMMAND__POST_UPDATE event the way Canvas would."""
    event = Mock()
    event.type = EventType.IMAGING_ORDER_COMMAND__POST_UPDATE
    event.target.id = "command-uuid"
    context: dict[str, Any] = {
        "fields": {
            "image": {"text": image_text, "value": 1},
            "comment": comment,
            "priority": "Routine",
        },
        "note": {"uuid": str(note.id)},
    }
    if with_patient:
        context["patient"] = {"id": str(note.patient.id)}
    event.context = context
    return event


def _prior_order(patient, days_ago: int, imaging: str = TTE_TEXT, status=OrderStatus.COMPLETED):
    note = NoteFactory.create(patient=patient)
    return ImagingOrderFactory.create(
        patient=patient,
        note=note,
        imaging=imaging,
        status=status,
        date_time_ordered=timezone.now() - datetime.timedelta(days=days_ago),
    )


def _prior_report(patient, days_ago: int, name: str = "Transthoracic echocardiogram"):
    d = timezone.now().date() - datetime.timedelta(days=days_ago)
    return ImagingReportFactory.create(
        patient=patient, name=name, result_date=d, original_date=d, s3_report_url=None
    )


def _payload(effect) -> dict[str, Any]:
    return json.loads(effect.payload)


def _commit_event(note, image_text: str = TTE_TEXT) -> Mock:
    ev = _event(note, image_text=image_text)
    ev.type = EventType.IMAGING_ORDER_COMMAND__POST_COMMIT
    return ev


def _pending_order(patient, days_ago: int, provider=None, imaging: str = TTE_TEXT):
    note = NoteFactory.create(patient=patient)
    return ImagingOrderFactory.create(
        patient=patient, note=note, imaging=imaging, status=OrderStatus.REQUESTED,
        ordering_provider=provider or StaffFactory.create(),
        date_time_ordered=timezone.now() - datetime.timedelta(days=days_ago),
    )


def _scheduled_echo(patient, days_ahead: int, provider=None, status=AppointmentProgressStatus.CONFIRMED,
                    label: str = "Echocardiogram, transthoracic, complete"):
    return Appointment.objects.create(
        patient=patient, provider=provider or StaffFactory.create(),
        start_time=timezone.now() + datetime.timedelta(days=days_ahead), duration_minutes=45,
        status=status, telehealth_instructions_sent=False,
        note_type=NoteTypeFactory.create(name=label), description=label,
    )


# --- configuration -----------------------------------------------------------


def test_handler_listens_to_imaging_order_update_and_commit() -> None:
    assert EventType.Name(EventType.IMAGING_ORDER_COMMAND__POST_UPDATE) in RepeatEchoCheck.RESPONDS_TO
    assert EventType.Name(EventType.IMAGING_ORDER_COMMAND__POST_COMMIT) in RepeatEchoCheck.RESPONDS_TO


def test_is_tte_matches_complete_transthoracic_studies() -> None:
    assert is_tte("Echocardiogram, transthoracic, complete (93306)")
    assert is_tte("93307 TTE without Doppler")
    assert is_tte("TTE")


def test_is_tte_rejects_limited_echo() -> None:
    # Limited studies are narrow follow-ups (effusion, chemo, post-procedure)
    # and must neither trigger the card nor count as a prior.
    assert not is_tte("Limited echocardiogram follow-up 93308")
    assert not is_tte("Echocardiogram, transthoracic, limited (93308)")
    assert not is_tte("TTE limited")


def test_is_tte_rejects_non_tte_and_non_echo_studies() -> None:
    assert not is_tte("Stress echocardiogram (93350)")
    assert not is_tte("Transesophageal echocardiogram (93312)")
    assert not is_tte("TEE")
    assert not is_tte("Fetal echocardiography")
    assert not is_tte("CT chest with contrast")
    assert not is_tte("")
    assert not is_tte(None)


# --- no card cases ------------------------------------------------------------


def test_no_card_when_order_is_not_an_echo() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=90)
    effects = RepeatEchoCheck(event=_event(note, image_text="CT chest with contrast")).compute()
    assert effects == []


def test_no_card_when_no_prior_echo_exists() -> None:
    note = NoteFactory.create()
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert effects == []


def test_no_card_when_prior_echo_is_outside_the_six_month_window() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=200)   # ~6.5 months
    _prior_report(note.patient, days_ago=300)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert effects == []


def test_no_card_when_limited_echo_is_ordered() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=60)
    effects = RepeatEchoCheck(
        event=_event(note, image_text="Echocardiogram, transthoracic, limited (93308)")
    ).compute()
    assert effects == []


def test_prior_limited_echo_does_not_count_as_prior() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=60, imaging="Echocardiogram, limited (93308)")
    _prior_report(note.patient, days_ago=30, name="Limited TTE, pericardial effusion follow-up")
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert effects == []


def test_no_card_when_prior_study_is_a_stress_or_transesophageal_echo() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=60, imaging="Stress echocardiogram (93350)")
    _prior_report(note.patient, days_ago=30, name="Transesophageal echocardiogram")
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert effects == []


def test_no_card_when_prior_order_was_cancelled() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=60, status=OrderStatus.CANCELLED)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert effects == []


def test_current_order_on_the_same_note_does_not_count_as_prior() -> None:
    note = NoteFactory.create()
    ImagingOrderFactory.create(
        patient=note.patient, note=note, imaging=TTE_TEXT,
        status=OrderStatus.REQUESTED, date_time_ordered=timezone.now(),
    )
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert effects == []


def test_no_card_for_another_patients_echo() -> None:
    note = NoteFactory.create()
    other = PatientFactory.create()
    _prior_order(other, days_ago=60)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert effects == []


# --- card cases ----------------------------------------------------------------


def test_card_raised_for_prior_tte_order_inside_window() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=120)  # ~4 months
    effects = RepeatEchoCheck(event=_event(note)).compute()

    assert len(effects) == 1
    assert effects[0].type == EffectType.ADD_OR_UPDATE_PROTOCOL_CARD
    payload = _payload(effects[0])
    assert payload["patient"] == str(note.patient.id)
    assert payload["key"] == CARD_KEY
    data = payload["data"]
    assert data["status"] == "due"
    assert data["can_be_snoozed"] is True
    assert data["narrative"].startswith("Is this exam needed?")
    assert "4 months ago" in data["narrative"]
    assert "rarely appropriate" in data["narrative"]


def test_card_carries_a_yes_button_that_opens_a_prefilled_plan_entry() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=120)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    recs = _payload(effects[0])["data"]["recommendations"]

    assert len(recs) == 1  # no report document on an order, so no "view" link
    yes = recs[0]
    assert yes["title"] == "Yes, this exam is needed"
    assert yes["button"] == "Document reason"
    assert len(yes["commands"]) == 1
    cmd = yes["commands"][0]
    assert cmd["command"]["type"] == "plan"
    assert cmd["context"]["narrative"].startswith("Complete TTE ordered with an echo already on the chart (")
    assert cmd["context"]["note_uuid"] == str(note.id)


def test_card_raised_for_prior_tte_report_inside_window() -> None:
    note = NoteFactory.create()
    _prior_report(note.patient, days_ago=95)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert len(effects) == 1
    data = _payload(effects[0])["data"]
    assert data["status"] == "due"
    assert "3 months ago" in data["narrative"]


def test_card_uses_the_most_recent_prior_study() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=150)
    _prior_report(note.patient, days_ago=40)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    data = _payload(effects[0])["data"]
    assert "1 month ago" in data["narrative"]


def test_card_stays_due_even_when_order_carries_a_comment() -> None:
    # The plugin never infers an indication from free text. The provider
    # answers the card's question with the button or snoozes it.
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=120)
    effects = RepeatEchoCheck(
        event=_event(note, comment="New murmur on exam, reassess MR severity")
    ).compute()
    assert len(effects) == 1
    assert _payload(effects[0])["data"]["status"] == "due"


def test_patient_is_resolved_from_note_when_context_has_no_patient() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=120)
    effects = RepeatEchoCheck(event=_event(note, with_patient=False)).compute()
    assert len(effects) == 1
    assert _payload(effects[0])["patient"] == str(note.patient.id)


def test_prior_echo_exactly_at_window_edge_counts() -> None:
    note = NoteFactory.create()
    _prior_report(note.patient, days_ago=183)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    assert len(effects) == 1


# --- forward-looking cases ------------------------------------------------------


def test_card_when_a_tte_is_already_ordered_and_not_resulted() -> None:
    note = NoteFactory.create()
    other = StaffFactory.create(first_name="Jane", last_name="Smith")
    _pending_order(note.patient, days_ago=12, provider=other)
    effects = RepeatEchoCheck(event=_event(note)).compute()

    assert len(effects) == 1
    data = _payload(effects[0])["data"]
    assert "already ordered and not yet resulted" in data["narrative"]
    assert "Jane Smith" in data["narrative"]
    assert "rarely appropriate" not in data["narrative"]  # no completed prior, so no AUC line


def test_pending_order_with_a_result_filed_is_a_prior_not_a_pending() -> None:
    note = NoteFactory.create()
    order = _pending_order(note.patient, days_ago=100)
    d = timezone.now().date() - datetime.timedelta(days=95)
    ImagingReportFactory.create(patient=note.patient, order=order, name="Transthoracic echocardiogram",
                                result_date=d, original_date=d, s3_report_url=None)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    data = _payload(effects[0])["data"]
    assert "not yet resulted" not in data["narrative"]
    assert "already on this chart from" in data["narrative"]


def test_stale_pending_order_is_ignored() -> None:
    note = NoteFactory.create()
    _pending_order(note.patient, days_ago=400)
    assert RepeatEchoCheck(event=_event(note)).compute() == []


def test_card_when_an_echo_appointment_is_already_scheduled() -> None:
    note = NoteFactory.create()
    other = StaffFactory.create(first_name="Ann", last_name="Lee")
    _scheduled_echo(note.patient, days_ahead=30, provider=other)
    effects = RepeatEchoCheck(event=_event(note)).compute()
    data = _payload(effects[0])["data"]
    assert "already on the schedule for" in data["narrative"]
    assert "Ann Lee" in data["narrative"]


def test_cancelled_or_past_appointments_do_not_count() -> None:
    note = NoteFactory.create()
    _scheduled_echo(note.patient, days_ahead=30, status=AppointmentProgressStatus.CANCELLED)
    _scheduled_echo(note.patient, days_ahead=-10)
    assert RepeatEchoCheck(event=_event(note)).compute() == []


def test_non_echo_appointment_does_not_count() -> None:
    note = NoteFactory.create()
    _scheduled_echo(note.patient, days_ahead=30, label="Office visit, follow-up")
    assert RepeatEchoCheck(event=_event(note)).compute() == []


def test_card_names_pending_order_scheduled_visit_and_prior_together() -> None:
    note = NoteFactory.create()
    _pending_order(note.patient, days_ago=5)
    _scheduled_echo(note.patient, days_ahead=20)
    _prior_order(note.patient, days_ago=120)
    data = _payload(RepeatEchoCheck(event=_event(note)).compute()[0])["data"]
    n = data["narrative"]
    assert n.startswith("Is this exam needed?")
    assert n.index("not yet resulted") < n.index("on the schedule") < n.index("rarely appropriate")


def test_signing_a_duplicate_sends_a_task_to_the_other_ordering_provider() -> None:
    note = NoteFactory.create()
    other = StaffFactory.create(first_name="Jane", last_name="Smith")
    _pending_order(note.patient, days_ago=12, provider=other)
    effects = RepeatEchoCheck(event=_commit_event(note)).compute()

    assert [e.type for e in effects] == [EffectType.ADD_OR_UPDATE_PROTOCOL_CARD, EffectType.CREATE_TASK]
    task = _payload(effects[1])["data"]
    assert task["assignee"]["id"] == str(other.id)
    assert task["patient"]["id"] == str(note.patient.id)
    assert task["title"].startswith("Duplicate TTE ordered ")
    assert "your pending TTE order from" in task["title"]
    assert "may be cancellable" in task["title"]
    assert task["labels"] == ["echo-auc", "duplicate-order"]


def test_signing_a_duplicate_of_a_scheduled_echo_tasks_that_provider() -> None:
    note = NoteFactory.create()
    other = StaffFactory.create()
    _scheduled_echo(note.patient, days_ahead=30, provider=other)
    effects = RepeatEchoCheck(event=_commit_event(note)).compute()
    task = _payload(effects[1])["data"]
    assert task["assignee"]["id"] == str(other.id)
    assert "the echo scheduled for" in task["title"]


def test_no_task_while_the_order_is_still_being_edited() -> None:
    note = NoteFactory.create()
    _pending_order(note.patient, days_ago=12)
    effects = RepeatEchoCheck(event=_event(note)).compute()  # POST_UPDATE, not commit
    assert [e.type for e in effects] == [EffectType.ADD_OR_UPDATE_PROTOCOL_CARD]


def test_no_task_when_the_same_provider_owns_both_orders() -> None:
    note = NoteFactory.create()
    _pending_order(note.patient, days_ago=12, provider=note.provider)
    effects = RepeatEchoCheck(event=_commit_event(note)).compute()
    assert [e.type for e in effects] == [EffectType.ADD_OR_UPDATE_PROTOCOL_CARD]


def test_no_task_when_only_a_completed_prior_exists() -> None:
    note = NoteFactory.create()
    _prior_order(note.patient, days_ago=120)
    effects = RepeatEchoCheck(event=_commit_event(note)).compute()
    assert [e.type for e in effects] == [EffectType.ADD_OR_UPDATE_PROTOCOL_CARD]
