import csv
import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from app.agent import IST, iso, parse_time


def _read_jsonl(path):
    with Path(path).open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _success(goal, start, deadline, slug, events, catalogue):
    goal_id = goal["goal_id"]
    qualifying = []
    play_days = set()
    for event in events:
        at = parse_time(event.get("received_at"))
        if not at or not (start < at <= deadline):
            continue
        properties = event.get("properties") or {}
        event_type = event.get("event")
        title = catalogue.get(properties.get("title_id"), {})

        success_event = goal.get("success", {}).get("event")
        if goal_id == "G01" and event_type == success_event:
            qualifying.append(event)
        elif goal_id == "G02" and event_type == success_event:
            qualifying.append(event)
        elif goal_id == "G03" and event_type == success_event:
            if at.astimezone(IST).date() > start.astimezone(IST).date():
                qualifying.append(event)
        elif goal_id in {"G04", "G05"} and event_type == success_event:
            if properties.get("is_trial") is False:
                qualifying.append(event)
        elif goal_id == "G06" and event_type == success_event:
            if properties.get("is_trial") is False:
                qualifying.append(event)
        elif goal_id == "G07" and event_type == success_event:
            if slug and title.get("content_slug") == slug:
                qualifying.append(event)
        elif goal_id == "G08" and event_type == success_event:
            release = title.get("release_date")
            if release:
                released = parse_time(release)
                if start - timedelta(days=30) <= released <= start:
                    qualifying.append(event)
        elif goal_id == "G09" and event_type == success_event:
            play_days.add(at.astimezone(IST).date())
        elif goal_id == "G12" and event_type == success_event:
            qualifying.append(event)

    if goal_id == "G09":
        if len(play_days) >= 3:
            return True, [{"event": "play_started", "distinct_ist_days": len(play_days)}]
        return False, []
    return bool(qualifying), qualifying[:10]


def _wilson(successes, total, z=1.96):
    if not total:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * (
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    ) ** 0.5 / denominator
    return [round(max(0, center - margin), 4), round(min(1, center + margin), 4)]


