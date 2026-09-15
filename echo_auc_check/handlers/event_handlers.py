"""
Repeat transthoracic echo check.

When a provider orders a transthoracic echocardiogram (TTE), look for a prior
TTE on the patient's chart inside the lookback window. If one exists, raise a
protocol card that names the prior study and its date, states the appropriate
use criteria position on routine repeat TTE, and asks for the indication to be
documented if the repeat is intended. The card never blocks the order. It
resolves itself when the order carries a comment, on the assumption that the
comment is where the indication goes.

Clinical basis: ACC/ASE Appropriate Use Criteria for Echocardiography (2011)
rate routine surveillance TTE inside one year with no change in clinical status
as "rarely appropriate" across heart failure, valvular disease, and ventricular
function follow-up. In every published audit that is the single most common
rarely-appropriate indication. See README.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from canvas_sdk.effects import Effect
from canvas_sdk.effects.protocol_card import ProtocolCard
from canvas_sdk.events import EventType
from canvas_sdk.handlers import BaseHandler
from canvas_sdk.v1.data.common import OrderStatus
from canvas_sdk.v1.data.imaging import ImagingOrder, ImagingReport
from canvas_sdk.v1.data.note import Note
from logger import log

# --- Configuration -----------------------------------------------------------

# How far back to look for a prior TTE. AUC treats "< 1 year" as the routine
# surveillance window for most indications.
LOOKBACK_DAYS = 365

# Stable key so repeat firings update one card instead of stacking new ones.
CARD_KEY = "echo-auc-repeat-tte"

# What counts as a transthoracic echo. CPT 93306 (complete with Doppler and
# color), 93307 (complete without Doppler), 93308 (limited/follow-up).
# Matched against the order's imaging text, which in Canvas is a string that
# usually carries both a description and a code.
_TTE_PATTERN = re.compile(
    r"(9330[678]|transthoracic|\bTTE\b|echocardiogra(m|phy))",
    re.IGNORECASE,
)

# Studies that contain the word "echo" but are not a routine TTE and should not
# trigger or count as a prior: stress echo (93350/93351), transesophageal
# (93312-93318), fetal, intracardiac.
_EXCLUDE_PATTERN = re.compile(
    r"(stress|transesophageal|\bTEE\b|9335[01]|9331[2-8]|fetal|intracardiac|\bICE\b)",
    re.IGNORECASE,
)

# Order statuses that mean the order was withdrawn and should not count.
_DEAD_ORDER_STATUSES = {OrderStatus.CANCELLED}


def is_tte(text: str | None) -> bool:
    """True if the imaging description reads as a routine transthoracic echo."""
    if not text:
        return False
    if _EXCLUDE_PATTERN.search(text):
        return False
    return bool(_TTE_PATTERN.search(text))


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

    def _order_comment(self) -> str:
        fields = self._fields()
        for key in ("comment", "additional_details"):
            value = fields.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

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

    # ---- chart lookup ------------------------------------------------------

    def _most_recent_prior_tte(
        self, patient_id: str, exclude_note_uuid: str | None
    ) -> tuple[date, str, str | None] | None:
        """Return (date, description, report_url) of the newest prior TTE in the window."""
        now = datetime.now(UTC)
        cutoff_dt = now - timedelta(days=LOOKBACK_DAYS)
        cutoff_d = cutoff_dt.date()

        candidates: list[tuple[date, str, str | None]] = []

        orders = ImagingOrder.objects.filter(
            patient__id=patient_id, date_time_ordered__gte=cutoff_dt
        ).exclude(status__in=_DEAD_ORDER_STATUSES)
        if exclude_note_uuid:
            orders = orders.exclude(note__id=exclude_note_uuid)
        for order in orders:
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

        prior = self._most_recent_prior_tte(patient_id, self._note_uuid())
        if prior is None:
            return []

        prior_date, prior_desc, prior_url = prior
        days_ago = (datetime.now(UTC).date() - prior_date).days
        months_ago = max(1, round(days_ago / 30.4))
        comment = self._order_comment()

        narrative = (
            f"A transthoracic echo is already on this chart from {prior_date:%d %b %Y} "
            f"({months_ago} month{'s' if months_ago != 1 else ''} ago"
            f"{', ' + prior_desc.strip() if prior_desc and len(prior_desc) < 80 else ''}). "
            "Under ACC/ASE appropriate use criteria, routine repeat TTE inside one year "
            "with no change in clinical status is rated rarely appropriate. "
            "If this repeat is indicated, note the reason in the order comment."
        )

        card = ProtocolCard(
            patient_id=patient_id,
            key=CARD_KEY,
            title="Repeat transthoracic echo ordered",
            narrative=narrative,
            status=ProtocolCard.Status.SATISFIED if comment else ProtocolCard.Status.DUE,
            can_be_snoozed=True,
            feedback_enabled=False,
        )
        if prior_url:
            card.add_recommendation(title="Open prior echo report", button="View", href=prior_url)

        log.info(
            f"[echo_auc_check] prior TTE {days_ago}d ago for patient {patient_id}; "
            f"status={'satisfied' if comment else 'due'}"
        )
        return [card.apply()]
