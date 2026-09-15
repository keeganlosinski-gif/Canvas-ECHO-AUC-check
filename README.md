# Echo AUC Check

A Canvas Medical plugin. When a provider orders a transthoracic echocardiogram and the patient already has one on the chart from the last twelve months, it puts a protocol card on the chart that names the prior study and its date, states what the appropriate use criteria say about routine repeats, and asks for the indication if the repeat is intended. It never blocks the order. It goes quiet when there is no prior echo, and it marks itself satisfied once the order carries a comment.

Spec, clinical logic, test cases, and this writeup are mine. The code was written with Claude Code. I am a cardiac sonographer, not a developer, and the point of this repo is to show what a clinician who works with a coding agent can put in front of an engineering team.

## Why

Routine repeat TTE with no change in clinical status is the single most common rarely-appropriate echo in every audit that has looked. The ACC/ASE Appropriate Use Criteria for Echocardiography (2011) rate surveillance inside one year without a change in status as rarely appropriate across heart failure, valvular disease, and ventricular function follow-up. Sonographers see the result of this every week: a patient scanned at seven months with a report that reads the same as the last one.

The interventions that have been tried are mostly education and feedback. Echo WISELY (JACC 2017) randomized 179 physicians across eight centers, gave the intervention group an AUC lecture, a decision-support app, and monthly ordering reports, and moved rarely-appropriate TTEs from 12.4% to 9.5% across about 14,700 studies. A 2023 meta-analysis pooled the quality-improvement studies and found the same modest effect. The investigators said afterward that what they wanted next was the check at the point of order, inside the EMR, because lectures fade and monthly reports arrive after the study is done.

This plugin is that check, done the small way: one fact the ordering provider may not have in front of them (there is already an echo from February), one sentence of guidance, one place to write the reason.

## How it works

The handler listens to `IMAGING_ORDER_COMMAND__POST_UPDATE` and `IMAGING_ORDER_COMMAND__POST_COMMIT`, so it runs while the provider is filling in the order and again when it is signed.

If the ordered study reads as a routine TTE (CPT 93306, 93307, 93308, or the words transthoracic, TTE, echocardiogram), it looks up the patient's imaging orders and imaging reports from the last 365 days and keeps the ones that are also TTEs. Stress echo, transesophageal, fetal, and intracardiac studies are excluded on both sides, so a stress echo does not trigger the card and a prior TEE does not count as a prior TTE. Cancelled orders do not count. An order on the current note does not count as its own prior.

If a prior exists, it returns a `ProtocolCard` keyed `echo-auc-repeat-tte`, so repeat firings update one card rather than stacking. The card is `due` by default and `satisfied` if the order's comment field has text in it, on the assumption that the comment is where a provider writes "new murmur, reassess MR." It can be snoozed. If the prior is an imaging report with a document attached, the card carries a link to open it.

If there is no prior, or the order is not an echo, the handler returns nothing.

## What was deliberately left out

It does not block. AUC are guidance, not rules, and a provider who orders a repeat at seven months usually has a reason. The card's job is to make sure the reason gets written down.

It does not parse report text to find an ejection fraction or a valve grade. That would be brittle and it is not needed for this question; the date and the study type are structured.

It does not try to score the indication against the full AUC table. That is the obvious v2 and it needs indication capture at order time, which this plugin does not add.

It cannot see echoes done at outside facilities unless they were filed into Canvas as imaging reports. That is a data-integration question, not a reason to withhold the signal that is already on the chart.

The lookback window and the code list are constants at the top of the handler, not settings. A practice that wants a different window changes one number.

## Testing

Two levels, both without a live Canvas instance.

The pytest suite runs against the SDK's own harness (`pytest-canvas`): real Django models in a throwaway SQLite database, populated with the SDK's factories, rolled back after each test. Sixteen cases cover the study-type matcher, no prior echo, prior echo outside the window, prior stress echo and TEE, cancelled prior, the current note's own order, another patient's echo, prior as an order, prior as a report, most-recent-wins, the comment-satisfies rule, patient resolved from the note when the context has no patient, and the window edge.

```
$ pytest -q
................                                                         [100%]
16 passed in 2.10s
```

The second level is Canvas's actual plugin runner, which loads the plugin into its `RestrictedPython` sandbox and executes it against a seeded local database. `fixtures/seed_local_db.py` creates one patient with a completed TTE 214 days ago; `fixtures/*.ndjson` are three imaging-order events for that patient.

```
$ canvas run-plugins ./echo_auc_check --reset-db --db-seed-file fixtures/seed_local_db.py
seeded patient a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d with a prior TTE 214 days ago
plugin-runner  Successfully loaded plugin "echo_auc_check", version 0.1.0 (1/1 handlers loaded)

$ canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE.ndjson
type: ADD_OR_UPDATE_PROTOCOL_CARD
payload: {"patient": "a1b2c3d4-...", "key": "echo-auc-repeat-tte", "data": {
  "title": "Repeat transthoracic echo ordered",
  "narrative": "A transthoracic echo is already on this chart from 13 Feb 2026 (7 months ago,
    Echocardiogram, transthoracic, complete with Doppler (93306)). Under ACC/ASE appropriate
    use criteria, routine repeat TTE inside one year with no change in clinical status is
    rated rarely appropriate. If this repeat is indicated, note the reason in the order comment.",
  "status": "due", "can_be_snoozed": true, ...}}

$ canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE_with_comment.ndjson
... "status": "satisfied" ...

$ canvas emit fixtures/IMAGING_ORDER_COMMAND__POST_UPDATE_ct_chest.ndjson
SUCCESS: No effects returned.
```

The runner caught something the unit tests did not. The first version imported `django.utils.timezone`, which the test harness allows and the plugin sandbox does not. The handler now uses `datetime.now(UTC)` from the standard library. That is the reason to test at both levels.

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

Indication capture at order time, so the card can score against the AUC table instead of asking for free text. A monthly per-provider report of TTEs ordered inside the window, which is the Echo WISELY feedback loop without the manual audit. Ingesting outside echo reports so the lookback covers studies done elsewhere.

## References

Douglas PS et al. ACCF/ASE/AHA/ASNC/HFSA/HRS/SCAI/SCCM/SCCT/SCMR 2011 Appropriate Use Criteria for Echocardiography. J Am Coll Cardiol 2011;57:1126-66.

Bhatia RS et al. Improving the Appropriate Use of Transthoracic Echocardiography: The Echo WISELY Trial. J Am Coll Cardiol 2017;70:1135-44.

Systematic review and meta-analysis of quality improvement interventions to reduce rarely appropriate echocardiograms, 2023. PubMed 37464949.

## Author

Keegan Losinski, RDCS(AE). Cardiac sonographer, four years in hospital echo labs and FDA device trials. github.com/keeganlosinski-gif. MIT license.
