import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.agent import TRIGGER_EVENTS, UserAgent, parse_time
from app.database import Database
from app.evaluation import evaluate, evaluate_pilot


def read_jsonl(path):
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"Invalid JSON in {path} at line {line_number}: {error}"
                    ) from error


def read_arms(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return {row["user_id"]: row["arm"] for row in csv.DictReader(source)}


def load_faults(path):
    faults = {}
    for fault in read_jsonl(path):
        trigger_id = fault["trigger_event_id"]
        if trigger_id in faults:
            raise ValueError("More than one model fault for a trigger event")
        faults[trigger_id] = fault
    return faults


def ingest_historical(path, database, batch_size=1000):
    total = 0
    newly_stored = 0
    batch = []
    for event in read_jsonl(path):
        batch.append(event)
        total += 1
        if len(batch) == batch_size:
            newly_stored += database.ingest_events(batch, "historical")
            batch.clear()
    if batch:
        newly_stored += database.ingest_events(batch, "historical")
    return total, newly_stored


def replay_items(pack):
    priority = {"control": 0, "live": 1, "trigger": 2}
    items = []
    for control in read_jsonl(pack / "replay" / "control_events.jsonl"):
        items.append((parse_time(control["at"]), priority["control"], control))
    for event in read_jsonl(pack / "replay" / "live_events.jsonl"):
        items.append((
            parse_time(event.get("deliver_at") or event["received_at"]),
            priority["live"], event,
        ))
    for trigger in read_jsonl(pack / "replay" / "triggers.jsonl"):
        items.append((
            parse_time(trigger.get("deliver_at") or trigger["received_at"]),
            priority["trigger"], trigger,
        ))
    items.sort(key=lambda item: (item[0], item[1], item[2].get("event_id", "")))
    return items


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            output.write("\n")


def export_outputs(database, out):
    decision_rows = database.export_rows("decisions", [
        "decision_id", "user_id", "trigger_event_id", "decided_at",
        "latency_ms", "goal_id", "arm", "outcome", "reason", "details_json",
    ])
    decisions = []
    for row in decision_rows:
        saved = json.loads(row.pop("details_json"))
        row["details"] = saved.get("details", {})
        row["latency_budget_ms"] = saved.get("latency_budget_ms")
        for field in (
            "goal_deadline_at", "goal_success_signal", "confidence",
            "assumptions", "conflicts", "checks", "evidence",
        ):
            row[field] = saved.get(field)
        decisions.append(row)
    decisions.sort(key=lambda row: (row["trigger_event_id"], row["decision_id"]))

    action_rows = database.export_rows("actions", [
        "action_id", "decision_id", "user_id", "action_type",
        "published_at", "payload_json",
    ])
    actions = [json.loads(row["payload_json"]) for row in action_rows]
    actions.sort(key=lambda row: (row["published_at"], row["action_id"]))

    fault_rows = database.export_rows("fault_results", [
        "trigger_event_id", "user_id", "fault", "result", "details_json",
    ])
    faults = [{
        "trigger_event_id": row["trigger_event_id"],
        "user_id": row["user_id"],
        "fault": row["fault"],
        "result": row["result"],
        "details": json.loads(row["details_json"]),
    } for row in fault_rows]
    faults.sort(key=lambda row: row["trigger_event_id"])

    write_jsonl(out / "decisions.jsonl", decisions)
    write_jsonl(out / "actions.jsonl", actions)
    write_jsonl(out / "fault_results.jsonl", faults)
    return decisions, actions, faults


def run_replay(pack, out, db_path, seed="oua-practice",
               latency_budget_ms=200, simulate=True):
    pack = Path(pack).resolve()
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    database = Database(db_path)
    historical_total, historical_new = ingest_historical(
        pack / "data" / "events.jsonl", database
    )
    arms = read_arms(pack / "replay" / "arms.csv")
    faults = load_faults(pack / "replay" / "model_faults.jsonl")
    agent = UserAgent(pack, database, arms, faults, latency_budget_ms)
    replay_counts = {"live_seen": 0, "live_new": 0, "triggers_seen": 0,
                     "unique_trigger_ids": 0, "duplicate_trigger_ids": 0,
                     "controls_seen": 0}
    unique_triggers = set()
    controls = []

    for _, kind, item in replay_items(pack):
        if kind == 0:
            controls.append(item)
            agent.apply_control(item)
            replay_counts["controls_seen"] += 1
        elif kind == 1:
            replay_counts["live_seen"] += 1
            is_new = not database.has_event(item["event_id"])
            if is_new:
                database.ingest_events([item], "live")
                agent.histories[item["user_id"]].append(item)
                replay_counts["live_new"] += 1
        else:
            replay_counts["triggers_seen"] += 1
            if item["event_id"] in unique_triggers:
                replay_counts["duplicate_trigger_ids"] += 1
            else:
                unique_triggers.add(item["event_id"])
            agent.process_trigger(item, controls)

    replay_counts["unique_trigger_ids"] = len(unique_triggers)
    decisions, actions, fault_results = export_outputs(database, out)

    trigger_users = {
        event["user_id"]
        for event in read_jsonl(pack / "replay" / "triggers.jsonl")
        if event.get("event") in TRIGGER_EVENTS
    }
    latencies = [row["latency_ms"] for row in decisions]
    metrics = {
        "historical_source_rows": historical_total,
        "historical_unique_payloads_added_this_run": historical_new,
        "replay": replay_counts,
        "triggered_users": len(trigger_users),
        "decisions_persisted": len(decisions),
        "actions_persisted": len(actions),
        "model_faults_reproduced": len(fault_results),
        "latency_budget_ms": latency_budget_ms,
        "measured_latency_ms": {
            "count": len(latencies),
            "mean": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "max": round(max(latencies), 3) if latencies else None,
            "within_budget": sum(value <= latency_budget_ms for value in latencies),
            "over_budget": sum(value > latency_budget_ms for value in latencies),
        },
        "decision_reasons": {},
    }
    for decision in decisions:
        metrics["decision_reasons"][decision["reason"]] = (
            metrics["decision_reasons"].get(decision["reason"], 0) + 1
        )
    (out / "replay_metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    database.save_metric("last_replay", metrics)
    pilot_evaluation = evaluate_pilot(pack, out / "pilot_evaluation.json")

    simulator_report = None
    evaluation = None
    if simulate:
        simulator_out = out / "simulator"
        command = [
            sys.executable,
            str(pack / "simulator" / "simulate_outcomes.py"),
            "--pack", str(pack),
            "--actions", str(out / "actions.jsonl"),
            "--out", str(simulator_out),
            "--seed", seed,
        ]
        subprocess.run(command, check=True, cwd=pack)
        simulator_report = json.loads(
            (simulator_out / "simulator_report.json").read_text(encoding="utf-8")
        )
        evaluation = evaluate(
            pack, database, out / "actions.jsonl",
            simulator_out / "outcome_events.jsonl", out / "evaluation_report.json",
        )

    return {
        "metrics": metrics,
        "simulator_report": simulator_report,
        "evaluation": evaluation,
        "pilot_evaluation": pilot_evaluation,
    }


def main():
    default_pack = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Run the deterministic OUA replay.")
    parser.add_argument("--pack", type=Path, default=default_pack)
    parser.add_argument("--out", type=Path, default=default_pack / "sample_run")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--seed", default="oua-practice")
    parser.add_argument("--latency-budget-ms", type=float, default=200)
    parser.add_argument("--no-simulate", action="store_true")
    args = parser.parse_args()
    db_path = args.db or args.pack / "agent.db"
    result = run_replay(
        args.pack, args.out, db_path, args.seed,
        args.latency_budget_ms, not args.no_simulate,
    )
    print(json.dumps(result["metrics"], indent=2))
    if result["evaluation"] is not None:
        print("Evaluation report:", args.out / "evaluation_report.json")


if __name__ == "__main__":
    main()
