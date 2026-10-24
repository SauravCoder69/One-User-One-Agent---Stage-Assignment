# Final “One User, One Agent” Compliance Audit

**Audit scope:** inspected current source, configuration, input data, tests, documentation, and preserved sample outputs. Ran the complete test suite and a full replay using a fresh temporary database and output directory. The existing `sample_run/` files and supplied datasets were not overwritten or edited.

## Requirement-by-requirement status

| # | Requirement | Status | Evidence and explanation |
|---:|---|---|---|
| 1 | One agent/viewer, one active goal/plan, durable history | **PASS** | `app/database.py` persists event payloads and one goal instance per user; `app/agent.py` reuses an unexpired open goal. SQLite schema is created automatically. |
| 2 | Viewer facts have source, timestamp/freshness, and explicit conflict resolution | **PARTIAL** | `app/agent.py` decisions record profile source, selected `updated_at`/`snapshot_at`, conflicting values, and deterministic selected values. `app/database.py` summarizes event sources and latest receipt times by event type. Event provenance is aggregated, not attached to every individual fact used. |
| 3 | Measurable eligible goals, deadlines, valid success signals, or recorded no-action | **PASS** | `config/goals.json`, `app/agent.py`, and `app/evaluation.py` define supported eligibility/success checks. Decisions include stored deadline and success condition when a goal exists; no-goal decisions retain goal-check reasons. G10/G11 are not assigned as measurable goals. |
| 4 | Dialect-correct, available content and at least 3 titles per home row | **PASS** | `Catalogue` and `MessagingPolicy.check_page` in `app/agent.py` validate dialect, availability, row size, duplicate `content_slug`, and revalidate at publication. `tests/test_agent.py` exercises valid/rejected pages and invalid availability windows. |
| 5 | Independent eligibility, dialect, availability, consent, caps, policy, duplicate-action checks | **PARTIAL** | Page constraints are checked before publication and again at publish time; message validation helpers cover consent, quiet hours, caps, templates, copy, and titles. Eligibility is not checked by a separate independent verifier, and message checks are not wired to an active sending path. Duplicate trigger idempotency is implemented. |
| 6 | Inspectable evidence, assumptions, confidence/reasons, conflicts, checks, latency, deadline/success signal, final status | **PARTIAL** | Fresh replay decisions include checks, profile/history source summaries, conflicts, assumptions, confidence status/reason, model/tool call counts, goal deadline/success condition, latency, outcome, and reason. Confidence is honestly `not_estimated` because the mock is not probabilistic; provenance is partly aggregated and no-goal decisions have no goal deadline/signal. Preserved `sample_run/decisions.jsonl` predates these new fields. |
| 7 | Late/out-of-order/duplicate event handling and idempotency | **PASS** | `app/replay.py` orders controls/live events/triggers by delivery/receipt time. `app/database.py` deduplicates by event ID and preserves timestamps. Tests cover duplicate ingestion/restart, repeated trigger suppression, delivery ordering, and exclusion of a late-received event from an earlier decision. |
| 8 | Safe model/tool/database failure fallback | **PARTIAL** | `app/agent.py` produces no-action decisions for provider timeout/connection, call-budget exhaustion, and handled SQLite read failures when persistence remains available. Tool timeout/connection failures are wrapped by the budget boundary and tested, but no substantive tool is integrated. Persistent database write failures are surfaced because a durable fallback cannot be guaranteed. |
| 9 | Corrected goal-rule handling without reprocessing all viewers | **PASS** | `app/agent.py` updates the in-memory rule and only open instances via `app/database.py.update_goal_rule`. `app/evaluation.py` now verifies with the persisted instance rule. Tests verify corrected G03 `play_started` succeeds while its old `app_open` signal does not satisfy the corrected rule. |
| 10 | Resist unsupported model claims and catalogue prompt injection | **PARTIAL** | The fixed mock does not receive catalogue descriptions or other free-text fields, and unsupported/malformed injected output falls back to no action. A dedicated malicious-catalogue/prompt-injection test and a real model boundary are absent. |
| 11 | Configurable maximum of 8 model calls and 20 tool calls per decision | **PARTIAL** | `config/interventions.json` now configures limits of 8 and 20. `DecisionCallBudget` enforces limits and fresh decisions report counts. No actual tool calls occur in the current replay, so integrated tool-call enforcement is not demonstrated. |
| 12 | Replay decisions within 60 seconds; over 5 minutes is a failure | **PASS** | Preserved sample metrics: max 587.776 ms. Isolated updated-code replay: max 305.834 ms. Both are below 60 seconds and 5 minutes. The separate 200 ms local budget was exceeded by 3 saved-run decisions and 7 isolated-run decisions. Simulated dependency delays/timeouts are not measured processing latency. |
| 13 | Messaging consent, 22:00–08:00 IST quiet hours, combined daily cap, approved templates | **PARTIAL** | Rules are in `config/interventions.json` and `config/messaging_policy.md`, with unit tests for policy helpers. The active action path publishes home pages only, so end-to-end message enforcement/delivery is not exercised. |
| 14 | Attributable outcomes, durable learning, randomized holdout analysis; no served/unserved causal claim | **PARTIAL** | `app/evaluation.py` independently verifies outcomes and compares randomized assignment, and persists outcomes. The evaluator does not feed outcomes into a learning/update policy. |
| 15 | Runnable CLI/API, setup, replay command, provider abstraction/mock | **PARTIAL** | `run_replay.py` and `process_event.py` are runnable; `README.md` documents commands. `MockModelProvider` exercises a boundary, but there is no API or substantive alternate provider. |
| 16 | Tests for late/duplicate trigger, conflicting source, tool failure, corrected goal rule | **PARTIAL** | Tests now cover each behavior at the implemented boundary: late event time filtering, duplicate trigger/idempotent storage, conflicting profile resolution, tool timeout/call-limit handling, and corrected G03 outcome verification. Late-event filtering and tool failure are not end-to-end integrations with a live event broker or production tool service. |
| 17 | Sample decisions, actions, no-action reasons, outcomes, latency, impact | **PASS** | Preserved artifacts exist under `sample_run/`: decisions, actions, faults, simulator events/report, replay metrics, pilot evaluation, and outcome evaluation. Counts and metrics were cross-checked before changes. |
| 18 | Required README, ARCHITECTURE, AI_COLLABORATION, KNOWN_LIMITATIONS | **PASS** | All four files now exist: `README.md`, `ARCHITECTURE.md`, `AI_COLLABORATION.md`, `KNOWN_LIMITATIONS.md`. README describes current behavior and notes the preserved decision output predates the extended evidence schema. |
| 19 | Honest limitations and evidence generated work was checked/tested | **PASS** | `AI_COLLABORATION.md` records the assistance and verification scope; this audit records actual tests and isolated replay results. Limitations remain explicit; no real-world impact claim is made. |

