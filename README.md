# One User, One Agent

## Overview

This project implements a deterministic per-viewer decision agent and replay pipeline for the synthetic OTT data pack. It reads historical viewer events and profile snapshots, evaluates configured goals, and—when the viewer is assigned to the agent arm and a measurable eligible goal exists—publishes a validated personalized home page. It records decisions and no-action reasons, simulates post-replay behavior, independently verifies goal outcomes, and compares randomized assignment arms.

The current implementation uses a fixed mock model provider. It is a runnable replay and policy-validation exercise, not a production recommendation service or a real-world impact study.

## Problem statement and objectives

The assignment asks for decisions that are specific to one viewer, grounded in the supplied evidence, and constrained by eligibility, catalogue availability, assignment, and policy. This implementation aims to:

- Persist event history and one current goal instance per viewer.
- Select only goals with a measurable success signal, record a deadline, and abstain when no measurable goal is eligible.
- Validate home-page content against app dialect, availability windows, row rules, and duplicate-content restrictions.
- Process replay inputs in delivery order, deduplicate events, and record a decision for each unique trigger.
- Apply holdout, replay controls, and injected model faults without publishing an action when a fallback is required.
- Evaluate outcomes separately from actions and report a randomized treatment-versus-holdout comparison.

## Key features

- SQLite-backed event, goal, decision, action, fault, and outcome state.
- Idempotent event ingestion by `event_id`.
- Historical event ingestion and delivery-time ordering of replay controls, live events, and triggers.
- Goal eligibility checks for the supported measurable goals; G10 and G11 are not assigned as actionable goals.
- Catalogue-aware home-page validation and a second availability/dialect check at publication time.
- Decision records with profile/event provenance summaries, conflict resolution, goal checks/deadlines, assumptions, and explicit confidence-estimation status.
- Configurable per-decision ceilings of 8 model calls and 20 tool calls.
- Holdout abstention, duplicate-trigger handling, active-page suppression, and deterministic fault fallbacks.
- JSONL and JSON outputs for decisions, actions, simulator outcomes, replay metrics, and evaluations.
- A standard-library simulator and a standard-library `unittest` suite.

## System workflow and decision-making

1. **Load inputs.** The replay reads `data/events.jsonl`, viewer profiles, catalogue and episode data, goal/intervention/pricing configuration, randomized replay arms, and replay control/live/trigger/fault files.
2. **Build durable history.** Historical events are ingested in batches into SQLite. The database stores event payloads and their source; duplicate IDs do not add another payload. The latest profile row is selected by `updated_at` with deterministic tie-break fields. Subscription status can be updated from later purchase, renewal, or lapse events.
3. **Order the replay.** Controls, live events, and app-open/login triggers are sorted by delivery/receipt time. Controls are ordered before live events, and live events before triggers at the same time. New live events are added to the viewer history before later triggers.
4. **Evaluate the trigger.** For each unique trigger, the agent checks the randomized arm, reuses an unexpired open goal or evaluates goals in configured order, and records a no-action reason when it cannot proceed. Decisions include checks, profile source/timestamps and conflicts, goal deadline/success condition when a goal exists, assumptions, call counts, and an explicit confidence status. The mock does not estimate outcome probability; confidence is recorded as `not_estimated`. Supported goal windows start when the goal is committed. G10 lacks a measurable predicate and G11 depends on an absent `tv_app_open` signal, so neither is offered as a measurable goal.
5. **Choose and validate an action.** The mock provider returns the fixed recommendation `home_page`, unless an injected fault applies. The agent constructs a page using titles in the trigger's current app dialect, checks catalogue availability and page constraints, and validates again at publication time. Holdout viewers do not receive actions. The current action path emits personalized home pages only; it does not send push, WhatsApp, or in-app messages.
6. **Record and evaluate.** Decisions, actions, faults, and metrics are persisted and exported. When simulation is enabled, the simulator generates synthetic future events for triggered viewers. The independent evaluator checks those events against goal conditions and compares outcomes by randomized arm, not by whether a viewer was served.

The G03 correction is applied by the replay harness to the in-memory goal rule and open G03 goal records. The evaluator uses the stored goal instance's corrected success event and condition. Dependency degradation and model faults are represented as replayed fallback decisions; their simulated delays are not added as actual elapsed processing time.

## Project structure

