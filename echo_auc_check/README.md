# Echo AUC Check

A Canvas Medical plugin for one ordering problem: complete transthoracic echoes that get ordered when one was done recently, is already ordered, or is already on the schedule.

When a provider orders a complete TTE, the plugin checks the chart for a complete TTE in the last six months, a complete TTE ordered and not yet resulted, and an echo appointment in the future. If it finds any of those, a protocol card asks whether this exam is needed and names what it found, with dates and the ordering provider. One button drops a pre-filled Plan entry into the note for the provider to finish. Snooze is the no. If the order is signed anyway and the existing study belongs to a different provider, that provider gets a task saying their pending study may be cancellable. Nothing in the card blocks the order.

Spec, clinical logic, test cases, and this writeup are mine. Claude Code wrote the code. I am a cardiac sonographer.

## Why

Routine repeat TTE with no change in clinical status is the most common rarely-appropriate echo in the audits I have read. The ACC/ASE Appropriate Use Criteria for Echocardiography (2011) rate surveillance inside one year without a change in status as rarely appropriate across heart failure, valvular disease, and ventricular function follow-up. Sonographers see the result every week: a patient scanned at four months with a report that reads the same as the last one.

The fixes that have been tried are mostly education and feedback. Echo WISELY (JACC 2017) randomized 179 physicians across eight centers, gave the intervention group an AUC lecture, a decision-support app, and monthly ordering reports, and moved rarely-appropriate TTEs from 12.4% to 9.5% across about 14,700 studies. A 2023 meta-analysis pooled the quality-improvement studies and found the same modest effect. The Echo WISELY investigators said afterward that the next step was a check at the point of order, inside the EMR, because lectures fade and monthly reports arrive after the study is done.

This plugin puts the facts the ordering provider may not have in front of them (there is already a complete echo from May, someone else ordered one twelve days ago, there is one on the schedule next month) on the chart at the moment the order is placed, and asks one question.

The forward-looking part comes from the hospital side. A patient is admitted, the inpatient team orders an echo, and nobody on that team can see that cardiology has an outpatient echo booked for next month. Or the outpatient study is booked and a second provider orders another one without knowing. The two orders live in the same chart and the two providers usually do not know about each other. The card shows the second orderer what already exists. If they proceed, the first orderer finds out, because their study is the one that may now be unnecessary.

## How it works

The handler listens to `IMAGING_ORDER_COMMAND__POST_UPDATE` and `IMAGING_ORDER_COMMAND__POST_COMMIT`, so it runs while the provider is filling in the order and again when it is signed.

If the ordered study reads as a complete TTE (CPT 93306 or 93307, or the words transthoracic, TTE, echocardiogram without a qualifier), it looks up the patient's imaging orders and imaging reports from the last 183 days and keeps the ones that are also complete TTEs. Limited echoes (93308), stress echo, transesophageal, fetal, and intracardiac studies are excluded on both sides: a limited echo does not trigger the card, and a prior limited echo does not count as a prior. Cancelled orders do not count. An order on the current note does not count as its own prior.

Limited echoes are excluded because in practice they are ordered for narrow, usually legitimate follow-up (a pericardial effusion, chemotherapy surveillance, a post-procedure check), and a complete study after a limited one is the provider getting the rest of the information rather than repeating it. Six months rather than twelve is deliberate: the card should fire on the clearly early repeats and stay out of the way of annual follow-up.

It then checks two more things. A pending order is a complete-TTE `ImagingOrder` in an open status (requested, accepted, in progress, and so on) with no imaging report filed against it, no older than a year; those are treated as "already ordered," not as priors. A scheduled study is a future `Appointment` that has not been cancelled or no-showed and whose note type, description, or comment reads as an echo, which covers practices that perform echoes in-house.