## Test results

Exact command:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**Final result: 27 tests passed, 0 failed.**

The added tests cover:

- Late event receipt: an event with an old phone timestamp but server receipt after the decision is not used early.
- Duplicate IDs/triggers: repeated ingestion and trigger processing do not create duplicate payloads/actions.
- Conflicting profile sources: latest `updated_at` with deterministic tie-break is selected and conflicting fields are reported.
- Tool failure and budgets: transient tool timeout is converted to a safe boundary error; model/tool call ceilings are enforced.
- Corrected goal outcomes: evaluator uses the stored corrected success event and condition.
- Database read and model provider failures: no-action fallbacks are recorded when persistence remains available.

The tool test covers the reusable tool boundary, not an integrated tool service; that distinction is reflected in the status table.

## Isolated replay verification

Ran `run_replay.py` with a newly created temporary database and a separate temporary output folder. The replay, simulator, and evaluation completed; the temporary folder was removed after inspecting outputs. Existing databases, `sample_run/`, source data, and evaluation artifacts were untouched.

| Isolated-run measure | Result |
|---|---:|
| Historical input rows / unique historical payloads added | 142,906 / 140,730 |
| Trigger rows / unique IDs / duplicate trigger IDs | 2,769 / 2,679 / 90 |
| Decisions / actions / model faults | 2,679 / 1,037 / 5 |
| Decision outputs containing checks, evidence, and confidence status | 2,679 / 2,679 / 2,679 |
| Maximum model calls / tool calls in a decision | 1 / 0 |
| Mean / maximum measured local latency | 30.297 ms / 305.834 ms |
| Decisions over configured 200 ms local budget | 7 |
| Simulator events / action count | 31,505 / 1,037 |
| Simulator policy problems | 0 |

