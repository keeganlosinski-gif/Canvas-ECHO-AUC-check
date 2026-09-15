"""
Repeat transthoracic echo check.

When a provider orders a complete transthoracic echocardiogram (TTE), look for
a prior complete TTE on the patient's chart inside the lookback window. If one
exists, raise a protocol card that asks one question: is this exam needed given
the prior study on that date? The card carries a single "Yes, document reason"
button that drops a pre-filled Plan entry into the note for the provider to
finish. Snooze is the "no". The card does not block the order or infer an
answer.

The card also looks forward. If a complete TTE is already ordered and not yet
resulted, or an echo appointment is already on the schedule, the card says so,
naming the date and the provider. When the new order is signed anyway and the
existing order or appointment belongs to a different provider, that provider
gets a task saying a duplicate was ordered and their pending study may be
cancellable. The two orders live in the same chart and the two providers
usually do not know about each other.

Limited echoes (93308) are ignored on both sides: they are ordered for narrow,
usually legitimate follow-up (effusion, chemo surveillance, post-procedure) and
a complete study after a limited one is the provider getting the rest of the
information.

Clinical basis: ACC/ASE Appropriate Use Criteria for Echocardiography (2011)
rate routine surveillance TTE inside one year with no change in clinical status
as "rarely appropriate" across heart failure, valvular disease, and ventricular
function follow-up. In the audits I have read it is the most common
rarely-appropriate indication. See README.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from canvas_sdk.commands import PlanCommand
from canvas_sdk.effects import Effect
from canvas_sdk.effects.protocol_card import ProtocolCard
from canvas_sdk.effects.task import AddTask, TaskStatus
from canvas_sdk.events import EventType
from canvas_sdk.handlers import BaseHandler
from canvas_sdk.v1.data.appointment import Appointment, AppointmentProgressStatus
from canvas_sdk.v1.data.common import OrderStatus
from canvas_sdk.v1.data.imaging import ImagingOrder, ImagingReport
from canvas_sdk.v1.data.note import Note
from logger import log

# --- Configuration -----------------------------------------------------------

# How far back to look for a prior complete TTE. AUC treats "< 1 year" as the
# routine surveillance window for most indications; six months is deliberately
# tighter so the card only fires on the clearly early repeats and stays quiet
# on legitimate annual follow-up.
LOOKBACK_DAYS = 183

# Stable key so repeat firings update one card instead of stacking new ones.
CARD_KEY = "echo-auc-repeat-tte"

# What counts as a complete transthoracic echo. CPT 93306 (complete with
# Doppler and color) and 93307 (complete without Doppler). Matched against the
# order's imaging text, which in Canvas is a string that usually carries both
# a description and a code.
_TTE_PATTERN = re.compile(
    r"(9330[67]|transthoracic|\bTTE\b|echocardiogra(m|phy))",
    re.IGNORECASE,
)

# Studies that contain the word "echo" but are not a complete TTE and should
# neither trigger the card nor count as a prior: limited echo (93308,
# whose CPT descriptor reads "follow-up or limited study"), stress echo (93350/93351), transesophageal (93312-93318), fetal,
# intracardiac.
_EXCLUDE_PATTERN = re.compile(
    r"(93308|limited|stress|transesophageal|\bTEE\b|9335[01]|9331[2-8]|fetal|intracardiac|\bICE\b)",
    re.IGNORECASE,
)

# Order statuses that mean the order was withdrawn and should not count.
_DEAD_ORDER_STATUSES = {OrderStatus.CANCELLED}

# Order statuses that mean the study is still in motion: ordered, not yet done.
_PENDING_ORDER_STATUSES = {
    OrderStatus.PROPOSED, OrderStatus.DRAFT, OrderStatus.PLANNED, OrderStatus.REQUESTED,
    OrderStatus.RECEIVED, OrderStatus.ACCEPTED, OrderStatus.IN_PROGRESS,
}

# A pending order older than this is treated as abandoned rather than pending.
PENDING_MAX_AGE_DAYS = 365

# Appointment statuses that mean the visit is not going to happen.
_DEAD_APPT_STATUSES = {AppointmentProgressStatus.CANCELLED, AppointmentProgressStatus.NOSHOWED}

TASK_LABELS = ["echo-auc", "duplicate-order"]


def is_tte(text: str | None) -> bool:
    """True if the imaging description reads as a complete transthoracic echo."""
    if not text:
        return False
    if _EXCLUDE_PATTERN.search(text):
        return False
    return bool(_TTE_PATTERN.search(text))


def _staff_name(staff: Any) -> str:
    if not staff:
        return "another provider"
    first = getattr(staff, "first_name", "") or ""
    last = getattr(staff, "last_name", "") or ""
    return (f"{first} {last}").strip() or "another provider"


# --- Handler ------------------------------------------------------------------


class RepeatEchoCheck(BaseHandler):
    """Raise a protocol card when a TTE is ordered and a prior TTE exists in the window."""

    RESPONDS_TO = [
        EventType.Name(EventType.IMAGING_ORDER_COMMAND__POST_UPDATE),
        EventType.Name(EventType.IMAGING_ORDER_COMMAND__POST_COMMIT),
    ]

    # ---- context helpers -----------------------------------------------------

    def _fields(self) -> dict[str, Any]:
        return (self.event.context or {}).get("fields") or {}

    def _ordered_imaging_text(self) -> str:
        """The imaging selected on the order command, as text."""
        fields = self._fields()
        raw = fields.get("image", fields.get("image_code"))
        if isinstance(raw, dict):
            return str(raw.get("text") or raw.get("label") or raw.get("value") or "")
        return str(raw or "")

    def _note_uuid(self) -> str | None:
        note = (self.event.context or {}).get("note") or {}
        return note.get("uuid") or (self.event.context or {}).get("note_id")

    def _patient_id(self) -> str | None:
        ctx = self.event.context or {}
        patient = ctx.get("patient") or {}
        if isinstance(patient, dict) and patient.get("id"):
            return str(patient["id"])
        if ctx.get("patient_id"):
            return str(ctx["patient_id"])
        note_uuid = self._note_uuid()
        if note_uuid:
            note = Note.objects.filter(id=note_uuid).select_related("patient").first()
            if note and note.patient:
                return str(note.patient.id)
        return None

    def _current_provider(self) -> Any:
        """The staff member signing the current order: the note's provider."""
        note_uuid = self._note_uuid()
        if not note_uuid:
            return None
        note = Note.objects.filter(id=note_uuid).select_related("provider").first()
        return note.provider if note else None

    def _is_commit(self) -> bool:
        return self.event.type == EventType.IMAGING_ORDER_COMMAND__POST_COMMIT

    # ---- chart lookup ------------------------------------------------------

    def _pending_tte_order(self, patient_id: str, exclude_note_uuid: str | None) -> ImagingOrder | None:
        """A complete-TTE order that is still open (no result filed), newest first."""
        cutoff = datetime.now(UTC) - timedelta(days=PENDING_MAX_AGE_DAYS)
        orders = (
            ImagingOrder.objects.filter(
                patient__id=patient_id,
                status__in=_PENDING_ORDER_STATUSES,
                date_time_ordered__gte=cutoff,
                results__isnull=True,
            )
            .select_related("ordering_provider")
            .order_by("-date_time_ordered")
        )
        if exclude_note_uuid:
            orders = orders.exclude(note__id=exclude_note_uuid)
        for order in orders:
            if is_tte(order.imaging):
                return order
        return None

    def _scheduled_tte_appointment(self, patient_id: str) -> Appointment | None:
        """A future appointment on the schedule that reads as an echo, soonest first."""
        appts = (
            Appointment.objects.filter(patient__id=patient_id, start_time__gte=datetime.now(UTC))
            .exclude(status__in=_DEAD_APPT_STATUSES)
            .select_related("provider", "note_type")
            .order_by("start_time")
        )
        for appt in appts:
            label = " ".join(
                str(x) for x in (
                    getattr(getattr(appt, "note_type", None), "name", None),
                    appt.description, appt.comment,
                ) if x
            )
            if is_tte(label):
                return appt
        return None

    def _most_recent_prior_tte(
        self, patient_id: str, exclude_note_uuid: str | None
    ) -> tuple[date, str, str | None] | None:
        """Return (date, description, report_url) of the newest prior TTE in the window."""
        now = datetime.now(UTC)
        cutoff_dt = now - timedelta(days=LOOKBACK_DAYS)
        cutoff_d = cutoff_dt.date()

        candidates: list[tuple[date, str, str | None]] = []

        # A "done" prior is an order that completed or has a result filed. An
        # order still in motion is handled separately as a pending order.
        orders = ImagingOrder.objects.filter(
            patient__id=patient_id, date_time_ordered__gte=cutoff_dt
        ).exclude(status__in=_DEAD_ORDER_STATUSES)
        if exclude_note_uuid:
            orders = orders.exclude(note__id=exclude_note_uuid)
        for order in orders:
            still_open = order.status in _PENDING_ORDER_STATUSES and not order.results.exists()
            if still_open:
                continue
            if is_tte(order.imaging):
                ordered: datetime = order.date_time_ordered
                candidates.append((ordered.date(), order.imaging, None))

        reports = ImagingReport.objects.filter(
            patient__id=patient_id, junked=False, result_date__gte=cutoff_d
        )
        for report in reports:
            if is_tte(report.name):
                url = None
                try:
                    url = report.document_url
                except Exception:  # presigning needs live settings; fine without it
                    url = None
                candidates.append((report.result_date, report.name, url))

        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0], reverse=True)
        return candidates[0]

    # ---- effect --------------------------------------------------------

    def compute(self) -> list[Effect]:
        imaging_text = self._ordered_imaging_text()
        if not is_tte(imaging_text):
            return []

        patient_id = self._patient_id()
        if not patient_id:
            log.warning("[echo_auc_check] TTE ordered but no patient in context; skipping")
            return []

        note_uuid = self._note_uuid()
        pending = self._pending_tte_order(patient_id, note_uuid)
        scheduled = self._scheduled_tte_appointment(patient_id)
        prior = self._most_recent_prior_tte(patient_id, note_uuid)

        if not (pending or scheduled or prior):
            return []

        today = datetime.now(UTC).date()
        sentences: list[str] = ["Is this exam needed?"]
        anchor_date = None

        if pending:
            ordered_on = pending.date_time_ordered.date()
            sentences.append(
                f"A complete transthoracic echo is already ordered and not yet resulted "
                f"(requested {ordered_on:%d %b %Y} by {_staff_name(pending.ordering_provider)})."
            )
            anchor_date = anchor_date or ordered_on
        if scheduled:
            sentences.append(
                f"An echo is already on the schedule for {scheduled.start_time:%d %b %Y} "
                f"with {_staff_name(scheduled.provider)}."
            )
            anchor_date = anchor_date or scheduled.start_time.date()
        if prior:
            prior_date, _desc, prior_url = prior
            days_ago = (today - prior_date).days
            months_ago = max(1, round(days_ago / 30.4))
            sentences.append(
                f"A complete transthoracic echo is already on this chart from {prior_date:%d %b %Y} "
                f"({months_ago} month{'s' if months_ago != 1 else ''} ago). Under ACC/ASE appropriate "
                "use criteria, a routine repeat inside one year with no change in clinical status is "
                "rated rarely appropriate."
            )
            anchor_date = anchor_date or prior_date
        else:
            prior_url = None
        sentences.append(
            "If this exam is indicated, click Yes and finish the sentence in the note. If not, snooze this card."
        )

        reason_entry = PlanCommand(
            note_uuid=note_uuid,
            narrative=f"Complete TTE ordered with an echo already on the chart ({anchor_date:%d %b %Y}). Reason: ",
        )
        card = ProtocolCard(
            patient_id=patient_id,
            key=CARD_KEY,
            title="Transthoracic echo already on this chart",
            narrative=" ".join(sentences),
            status=ProtocolCard.Status.DUE,
            can_be_snoozed=True,
            feedback_enabled=False,
            recommendations=[reason_entry.recommend(title="Yes, this exam is needed", button="Document reason")],
        )
        if prior_url:
            card.add_recommendation(title="Open prior echo report", button="View", href=prior_url)

        effects: list[Effect] = [card.apply()]

        # On signing, tell the other provider their pending study may be cancellable.
        if self._is_commit():
            current = self._current_provider()
            current_id = str(current.id) if current else None
            other = None
            other_when = None
            if pending and pending.ordering_provider and str(pending.ordering_provider.id) != current_id:
                other = pending.ordering_provider
                other_when = f"your pending TTE order from {pending.date_time_ordered:%d %b %Y}"
            elif scheduled and scheduled.provider and str(scheduled.provider.id) != current_id:
                other = scheduled.provider
                other_when = f"the echo scheduled for {scheduled.start_time:%d %b %Y}"
            if other:
                task = AddTask(
                    patient_id=patient_id,
                    assignee_id=str(other.id),
                    title=(
                        f"Duplicate TTE ordered {today:%d %b %Y} by {_staff_name(current)}; "
                        f"{other_when} may be cancellable."
                    ),
                    status=TaskStatus.OPEN,
                    labels=TASK_LABELS,
                )
                effects.append(task.apply())
                log.info(f"[echo_auc_check] duplicate TTE signed; task sent to {_staff_name(other)}")

        log.info(
            f"[echo_auc_check] card for patient {patient_id}: "
            f"pending={bool(pending)} scheduled={bool(scheduled)} prior={bool(prior)}"
        )
        return effects