```text
.
├── app/
│   ├── agent.py                 # Goals, profile/history logic, policy checks, decisions
│   ├── database.py              # SQLite schema and persistence
│   ├── evaluation.py            # Outcome verification and randomized comparisons
│   └── replay.py                # Replay orchestration, CLI, and exports
├── config/
│   ├── goals.json
│   ├── interventions.json
│   ├── messaging_policy.md
│   └── pricing.json
├── data/
│   ├── catalogue.csv
│   ├── episodes.csv
│   ├── events.jsonl
│   └── profiles_snapshot.csv
├── pilot/
│   ├── pilot_assignment.csv
│   └── pilot_outcomes.csv
├── replay/
│   ├── arms.csv
│   ├── control_events.jsonl
│   ├── live_events.jsonl
│   ├── model_faults.jsonl
│   └── triggers.jsonl
├── sample_run/
│   ├── actions.jsonl
│   ├── decisions.jsonl
│   ├── evaluation_report.json
│   ├── fault_results.jsonl
│   ├── FINAL_REPORT.md
│   ├── pilot_evaluation.json
│   ├── replay_metrics.json
│   └── simulator/
│       ├── outcome_events.jsonl
│       └── simulator_report.json
├── simulator/
│   └── simulate_outcomes.py
├── tests/
│   └── test_agent.py
├── process_event.py
├── run_replay.py
├── AI_COLLABORATION.md          # Assistance scope and verification record
├── ARCHITECTURE.md              # Components, state, and replay lifecycle
├── KNOWN_LIMITATIONS.md
├── result.md                   # Final assignment compliance audit
└── .venv/                      # Existing development virtual environment
```

`agent.db` is not currently present. The application creates the SQLite schema automatically at the selected database path when run; the default is the project-root `agent.db`. `agent_backup.db` exists but is not read by the application or replay.

## Technology stack

- Python; application and supplied simulator use the Python standard library.
- SQLite for durable local state.
- JSON/JSONL and CSV for configuration, source data, replay inputs, and outputs.
- `unittest` for automated tests.

The existing project virtual environment is used in the commands below. No third-party package installation is required by the documented workflow.

## Prerequisites and setup

1. Open PowerShell in the project root.
2. Confirm the existing virtual environment is available:

   ```powershell
   Test-Path .\.venv\Scripts\python.exe
   ```

3. Use that interpreter for replay, simulator, and test commands. No separate dependency-install command is needed for the current standard-library implementation.

## Run the application/replay

The full replay command is:

```powershell
.\.venv\Scripts\python.exe run_replay.py
```

By default, `run_replay.py` uses the project root as the pack, writes to `sample_run`, uses or creates `agent.db`, and runs the simulator and outcome evaluator. **This command can overwrite generated files in `sample_run` and will create/update the selected database.** To preserve the checked-in/local sample results, use separate output and database paths:

```powershell
.\.venv\Scripts\python.exe run_replay.py --out sample_run\local_run --db sample_run\local_run\agent_state.db
```

The supported replay options are `--pack`, `--out`, `--db`, `--seed`, `--latency-budget-ms`, and `--no-simulate`. For example, `--no-simulate` skips simulator execution and the corresponding outcome evaluation; it still performs replay and pilot evaluation. The output path may be created automatically, and files with matching names in that directory are replaced by a run.

The standalone `process_event.py` reads `data/events.jsonl` and ingests historical event batches using the default project-root database. It is not needed before `run_replay.py`, which performs its own historical ingestion.

## Run automated tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The latest observed run completed with **27 tests passed**. Tests cover event ID idempotency and persistence across database reopening, late-event filtering, conflicting profile resolution, replay ordering, page and message policy checks, holdout behavior, model/tool/database failure fallbacks, corrected-rule outcome verification, G07 success matching, and randomized pilot assignment evaluation. This is a local test result, not a claim that every assignment requirement is covered.

## Run the simulator

The simulator accepts `--pack`, `--actions`, `--out`, and `--seed`. To run it directly without replacing the saved simulator outputs:

```powershell
.\.venv\Scripts\python.exe simulator\simulate_outcomes.py --pack . --actions sample_run\actions.jsonl --out sample_run\simulator_manual --seed oua-practice
```

It generates `outcome_events.jsonl` and `simulator_report.json` in the chosen output directory. If that directory already contains those files, they are replaced. The simulator is deterministic for the same pack, actions, and seed; it generates synthetic behavior for triggered users only.

## Generated output files

The current saved results are under `sample_run/`:

| File | Purpose |
|---|---|
| `decisions.jsonl` | One record per unique trigger, including arm, goal ID, action/no-action outcome, reason, measured latency, and available details. A fresh replay also exports profile/history evidence, conflicts, checks, goal deadline/success condition, assumptions, confidence-estimation status, and call counts. The preserved saved sample file predates that schema extension. |
| `actions.jsonl` | Published actions in simulator-compatible JSONL format. The saved run contains home-page actions only. |
| `fault_results.jsonl` | Injected model faults and their fallback results. |
| `replay_metrics.json` | Ingestion/replay counts, decision reasons, action/fault counts, and measured local latency. |
| `pilot_evaluation.json` | Recalculated randomized pilot comparison and a separately labeled observational served/unserved summary. |
| `evaluation_report.json` | Per-user simulated outcome verification and randomized replay-arm comparison. |
| `FINAL_REPORT.md` | Submission-oriented summary of saved results and their limitations. |
| `simulator/outcome_events.jsonl` | Synthetic post-replay event stream for triggered users. |
| `simulator/simulator_report.json` | Simulator action acceptance, display, and policy-check summary. |