If any of the three exist, it returns a `ProtocolCard` keyed `echo-auc-repeat-tte`, so repeat firings update one card rather than stacking. The narrative names the pending order first, since that is the most actionable, then the scheduled study, then the prior with the AUC sentence. The card carries one recommendation, "Yes, this exam is needed," whose button originates a Plan command in the current note pre-filled with "Complete TTE ordered with an echo already on the chart (03 Sep 2026). Reason: " for the provider to finish. The reason ends up in the chart where an auditor can find it. If the provider does not click, the card stays due until snoozed. If the prior is an imaging report with a document attached, the card also carries a link to open it.

The plugin never infers an answer. An earlier version marked the card satisfied when the order's comment field had text in it. That was dropped because the comment field is also where "call patient with results" goes.

On `POST_COMMIT`, meaning the order was signed, one more effect can fire. If a pending order or scheduled study exists and its provider is not the provider signing this note, an `AddTask` goes to that provider: "Duplicate TTE ordered 15 Sep 2026 by Sam Ortiz; your pending TTE order from 03 Sep 2026 may be cancellable." Same provider on both, no task; the card already told them. Nothing fires on `POST_UPDATE`, so a provider who opens the order, sees the card, and changes their mind never generates a task.

If there is nothing to report, or the order is not a complete echo, the handler returns nothing.

## What was deliberately left out

It does not block. AUC are guidance, and a provider who orders a repeat at four months usually has a reason. The card exists to get that reason into the chart.

It does not parse report text to find an ejection fraction or a valve grade. That would be brittle and it is not needed for this question; the date and the study type are structured.

It does not try to score the indication against the full AUC table. Canvas's indication field on an imaging order is an ICD-10 picker, and a repeat echo for chronic heart failure carries the same I50.22 as the last one; the code cannot say "worse," which is the only thing that makes a repeat appropriate. So the plugin asks the provider instead of reading the code.

It does not record the yes/no itself. The "yes" lands as a note entry because that is where the reason belongs and because it needs no extra plumbing. Recording the click and resolving the card on it would need the plugin to expose an API endpoint and store state; that is listed under Next.

It cannot see echoes done at outside facilities unless they were filed into Canvas as imaging reports. Covering outside studies would need a data-integration step.

The lookback window and the code list are constants at the top of the handler. Changing the window means editing one constant.

## Testing

Two levels, both without a live Canvas instance.

The pytest suite runs against the SDK's own harness (`pytest-canvas`): real Django models in a throwaway SQLite database, populated with the SDK's factories, rolled back after each test. Thirty-two cases in four groups.

The study matcher: complete TTE codes and wording match; limited echo, stress echo, TEE, fetal echo, and non-echo studies do not.

Silence: no prior, prior outside six months, limited echo ordered, prior limited echo, cancelled prior, the current note's own order, another patient's echo, stale pending order, cancelled or past appointment, non-echo appointment.

The card: prior as an order, prior as a report, most recent prior wins, pending order, pending order that has since resulted counts as a prior, scheduled echo, all three findings in one card in the right order, the Plan-command button, a comment on the order does not resolve the card, patient resolved from the note when the context has none, the window edge.

The task: sent to the other provider on commit for a pending order and for a scheduled echo; not sent while the order is still being edited, when the same provider owns both, or when only a completed prior exists.

```
$ pytest -q
................................                                         [100%]
32 passed in 2.41s
```

The second level is Canvas's actual plugin runner, which loads the plugin into its `RestrictedPython` sandbox and executes it against a seeded local database. `fixtures/seed_local_db.py` creates one patient with a completed TTE 120 days ago, a TTE ordered 12 days ago by a different provider and not yet resulted, and an echo appointment 30 days out; `fixtures/*.ndjson` are imaging-order events for that patient.