def evaluate(pack, database, actions_path, outcome_events_path, report_path):
    pack = Path(pack)
    goals = json.loads(
        (pack / "config" / "goals.json").read_text(encoding="utf-8")
    )
    goal_map = {goal["goal_id"]: goal for goal in goals["goals"]}
    with (pack / "data" / "catalogue.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        catalogue = {row["title_id"]: row for row in csv.DictReader(source)}
    with (pack / "replay" / "arms.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        arms = {row["user_id"]: row["arm"] for row in csv.DictReader(source)}

    trigger_users = {
        event["user_id"] for event in _read_jsonl(pack / "replay" / "triggers.jsonl")
    }
    trigger_ids_by_user = defaultdict(list)
    for event in _read_jsonl(pack / "replay" / "triggers.jsonl"):
        trigger_ids_by_user[event["user_id"]].append(event["event_id"])
    triggered_users = {
        user_id for user_id in trigger_users if arms.get(user_id) in {"agent", "holdout"}
    }

    goals_by_user = {
        row["user_id"]: row
        for row in database.export_rows("goal_instances", [
            "user_id", "goal_id", "started_at", "deadline_at", "status",
            "success_event", "condition_json", "content_slug", "rule_version",
        ])
    }
    outcomes_by_user = defaultdict(list)
    for event in _read_jsonl(outcome_events_path):
        outcomes_by_user[event["user_id"]].append(event)

    results = {}
    for user_id in sorted(triggered_users):
        instance = goals_by_user.get(user_id)
        if not instance:
            results[user_id] = {
                "goal_id": None,
                "category": "inconclusive",
                "reason": "no_eligible_measurable_goal",
                "evidence": [],
            }
            continue
        goal = goal_map.get(instance["goal_id"])
        if not goal or instance["goal_id"] not in {
            "G01", "G02", "G03", "G04", "G05", "G06",
            "G07", "G08", "G09", "G12",
        }:
            results[user_id] = {
                "goal_id": instance["goal_id"],
                "category": "inconclusive",
                "reason": "success_signal_unmeasurable",
                "evidence": [],
            }
            database.close_goal(user_id, "inconclusive")
            continue
        start = parse_time(instance["started_at"])
        deadline = parse_time(instance["deadline_at"])
        goal_for_evaluation = dict(goal)
        success = dict(goal.get("success", {}))
        stored_condition = json.loads(instance.get("condition_json") or "{}")
        success.update(stored_condition)
        if instance.get("success_event"):
            success["event"] = instance["success_event"]
        goal_for_evaluation["success"] = success
        succeeded, evidence = _success(
            goal_for_evaluation, start, deadline, instance.get("content_slug"),
            outcomes_by_user[user_id], catalogue,
        )
        category = "succeeded" if succeeded else "failed"
        reason = "success_signal_observed" if succeeded else "deadline_passed_without_signal"
        results[user_id] = {
            "goal_id": instance["goal_id"],
            "category": category,
            "reason": reason,
            "evidence": [{
                "event_id": item.get("event_id"),
                "event": item.get("event"),
                "received_at": item.get("received_at"),
                "distinct_ist_days": item.get("distinct_ist_days"),
            } for item in evidence],
        }
        database.save_outcome(
            user_id, instance["goal_id"], category, iso(deadline), reason,
            results[user_id]["evidence"],
        )
        database.close_goal(user_id, category)

    groups = {}
    for arm in ("agent", "holdout"):
        users = [user for user in triggered_users if arms.get(user) == arm]
        successes = sum(results[user]["category"] == "succeeded" for user in users)
        failures = sum(results[user]["category"] == "failed" for user in users)
        inconclusive = sum(
            results[user]["category"] == "inconclusive" for user in users
        )
        total = len(users)
        groups[arm] = {
            "assigned_triggered_users": total,
            "succeeded": successes,
            "failed": failures,
            "inconclusive": inconclusive,
            "success_rate": round(successes / total, 4) if total else None,
            "success_rate_denominator": total,
            "success_rate_wilson_95_ci": _wilson(successes, total),
        }
    agent_rate = groups["agent"]["success_rate"]
    holdout_rate = groups["holdout"]["success_rate"]
    absolute = (
        round(agent_rate - holdout_rate, 4)
        if agent_rate is not None and holdout_rate is not None else None
    )
    relative = (
        round(agent_rate / holdout_rate, 4)
        if agent_rate is not None and holdout_rate else None
    )
    difference_interval = None
    if (
        agent_rate is not None and holdout_rate is not None
        and groups["agent"]["assigned_triggered_users"]
        and groups["holdout"]["assigned_triggered_users"]
    ):
        standard_error = (
            agent_rate * (1 - agent_rate)
            / groups["agent"]["assigned_triggered_users"]
            + holdout_rate * (1 - holdout_rate)
            / groups["holdout"]["assigned_triggered_users"]
        ) ** 0.5
        difference_interval = [
            round(absolute - 1.96 * standard_error, 4),
            round(absolute + 1.96 * standard_error, 4),
        ]
    report = {
        "estimand": "intent_to_treat among users with at least one replay trigger",
        "assignment_source": "replay/arms.csv",
        "outcome_source": "simulator outcome_events.jsonl, independently verified against goal rules",
        "groups": groups,
        "absolute_success_rate_difference": absolute,
        "absolute_difference_approximate_95_ci": difference_interval,
        "relative_success_rate": relative,
        "positive_impact_supported_by_interval": (
            difference_interval[0] > 0 if difference_interval else None
        ),
        "missing_or_untriggered_assigned_users": 5000 - len(triggered_users),
        "eligible_assignment_discrepancies": {
            "triggered_without_arm": sum(user not in arms for user in trigger_users),
            "holdout_actions": sum(
                1 for action in _read_jsonl(actions_path)
                if arms.get(action["user_id"]) == "holdout"
            ),
        },
        "interpretation": (
            "The comparison follows randomized assignment, not action receipt. "
            "The denominator is all users in an assigned arm who had at least one "
            "replay trigger; users without a measurable eligible goal are counted "
            "as inconclusive and remain in the denominator. The approximate "
            "difference interval is a normal-approximation interval."
        ),
        "limitation": (
            "The supplied simulator is synthetic, only triggered users are simulated, "
            "and its outcome process does not apply every messaging-policy rule. "
            "This estimate is not evidence of real-world impact."
        ),
        "per_user_outcomes": results,
    }
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def evaluate_pilot(pack, report_path):
    pack = Path(pack)
    with (pack / "pilot" / "pilot_assignment.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        assignment_rows = list(csv.DictReader(source))
    with (pack / "pilot" / "pilot_outcomes.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        outcome_rows = list(csv.DictReader(source))

    assignments = {row["user_id"]: row for row in assignment_rows}
    outcomes = {row["user_id"]: row for row in outcome_rows}
    groups = {}
    for arm in ("treatment", "holdout"):
        users = [
            user_id for user_id, row in assignments.items()
            if row["arm"] == arm and user_id in outcomes
        ]
        successes = sum(outcomes[user]["succeeded"].lower() == "true" for user in users)
        total = len(users)
        groups[arm] = {
            "assigned_users_with_outcome": total,
            "succeeded": successes,
            "failed": total - successes,
            "success_rate": round(successes / total, 4) if total else None,
            "success_rate_wilson_95_ci": _wilson(successes, total),
        }

    treatment_rate = groups["treatment"]["success_rate"]
    holdout_rate = groups["holdout"]["success_rate"]
    difference = (
        treatment_rate - holdout_rate
        if treatment_rate is not None and holdout_rate is not None else None
    )
    difference_ci = None
    if difference is not None:
        standard_error = (
            treatment_rate * (1 - treatment_rate)
            / groups["treatment"]["assigned_users_with_outcome"]
            + holdout_rate * (1 - holdout_rate)
            / groups["holdout"]["assigned_users_with_outcome"]
        ) ** 0.5
        difference_ci = [
            round(difference - 1.96 * standard_error, 4),
            round(difference + 1.96 * standard_error, 4),
        ]

    served_groups = {}
    for served in ("true", "false"):
        users = [
            user_id for user_id, assignment in assignments.items()
            if assignment["served"].lower() == served and user_id in outcomes
        ]
        successes = sum(outcomes[user]["succeeded"].lower() == "true" for user in users)
        served_groups[served] = {
            "users": len(users),
            "succeeded": successes,
            "success_rate": round(successes / len(users), 4) if users else None,
        }

    report = {
        "estimand": "intent-to-treat by randomized pilot assignment",
        "assignment_rows": len(assignment_rows),
        "unique_assigned_users": len(assignments),
        "outcome_rows": len(outcome_rows),
        "unique_outcome_users": len(outcomes),
        "groups": groups,
        "absolute_success_rate_difference": (
            round(difference, 4) if difference is not None else None
        ),
        "relative_success_rate": (
            round(treatment_rate / holdout_rate, 4)
            if treatment_rate is not None and holdout_rate else None
        ),
        "absolute_difference_approximate_95_ci": difference_ci,
        "positive_impact_supported_by_interval": (
            difference_ci[0] > 0 if difference_ci else None
        ),
        "missing_outcomes": len(set(assignments) - set(outcomes)),
        "outcomes_without_assignment": len(set(outcomes) - set(assignments)),
        "assignment_vs_outcome_arm_mismatches": sum(
            assignments[user]["arm"] != outcomes[user]["arm"]
            for user in set(assignments) & set(outcomes)
        ),
        "served_vs_unserved_observational_only": served_groups,
        "interpretation": (
            "Use randomized treatment versus holdout as the primary comparison. "
            "Served versus unserved conditions on post-assignment behavior and is "
            "reported only as observational context. The approximate normal "
            "difference interval includes zero when the evidence does not support "
            "a positive causal impact."
        ),
    }
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
