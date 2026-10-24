# One User, One Agent — Final Project Results

## Results summary

| Measure | Agent-assigned | Holdout |
|---|---:|---:|
| Triggered users evaluated | 863 | 883 |
| Verified successes | 92 | 85 |
| Success rate | 10.66% | 9.63% |
| Failed outcomes | 766 | 791 |
| Inconclusive outcomes | 5 | 7 |

**Triggered-user ITT difference:** **+1.03 percentage points** (agent minus holdout).  
**Approximate 95% confidence interval:** **−1.80 to +3.86 percentage points**.  
**Interpretation:** the interval includes zero. **Positive impact is NOT established.**

This is an intent-to-treat comparison by randomized replay assignment among users with at least one trigger only.
It is **not a full-population ITT estimate**: 3,254 assigned-but-untriggered users have no simulated outcomes and
are outside these denominators. The evaluation records no holdout actions.

## Project overview and objective

The project implements a deterministic per-user decision and replay workflow for the supplied OTT assignment. Its
objective is to use observed evidence to select at most one measurable current goal, validate a permitted action,
record an action or a reasoned abstention, and assess subsequent outcomes against randomized holdout users. The
default demo uses the supplied synthetic data and simulator; its results do not establish real-world causal impact.

## System workflow and decision logic

The CLI entry point loads historical events idempotently into SQLite, then merges replay controls, live events, and
app-open/login triggers in delivery-time order. It applies controls at their replay times, updates per-user evidence,
checks assignment and goal eligibility, and persists one current goal per user. For an eligible treatment user, it
generates and validates a home-page page against the current dialect, catalogue availability, page limits, and
duplicate-content rules. It records either the validated action or a structured no-action reason.

Replay action publication timestamps are based on trigger delivery time plus measured local decision processing
time. Goal success is checked separately after simulation; a published page is not itself a success. Duplicate
event IDs are deduplicated, and repeated trigger IDs do not create a second action.

## Dataset summary and data-quality handling

| Input | Observed size / notes |
|---|---|
| Historical events | 142,906 rows; 140,730 unique event IDs |
| Profile snapshot | 4,888 rows; duplicate user rows and missing profiles are present |
| Catalogue | 2,014 title records; includes language variants sharing content slugs |
| Episodes | 26,986 rows |
| Replay triggers | 2,769 rows; 2,679 unique trigger IDs and 90 duplicate deliveries |
| Replay live events | 607 rows; 600 were new to the clean replay database |
| Replay controls | 4 control events |
| Replay arms | Assignment is read from the supplied 5,000-user arms file |

The supplied data contains duplicate event deliveries, inconsistent phone/server timestamps, profile conflicts and
missing profile attributes, app dialect values not represented by the catalogue, and invalid title availability
windows. The workflow deduplicates by `event_id`, uses delivery/receipt timestamps rather than phone occurrence
times to order replay, selects a profile row deterministically, and abstains if the app dialect cannot be supported.
Invalid availability windows are rejected; language variants are treated as one content identity for page
uniqueness. Source records are not rewritten to conceal these issues.

## Goals and success verification

Goal definitions are loaded from `config/goals.json`; the verifier checks trusted simulator event receipt times
against each stored goal window and predicate. The run contains goal instances for G01, G02, G03, G05, G06, G07,
and G08. G10 is not measurable because it lacks a defined event/predicate; G11 relies on `tv_app_open`, which is
absent from the event stream. These are not treated as verified successes.

G03's replay correction changes success from `app_open` to `play_started` and is applied to open goal instances
as well as future ones. G06 uses the configured lapsed status without inventing a recency threshold. G07 eligibility
uses catalogue episode rows, completed episode IDs, and a three-day observed play gap because the assignment does
not define “unwatched” more precisely.

**G07 verification:** its configured success event is `play_started` for the same `content_slug` as the unfinished
series. The independent verifier checks that signal and the simulator outcome events. All 22 reported G07
successes were matched to simulator `play_started` events; no mismatched evidence was found. An
`episode_completed` event alone does not satisfy G07.

| Verified outcome category | Users |
|---|---:|
| Succeeded | 177 |
| Failed | 1,557 |
| Inconclusive | 12 |

## Intervention policy and holdout behavior

The replay action path publishes only validated personalized home pages. Pages use real catalogue entries in the
trigger's supported app dialect, respect the configured row/title and availability restrictions, avoid duplicate
`content_slug` values, and include a 120-minute expiry. A still-live page for the same dialect suppresses another
page. Unknown or unsupported dialects and insufficient valid catalogue candidates lead to abstention.

The run generated **1,037 home-page actions**. The simulator accepted all 1,037 and reported **zero policy
problems**; it reported 819 personalized page views and 14 published pages not shown. Holdout users receive no
intervention, and the evaluation reports zero holdout actions.

