"""
Seed the local plugin-runner SQLite database with one patient who has:
  * a completed complete TTE about four months ago (prior study),
  * a complete TTE ordered 12 days ago by a different provider, not yet resulted (pending order),
  * an echo appointment on the schedule 30 days out with that provider (scheduled study),
so `canvas emit` on the fixture events produces the card, and on the commit
fixture also the task to the other provider.

Usage:
  canvas run-plugins ./echo_auc_check --reset-db --db-seed-file fixtures/seed_local_db.py
  canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE.ndjson
  canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_COMMIT.ndjson
"""

import datetime

from canvas_sdk.test_utils.factories import (
    ImagingOrderFactory,
    NoteFactory,
    NoteTypeFactory,
    StaffFactory,
)
from canvas_sdk.v1.data.appointment import Appointment, AppointmentProgressStatus
from canvas_sdk.v1.data.common import OrderStatus

# These ids are referenced by the fixtures/*.ndjson event files.
PATIENT_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
CURRENT_NOTE_ID = "36eab35a-9484-459a-aa49-166caccbf9d8"

now = datetime.datetime.now(datetime.UTC)
TTE = "Echocardiogram, transthoracic, complete with Doppler (93306)"

# The note the provider is charting in right now (no order on it yet).
current_note = NoteFactory.create(provider=StaffFactory.create(first_name="Sam", last_name="Ortiz"))
current_note.patient.id = PATIENT_ID
current_note.patient.save()
current_note.id = CURRENT_NOTE_ID
current_note.save()
patient = current_note.patient

# Prior: a completed TTE about four months ago, on an earlier note.
ImagingOrderFactory.create(
    patient=patient, note=NoteFactory.create(patient=patient), imaging=TTE,
    status=OrderStatus.COMPLETED, date_time_ordered=now - datetime.timedelta(days=120),
)

# Pending: a TTE ordered 12 days ago by a different provider, not yet resulted.
other = StaffFactory.create(first_name="Jane", last_name="Smith")
ImagingOrderFactory.create(
    patient=patient, note=NoteFactory.create(patient=patient, provider=other), imaging=TTE,
    status=OrderStatus.REQUESTED, ordering_provider=other,
    date_time_ordered=now - datetime.timedelta(days=12),
)

# Scheduled: an echo appointment 30 days out with that provider.
Appointment.objects.create(
    patient=patient, provider=other, start_time=now + datetime.timedelta(days=30),
    duration_minutes=45, status=AppointmentProgressStatus.CONFIRMED,
    telehealth_instructions_sent=False,
    note_type=NoteTypeFactory.create(name="Echocardiogram, transthoracic, complete"),
    description="Complete TTE",
)
print(f"seeded patient {PATIENT_ID}: prior TTE 120d ago, pending TTE by Jane Smith 12d ago, echo appt in 30d")