```
$ canvas run-plugins ./echo_auc_check --reset-db --db-seed-file fixtures/seed_local_db.py
seeded patient a1b2c3d4-...: prior TTE 120d ago, pending TTE by Jane Smith 12d ago, echo appt in 30d
plugin-runner  Successfully loaded plugin "echo_auc_check", version 0.3.0 (1/1 handlers loaded)

$ canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE.ndjson
type: ADD_OR_UPDATE_PROTOCOL_CARD
payload: {"patient": "a1b2c3d4-...", "key": "echo-auc-repeat-tte", "data": {
  "title": "Transthoracic echo already on this chart",
  "narrative": "Is this exam needed? A complete transthoracic echo is already ordered and not yet
    resulted (requested 03 Sep 2026 by Jane Smith). An echo is already on the schedule for
    15 Oct 2026 with Jane Smith. A complete transthoracic echo is already on this chart from
    18 May 2026 (4 months ago). Under ACC/ASE appropriate use criteria, a routine repeat inside
    one year with no change in clinical status is rated rarely appropriate. If this exam is
    indicated, click Yes and finish the sentence in the note. If not, snooze this card.",
  "recommendations": [{"title": "Yes, this exam is needed", "button": "Document reason",
    "commands": [{"command": {"type": "plan"}, "context": {"narrative":
      "Complete TTE ordered with an echo already on the chart (03 Sep 2026). Reason: ", ...}}]}],
  "status": "due", "can_be_snoozed": true, ...}}

$ canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_COMMIT.ndjson
type: ADD_OR_UPDATE_PROTOCOL_CARD
  ... same card ...
type: CREATE_TASK
payload: {"data": {"patient": {"id": "a1b2c3d4-..."}, "assignee": {"id": "<Jane Smith>"},
  "title": "Duplicate TTE ordered 15 Sep 2026 by Sam Ortiz; your pending TTE order from
    03 Sep 2026 may be cancellable.", "status": "OPEN", "labels": ["echo-auc", "duplicate-order"], ...}}

$ canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE_limited.ndjson
SUCCESS: No effects returned.

$ canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE_ct_chest.ndjson
SUCCESS: No effects returned.
```

The runner caught something the unit tests did not. The first version imported `django.utils.timezone`, which the test harness allows and the plugin sandbox does not. The handler now uses `datetime.now(UTC)` from the standard library.

What has not been done is a deploy to a live Canvas instance. The one thing I would confirm there first is the exact shape of `fields.image` and `fields.comment` in the `POST_UPDATE` context, which the handler reads defensively (dict with `text`, or plain string) because the public docs do not show a payload example for this command.

## Running it

```
pip install "canvas[test-utils]" pytest-canvas
pytest -q
canvas run-plugins ./echo_auc_check --reset-db --db-seed-file fixtures/seed_local_db.py
canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE.ndjson
canvas validate-manifest ./echo_auc_check
canvas install ./echo_auc_check   # against a configured instance
```

## Next

- Record the yes/no on the card through a plugin API endpoint, so the card resolves on the click and the answer is queryable.
- Give the task to the other provider a one-click cancel of their pending order.
- A monthly per-provider report of complete TTEs ordered inside the window and how each card was answered, which is the Echo WISELY feedback loop without the manual audit.
- Ingest outside echo reports so the lookback covers studies done elsewhere.

## References

Douglas PS et al. ACCF/ASE/AHA/ASNC/HFSA/HRS/SCAI/SCCM/SCCT/SCMR 2011 Appropriate Use Criteria for Echocardiography. J Am Coll Cardiol 2011;57:1126-66.

Bhatia RS et al. Improving the Appropriate Use of Transthoracic Echocardiography: The Echo WISELY Trial. J Am Coll Cardiol 2017;70:1135-44.

Tao et al. The use of quality improvement interventions in reducing rarely appropriate echocardiograms: a systematic review and meta-analysis. Echocardiography 2023. doi:10.1111/echo.15653.

## Author

Keegan Losinski, RDCS(AE). Cardiac sonographer, four years in hospital echo labs and FDA device trials. github.com/keeganlosinski-gif. MIT license.