## Evaluation methodology and results

The saved replay evaluation compares users by their randomized assignment in `replay/arms.csv`. Its denominator includes assigned users with at least one replay trigger; inconclusive outcomes remain in each denominator. It does not compare “served” versus “unserved” as a causal estimate.

| Arm | Triggered users | Successes | Success rate |
|---|---:|---:|---:|
| Agent | 863 | 92 | 10.66% |
| Holdout | 883 | 85 | 9.63% |

The agent-minus-holdout difference is **+1.03 percentage points**. The approximate 95% confidence interval is **−1.80 to +3.86 percentage points**, which spans zero; **positive impact is not established**. The simulator provides no outcomes for **3,254 assigned-but-untriggered users**, so this is not a full-population ITT estimate. Results are synthetic simulator measurements and do not demonstrate real-world effectiveness.

The saved replay metrics report 2,679 decisions, 1,037 actions, and 5 reproduced model faults. Measured local decision latency is 40.75 ms on average and 587.776 ms maximum; 3 decisions exceed the separate 200 ms local budget. The assignment's 60-second / 5-minute thresholds are met in these saved measurements. Simulated degraded-store latency and model timeouts are recorded fallback conditions, not measured local processing delays.

## Safety, policy validation, deduplication, and failure handling

- Home-page candidates are checked against the trigger's app dialect, catalogue availability window, row type/size, and unique `content_slug` constraints. A page is checked again at its publication time.
- The event store deduplicates by `event_id`; replay controls and inputs are processed by delivery time. A repeated trigger with a persisted decision returns the prior decision rather than publishing another action.
- The randomized holdout arm receives no agent action. Unsupported or unmeasurable goals, unsupported dialects, unavailable content, active personalization, and fault/dependency conditions can result in a recorded no-action decision.
- The mock provider returns a fixed `home_page` recommendation and reproduces supplied malformed, unsupported-claim, or timeout faults without substantive recommendations. Faults use a no-action fallback.
- Message policy validators exist for consent, opt-outs, quiet hours, daily caps, approved WhatsApp templates, copy limits, title constraints, and configured prices. The active replay path does not generate messages, so these validators are not evidence of end-to-end message delivery.
- Event payloads are synthetic, but the supplied policy still instructs users to treat the pack as personal data and not upload it to services that retain or train on submitted content.

## Known limitations

- G10 has no measurable success predicate; G11 depends on an event type absent from the source stream. They are not treated as measurable actionable goals.
- G06 eligibility uses the available `lapsed` status because the brief/configuration does not define how recent a lapse must be.
- G07 does not formally define “unwatched.” The current implementation uses catalogue episode rows, observed completed episode IDs, and a three-day gap since the last observed play. Its configured success is a `play_started` event for the same `content_slug` within the goal window; an `episode_completed` event alone does not qualify.
- Conflicting profile rows use a deterministic latest-`updated_at` selection with tie-break fields. This does not resolve the underlying source-data conflict. Unknown or missing app dialects lead to abstention.
- Duplicate event IDs and device/server timestamp differences exist in the input. First persisted payload wins for a repeated ID; server receipt/delivery time is used for replay ordering.
- The mock model does not rank or recommend titles. Home-page selection is deterministic application logic. Push, WhatsApp, and in-app messages are not sent by the current path.
- Outcome simulation is limited to triggered users and models synthetic behavior. The simulator does not enforce every messaging rule or consume all replay controls/faults and the G03 correction; the replay harness applies its agent-side control handling.
- The evaluator uses the selected goal instance and simulated events. Its estimates are limited by simulator assumptions, sparse modeled behavior, and the triggered-user-only population.
- Confidence is explicitly marked `not_estimated`, because the fixed mock is not probabilistic. Event provenance is summarized by event type and source with counts/latest receipt times rather than listing every consulted event. The configured tool-call ceiling is enforced at the tool boundary, but no substantive tool is currently integrated or called by the replay.

See `KNOWN_LIMITATIONS.md` for the implementation's recorded assumptions and evidence gaps.
See `ARCHITECTURE.md` for component responsibilities and `AI_COLLABORATION.md` for the scope and verification record.

## Data and privacy considerations

The pack describes its users, events, and titles as synthetic. It contains historical events, profile snapshots, catalogue and episode records, pilot data, and replay inputs. Inputs are read from the project folders and are not rewritten by the replay. A replay writes local SQLite state and output files; choose a separate `--db` and `--out` path when preserving current state/results matters. Follow the supplied policy and do not upload the pack to services that retain or train on submitted content.

## Conclusion

The repository contains a runnable deterministic replay, policy checks, a fixed mock-model/fault boundary, a synthetic outcome simulator, and randomized-arm evaluation. The saved evaluation is transparent about uncertainty and its triggered-user scope: it does not establish a positive impact. This project is an assignment implementation and demonstration, not a production-ready personalized messaging system or evidence of real-world uplift.