Push, WhatsApp, and in-app message policy checks exist, but the current action path intentionally sends none of
these message types. There are no message delivery or click claims in these results.

## Replay execution, latency, and faults

| Replay measure | Result |
|---|---:|
| Historical source rows | 142,906 |
| Historical unique payloads ingested on the clean run | 140,730 |
| Trigger rows / unique trigger IDs / duplicate trigger IDs | 2,769 / 2,679 / 90 |
| Live events seen / newly ingested | 607 / 600 |
| Controls processed | 4 |
| Decisions persisted | 2,679 |
| Actions persisted | 1,037 |
| Model faults reproduced | 5 |

Measured local decision processing latency was **40.75 ms mean** and **587.776 ms maximum** across 2,679 decisions.
The configured 200 ms budget was met for 2,676 decisions; 3 decisions exceeded it. These are actual local
measurements for this run.

Dependency degradation and timeout figures are different: the `user_data_store` degraded window produced 15
recorded read-latency fallbacks and 1 simulated read timeout; the `model_provider` outage window produced 11
no-action decisions. These are replayed/simulated conditions, not measured local processing delays. The fault
file reproduced 3 unsupported-claim cases, 1 malformed-output case, and 1 timeout. The 45-second model timeout
was represented as a safe fallback without waiting 45 seconds.

## Tests

The latest test run executed:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**Result: 21 tests passed.** Tests cover event idempotency and restart behavior, replay delivery order, G03
correction, holdout action prevention, message/page validation, fault fallback, outcome verification, G07
same-slug verification, and pilot randomized assignment evaluation.

## Outcome evaluation and randomized ITT

The replay evaluation uses `replay/arms.csv` assignment, not action receipt, and compares all triggered users in
each randomized arm. Inconclusive users remain in the denominator.

| Arm | Triggered users | Succeeded | Failed | Inconclusive | Success rate |
|---|---:|---:|---:|---:|---:|
| Agent | 863 | 92 | 766 | 5 | 10.66% |
| Holdout | 883 | 85 | 791 | 7 | 9.63% |

| ITT estimate | Value |
|---|---:|
| Absolute difference (agent − holdout) | +1.03 percentage points |
| Approximate 95% confidence interval | −1.80 to +3.86 percentage points |
| Positive impact established? | **No** |

The simulator is synthetic and produces outcomes only for triggered users. Although the point estimate is positive,
the uncertainty interval includes zero; this run therefore does not establish positive impact. The estimate should
not be generalized to all 5,000 assigned users or to real-world users.

The separate pilot evaluation is also calculated by randomized assignment: treatment 349/1,912 (18.25%) versus
holdout 387/1,979 (19.56%), a −1.31 percentage-point difference with an approximate 95% interval of −3.77 to
+1.15 percentage points.

For context only, the pilot summary reported 87/217 served users succeeded (40.1%) versus 649/3,674 other users
(17.7%), or 2.3x. This served-versus-unserved comparison is observational: “served” is a post-assignment group,
and “other users” combines holdout and unserved treatment users. It is not the randomized-arm ITT result and
must not be interpreted as causal evidence.

## Limitations, assumptions, and future improvements

- G10 and G11 remain unmeasurable from supplied signals; add validated success definitions/events before assigning
  them.
- G06 lapsed recency and G07 “unwatched” semantics are underspecified. The current deterministic interpretations
  are documented, not presented as source facts.
- Duplicate/conflicting profile rows and missing or unsupported dialects require production-quality source
  resolution; the current deterministic tie-break is only a fallback.
- Phone timestamps may be skewed or delayed. The replay uses server delivery timestamps; the original event
  timestamps are retained as provenance.
- The mock model exercises the provider/fault boundary but does not make substantive recommendations. The current
  action path uses validated home-page personalization and intentionally sends no push, WhatsApp, or in-app
  messages.
- The supplied simulator does not apply every messaging-policy check and does not itself consume all replay
  controls/faults or the G03 correction. The replay harness handles the relevant agent-side controls and injected
  model faults; simulator limitations remain.
- The synthetic simulator’s behavior and the limited triggered-user population constrain causal interpretation.
  A future evaluation should include outcomes for all assigned users or explicitly define a triggered-user
  estimand, then validate behavior on the unseen replay day and independent production-quality evidence.
- Dependency outage/degradation durations are modeled replay scenarios, not measured service telemetry.
- The simulator output files are stored under `sample_run/simulator/` (`simulator_report.json` and
  `outcome_events.jsonl`), while replay/evaluation summaries are stored directly under `sample_run/`.

## Final conclusion

The end-to-end replay, deterministic safety checks, fault fallbacks, simulator, and independent goal verification
completed successfully. G07’s 22 reported successes use its configured same-series `play_started` signal. The
triggered-user randomized comparison shows a small positive point estimate, but its approximate confidence
interval spans both negative and positive effects. **Positive impact is NOT established**, and the results do not
represent a full-population ITT estimate.
