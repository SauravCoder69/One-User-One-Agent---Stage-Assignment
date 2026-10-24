# Known limitations and assumptions

- G10 has no measurable success predicate and G11 depends on absent `tv_app_open` events; both are blocked from
  goal assignment and are inconclusive if encountered in persisted state.
- G06 has no lapsed-recency threshold. Eligibility uses the available `lapsed` subscription status only; it does not
  invent a recency cutoff.
- G07 has no formal definition of “unwatched.” The implementation uses episode rows per `content_slug`, completed
  episode IDs, and a three-day gap since the last observed play. A `play_progress` event is not treated as a
  completion. Outcome verification follows the configured success signal exactly: only a `play_started` event
  whose catalogue title has the goal's unfinished-series `content_slug`, received after goal start and by the
  deadline, succeeds. The simulator's `episode_completed` signal alone does not satisfy G07.
- Conflicting profile rows use the latest `updated_at`, with a deterministic tie-break. This is a fallback, not a
  correction to source data. App settings on the trigger are used for the page dialect; unknown/missing dialects
  result in abstention.
- The source event stream has duplicate IDs, mismatched device/server times, and late events. Deduplication uses the
  first persisted payload for an ID; replay ordering uses server delivery time. Historical receipt-time differences
  are preserved rather than “fixed.”
- A goal instance remains current until its deadline; a new candidate is not selected while that instance remains
  open. Success/failure is verified after simulator output, not during the replay day.
- The mock model does not make substantive recommendations. It exists to exercise a provider boundary and injected
  faults. The current decision path chooses only validated home-page personalization; it intentionally sends no
  push, WhatsApp, or in-app message.
- The supplied simulator itself does not enforce every messaging policy, apply the G03 correction, or consume the
  control/fault feeds. The replay harness applies the G03 correction, title withdrawal, dependency windows, and
  model faults for the agent; simulator control/fault behavior remains unchanged.
- The simulator reports synthetic future events only for triggered users. Evaluation therefore reports ITT among
  triggered users, retains inconclusive users in the denominator, and cannot estimate outcomes for the 3,254
  assigned-but-untriggered users.
- The evaluator uses the selected goal instance and simulator events as evidence. Its success rates are synthetic
  simulator measurements, not a real-world causal claim. Sparse event generation and the simulator's modeled
  effects limit conclusions.
- Processing latency is measured for the local decision path. The 8-second degraded-store latency and model timeout
  are recorded as simulated failure conditions; they are not represented as measured elapsed processing time.
- Decision output records source/timestamp summaries, profile conflicts, checks, and goal conditions, but event
  provenance is grouped by type/source rather than enumerating every consulted event. The mock is not probabilistic,
  so confidence is explicitly marked `not_estimated`.
- Model/tool call ceilings are configured per decision. The current replay makes no substantive tool calls; the
  tool failure boundary is exercised by unit tests rather than an integrated tool implementation.
- Known provider timeout/connection errors and SQLite read errors fall back to no action when decision persistence
  remains available. A persistent database write failure cannot be recorded durably and is surfaced to the caller.
- The input pack's pilot headline compares served users with a mixed unserved group. The implementation does not use
  that observational comparison as causal evidence; the randomized assignment is the primary comparison.