Decision counts, action counts, simulator action counts, and simulator event line counts matched. No decision exceeded the candidate brief's 60-second or 5-minute thresholds.

## Evaluation and remaining limitations

The isolated evaluation reproduced the saved randomized triggered-user result:

| Arm | Triggered users | Successes | Rate |
|---|---:|---:|---:|
| Agent | 863 | 92 | 10.66% |
| Holdout | 883 | 85 | 9.63% |

The difference is **+1.03 percentage points**, with approximate 95% CI **−1.80 to +3.86 percentage points**. The interval spans zero; positive impact is **not established**. Outcomes exist only for triggered users; **3,254 assigned-but-untriggered users** have no simulated outcomes, so this is not a full-population ITT estimate. Served-versus-unserved is observational only.

Confirmed limitations remain:

- **G10/G11:** G10 has no measurable success predicate; G11's `tv_app_open` event is absent.
- **G06:** no threshold defines “recently lapsed”; current eligibility uses lapsed status without inventing a recency cutoff.
- **G07:** “unwatched” is ambiguous; current eligibility uses catalogue episodes, completion events, and a three-day gap. Its verifier requires same-slug `play_started` within the deadline; completion alone does not qualify.
- **Mock/action scope:** the mock makes no substantive recommendations. Only validated home-page actions are emitted; no push, WhatsApp, or in-app messages are sent.
- **Tool and model confidence:** model-call limits are enforced; tool-call limits are configured/tested at the boundary but there is no active tool integration. Confidence is not estimated.
- **Failure handling:** known provider/tool errors and transient database read errors can fall back if decision persistence remains available. Persistent write failures surface and cannot be durably recorded.
- **Data evidence:** profile conflicts are disclosed and resolved deterministically; event provenance in decisions is summarized rather than enumerated per fact.
- **Simulator/effect:** simulator outcomes are synthetic, only cover triggered users, and do not enforce every messaging policy or consume all replay controls/faults. No durable outcome-driven learning loop is implemented.

## Changes made for this audit

- **Code/config/tests:** `app/agent.py`, `app/database.py`, `app/evaluation.py`, `app/replay.py`, `config/interventions.json`, `tests/test_agent.py`.
- **Documentation:** `README.md`, `ARCHITECTURE.md`, `AI_COLLABORATION.md`, `KNOWN_LIMITATIONS.md`, `result.md`.
- **Not changed:** supplied datasets, existing database backup, and all saved `sample_run/` evaluation/replay outputs. The removed root `agent.db` remains absent.

## Overall readiness: **READY WITH LIMITATIONS**

The core replay, policy checks, durable state, corrected-rule verification, evidence records, configured call budgets, targeted failure boundaries, required documents, and saved evaluation are present and tested. This is not full production compliance: substantive tools and real model recommendations are not implemented, message delivery is inactive, confidence is not probabilistic, source provenance is summarized, durable learning is absent, and outcomes are simulator-only.

### Highest-priority remaining gaps

1. Integrate real tools/provider operations if required, with end-to-end call-budget and failure tests.
2. Add per-fact provenance and a separately implemented eligibility verifier; retain honest confidence as not estimated until a calibrated model exists.
3. Add end-to-end message dispatch only if messaging is in scope, including shared frequency-cap state.
4. Define measurable G10/G11 signals and clarify G06/G07 semantics with assignment owners.
5. Add a durable outcome-driven learning process only when supported by validated evidence.

### Submission checklist

- [x] `README.md`, `ARCHITECTURE.md`, `AI_COLLABORATION.md`, and `KNOWN_LIMITATIONS.md` present.
- [x] Existing saved results and original input data preserved.
- [x] Full test command passed: 27/27.
- [x] Full replay/simulator/evaluation completed using isolated temporary paths.
- [x] Randomized triggered-user estimate, uncertainty, and population limitation stated accurately.
- [ ] Integrate substantive tool/model behavior and message dispatch if the candidate brief requires production-like execution.
- [ ] Resolve ambiguous/unmeasurable goal definitions with the assignment owner before claiming complete goal coverage.
