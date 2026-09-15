"""
Seed the local plugin-runner SQLite database with one patient who had a TTE
about seven months ago, so `canvas emit` on the fixture event produces the card.

Usage:
  canvas run-plugins ./echo_auc_check --reset-db --db-seed-file fixtures/seed_local_db.py
  canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE.ndjson
"""

import datetime

from canvas_sdk.test_utils.factories import ImagingOrderFactory, NoteFactory
from canvas_sdk.v1.data.common import OrderStatus

# These ids are referenced by fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE.ndjson
PATIENT_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
CURRENT_NOTE_ID = "36eab35a-9484-459a-aa49-166caccbf9d8"

# The note the provider is charting in right now (no order on it yet).
current_note = NoteFactory.create()
current_note.patient.id = PATIENT_ID
current_note.patient.save()
current_note.id = CURRENT_NOTE_ID
current_note.save()

# A completed TTE order from about seven months ago, on an earlier note.
prior_note = NoteFactory.create(patient=current_note.patient)
ImagingOrderFactory.create(
    patient=current_note.patient,
    note=prior_note,
    imaging="Echocardiogram, transthoracic, complete with Doppler (93306)",
    status=OrderStatus.COMPLETED,
    date_time_ordered=datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=214),
)
print(f"seeded patient {PATIENT_ID} with a prior TTE 214 days ago")
