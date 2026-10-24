import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.agent import (
    Catalogue,
    DecisionCallBudget,
    IST,
    MessagingPolicy,
    MockModelProvider,
    UserAgent,
    parse_time,
)
from app.database import Database
from app.evaluation import _success, evaluate, evaluate_pilot
from app.replay import replay_items

PACK = Path(__file__).resolve().parents[1]


def event(event_id, user_id="U-00001", kind="app_open", when="2026-10-01T12:00:00Z"):
    return {
        "event_id": event_id,
        "user_id": user_id,
        "event": kind,
        "occurred_at": when,
        "sent_at": when,
        "received_at": when,
        "deliver_at": when,
        "session_id": "session-" + event_id,
        "app_dialect": "d1",
        "app_language": "hin",
        "platform": "android",
        "app_version": "6.1.4",
        "properties": {},
    }


class DatabaseTests(unittest.TestCase):
    def test_event_ids_are_idempotent_across_batches_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.db"
            first = Database(path)
            row = event("duplicate-id")
            self.assertEqual(first.ingest_events([row, row], "test"), 1)
            self.assertEqual(first.ingest_events([row], "test"), 0)
            second = Database(path)
            self.assertTrue(second.has_event("duplicate-id"))
            self.assertEqual(len(second.events_for_user("U-00001")), 1)

    def test_received_time_is_preserved_for_out_of_order_event(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            late_phone_clock = event("late-phone-clock")
            late_phone_clock["occurred_at"] = "2026-09-01T00:00:00Z"
            self.assertEqual(database.ingest_events([late_phone_clock], "test"), 1)
            loaded = database.events_for_user("U-00001")[0]
            self.assertEqual(loaded["occurred_at"], "2026-09-01T00:00:00Z")
            self.assertEqual(loaded["received_at"], "2026-10-01T12:00:00Z")

    def test_late_event_is_not_used_before_server_receipt_time(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            late_play = event(
                "late-play", kind="play_started",
                when="2026-09-01T00:00:00Z",
            )
            late_play["received_at"] = "2026-10-02T12:00:00Z"
            database.ingest_events([late_play], "test")
            agent = UserAgent(PACK, database, {}, {})
            trigger = event("before-late-receipt")
            profile = {
                "signup_at": "2026-09-30T12:00:00Z",
                "subscription_status": "never",
            }
            eligible, _ = agent._eligible(
                "G01", "U-00001", parse_time("2026-10-01T12:00:00Z"),
                trigger, profile,
            )
            self.assertTrue(eligible)

    def test_conflicting_profile_rows_are_reported_and_resolved(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = UserAgent(PACK, Database(Path(folder) / "state.db"), {}, {})
            agent.profiles["U-00001"] = [
                {
                    "updated_at": "2026-10-01T00:00:00Z",
                    "subscription_status": "never",
                    "preferred_dialect": "d1",
                },
                {
                    "updated_at": "2026-10-01T01:00:00Z",
                    "subscription_status": "active",
                    "preferred_dialect": "d2",
                },
            ]
            selected, evidence, conflicts = agent._profile_resolution("U-00001")
            self.assertEqual(selected["subscription_status"], "active")
            self.assertEqual(evidence["updated_at"], "2026-10-01T01:00:00Z")
            self.assertIn("subscription_status", {item["field"] for item in conflicts})
            self.assertIn("preferred_dialect", {item["field"] for item in conflicts})


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = Catalogue(PACK)
        self.policy = MessagingPolicy(PACK)
        self.at = parse_time("2026-10-02T12:00:00Z")

    def test_home_page_accepts_valid_distinct_titles(self):
        titles = self.catalogue.dialect_titles("d1", self.at)
        distinct = []
        slugs = set()
        for title in titles:
            if title["content_slug"] not in slugs:
                distinct.append(title)
                slugs.add(title["content_slug"])
            if len(distinct) == 4:
                break
        page = {
            "dialect": "d1",
            "hero_title_id": distinct[0]["title_id"],
            "rows": [{
                "row_type": "popular_in_dialect",
                "title_ids": [item["title_id"] for item in distinct[1:]],
            }],
        }
        self.assertEqual(self.policy.check_page(page, self.catalogue, self.at), [])

    def test_home_page_rejects_unknown_dialect_and_short_row(self):
        page = {
            "dialect": "d7",
            "hero_title_id": None,
            "rows": [{"row_type": "popular_in_dialect", "title_ids": ["missing"]}],
        }
        issues = self.policy.check_page(page, self.catalogue, self.at)
        self.assertIn("unsupported_dialect", issues)
        self.assertIn("invalid_row_size", issues)
        self.assertIn("unknown_title", issues)

    def test_home_page_rejects_cross_language_duplicate_slug(self):
        variants = next(
            rows for rows in self.catalogue.by_slug.values() if len(rows) > 1
        )
        page = {
            "dialect": variants[0]["dialect"],
            "hero_title_id": None,
            "rows": [{
                "row_type": "popular_in_dialect",
                "title_ids": [variants[0]["title_id"], variants[1]["title_id"]],
            }],
        }
        issues = self.policy.check_page(page, self.catalogue, self.at)
        self.assertIn("duplicate_content_slug", issues)
        self.assertIn("invalid_row_size", issues)

    def test_invalid_catalogue_window_is_never_available(self):
        title = next(
            row for row in self.catalogue.by_id.values()
            if row["available_until"]
            and parse_time(row["available_until"]) <= parse_time(row["available_from"])
        )
        self.assertFalse(self.catalogue.available(title["title_id"], self.at))

    def test_push_consent_quiet_hours_and_cap(self):
        message = {
            "type": "push", "text": {"title": "Watch now", "body": "A title for you."},
        }
        profile = {"notifications_opt_in": "false"}
        issues = self.policy.check_message(
            message, profile, parse_time("2026-10-01T23:30:00Z")
        )
        self.assertIn("push_no_consent", issues)
        self.assertIn("quiet_hours", issues)
        self.assertIn(
            "combined_daily_cap",
            self.policy.check_message(message, {"notifications_opt_in": "true"},
                                      self.at, messages_today=1),
        )
        opted_out = self.policy.check_message(
            message, {"notifications_opt_in": "true"}, self.at,
            opted_out_channels={"push"},
        )
        self.assertIn("push_opted_out", opted_out)

    def test_message_copy_and_price_constraints(self):
        message = {
            "type": "push",
            "text": {
                "title": "Hurry! Only today",
                "body": "Guaranteed offer at ₹999. " + ("x" * 125),
            },
        }
        issues = self.policy.check_message(
            message, {"notifications_opt_in": "true"}, self.at
        )
        self.assertIn("forbidden_phrase", issues)
        self.assertIn("unsupported_price", issues)
        self.assertIn("body_too_long", issues)

    def test_whatsapp_template_is_allowlisted_and_variable_exact(self):
        message = {
            "type": "whatsapp",
            "template_id": "wa_recommend_title_v1",
            "variables": {"title_name": "Name"},
        }
        issues = self.policy.check_message(
            message, {"whatsapp_opt_in": "true"}, self.at
        )
        self.assertIn("invalid_template_variables", issues)
        message["template_id"] = "not_approved"
        self.assertIn(
            "unapproved_template",
            self.policy.check_message(message, {"whatsapp_opt_in": "true"}, self.at),
        )

    def test_whatsapp_variables_must_match_catalogue_and_pricing(self):
        title = next(
            item for item in self.catalogue.dialect_titles("d1", self.at)
            if item["language"] == "hin"
        )
        message = {
            "type": "whatsapp",
            "template_id": "wa_recommend_title_v1",
            "title_id": title["title_id"],
            "variables": {
                "title_name": title["title"],
                "dialect_name": title["dialect"],
            },
        }
        self.assertEqual(
            self.policy.check_message(
                message, {"whatsapp_opt_in": "true"}, self.at,
                catalogue=self.catalogue, app_dialect="d1",
            ),
            [],
        )
        message["variables"]["title_name"] = "Unsupported title"
        self.assertIn(
            "template_title_not_in_catalogue",
            self.policy.check_message(
                message, {"whatsapp_opt_in": "true"}, self.at,
                catalogue=self.catalogue, app_dialect="d1",
            ),
        )

    def test_title_recommendation_checks_catalogue_dialect(self):
        message = {
            "type": "push",
            "title_id": "missing",
            "recommends_title": True,
            "text": {"title": "Title", "body": "Body"},
        }
        issues = self.policy.check_message(
            message, {"notifications_opt_in": "true"}, self.at,
            catalogue=self.catalogue, app_dialect="d1",
        )
        self.assertIn("unknown_message_title", issues)


class ReplayLogicTests(unittest.TestCase):
    def test_decision_call_limits_and_tool_failure_fallback(self):
        budget = DecisionCallBudget(model_limit=1, tool_limit=1)
        provider = MockModelProvider({})
        budget.call_model(provider, event("budget-trigger"))
        with self.assertRaisesRegex(RuntimeError, "model call limit"):
            budget.call_model(provider, event("budget-trigger-2"))

        result, error = UserAgent._call_tool(
            budget, lambda: (_ for _ in ()).throw(TimeoutError("slow tool"))
        )
        self.assertIsNone(result)
        self.assertEqual(error, "tool execution failed")
        result, error = UserAgent._call_tool(budget, lambda: "not called")
        self.assertIsNone(result)
        self.assertEqual(error, "tool call limit exceeded")

    def test_model_provider_timeout_falls_back_without_action(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            trigger = event("provider-timeout-trigger")
            agent = UserAgent(PACK, database, {"U-00001": "agent"}, {})
            agent.model.recommend = lambda item: (_ for _ in ()).throw(
                TimeoutError("provider timeout")
            )
            decision, action = agent.process_trigger(trigger, [])
            self.assertIsNone(action)
            self.assertEqual(decision["reason"], "model_provider_unavailable")
            self.assertEqual(decision["evidence"]["model_calls"], 1)

    def test_database_read_failure_records_safe_no_action(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            original = database.get_goal
            calls = 0

            def fail_once(user_id):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise sqlite3.OperationalError("temporary read failure")
                return original(user_id)

            database.get_goal = fail_once
            agent = UserAgent(PACK, database, {"U-00001": "agent"}, {})
            decision, action = agent.process_trigger(event("db-read-error"), [])
            self.assertIsNone(action)
            self.assertEqual(decision["reason"], "database_unavailable")
            self.assertEqual(
                database.decision_for_trigger("db-read-error")["outcome"],
                "no_action",
            )

    def test_mock_provider_reproduces_faults_without_waiting(self):
        faults = {
            "fault-trigger": {
                "fault": "timeout",
                "seconds": 45,
                "trigger_event_id": "fault-trigger",
            }
        }
        result, fault, details = MockModelProvider(faults).recommend(
            event("fault-trigger")
        )
        self.assertIsNone(result)
        self.assertEqual(fault, "model_timeout")
        self.assertEqual(details["simulated_timeout_seconds"], 45)

    def test_control_correction_updates_open_goal_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            database.save_goal(
                "U-00001", "G03", "2026-10-01T07:00:00Z",
                "2026-10-03T07:00:00Z", "app_open",
                {"event": "app_open", "condition": "old"},
            )
            agent = UserAgent(PACK, database, {}, {})
            control = {
                "type": "goal_rule_corrected",
                "at": "2026-10-01T08:30:00Z",
                "goal_id": "G03",
                "new_success": {
                    "event": "play_started",
                    "condition": "later IST calendar day",
                },
            }
            agent.apply_control(control)
            active = database.get_goal("U-00001")
            self.assertEqual(active["success_event"], "play_started")
            self.assertEqual(active["rule_version"], "control_correction")

    def test_holdout_trigger_always_abstains(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            agent = UserAgent(PACK, database, {"U-00001": "holdout"}, {})
            decision, action = agent.process_trigger(
                event("holdout-trigger"), []
            )
            self.assertIsNone(action)
            self.assertEqual(decision["reason"], "holdout_no_action")
            self.assertEqual(decision["outcome"], "no_action")

    def test_one_persisted_goal_remains_current(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            database.save_goal(
                "U-00001", "G05", "2026-10-01T11:00:00Z",
                "2026-10-08T11:00:00Z", "purchase_completed",
                {"event": "purchase_completed"},
            )
            agent = UserAgent(PACK, database, {}, {})
            trigger = event("current-goal-trigger")
            chosen = agent._start_or_get_goal(
                "U-00001", parse_time(trigger["received_at"]), trigger, {},
            )
            self.assertEqual(chosen[0], "G05")
            self.assertEqual(database.get_goal("U-00001")["goal_id"], "G05")

    def test_injected_fault_is_recorded_and_falls_back_without_action(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            trigger = event("unsupported-claim-trigger")
            fault = {
                "trigger_event_id": trigger["event_id"],
                "fault": "unsupported_claim",
                "model_output": {
                    "goal_id": "G04",
                    "eligible": True,
                    "reason": "billing confirmed",
                },
            }
            agent = UserAgent(
                PACK, database, {"U-00001": "agent"},
                {trigger["event_id"]: fault},
            )
            decision, action = agent.process_trigger(trigger, [])
            self.assertIsNone(action)
            self.assertEqual(decision["reason"], "model_fault:unsupported_claim")
            self.assertEqual(
                database.export_rows("fault_results", ["result"])[0]["result"],
                "safe_fallback_no_action",
            )

    def test_published_page_has_measured_time_and_suppresses_duplicate_page(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "state.db")
            agent = UserAgent(
                PACK, database, {"U-TEST": "agent"}, {}, latency_budget_ms=5000
            )
            agent.profiles["U-TEST"] = [{
                "user_id": "U-TEST",
                "signup_at": "2026-10-01T00:00:00Z",
                "subscription_status": "never",
                "preferred_dialect": "d1",
                "updated_at": "2026-10-01T00:00:00Z",
                "notifications_opt_in": "false",
                "whatsapp_opt_in": "false",
            }]
            first = event("page-trigger-1", "U-TEST")
            first["app_dialect"] = "d1"
            decision, action = agent.process_trigger(first, [])
            self.assertIsNotNone(action)
            self.assertEqual(action["type"], "home_page")
            self.assertIsNotNone(decision["goal_deadline_at"])
            self.assertEqual(
                decision["goal_success_signal"]["event"], "play_started"
            )
            self.assertEqual(decision["confidence"]["level"], "not_estimated")
            self.assertTrue(any(
                check.get("check") == "page_policy"
                and check["result"] == "passed"
                for check in decision["checks"]
            ))
            self.assertIn("history_sources", decision["evidence"])
            self.assertEqual(agent.model_call_limit, 8)
            self.assertEqual(agent.tool_call_limit, 20)
            published_delta = (
                parse_time(action["published_at"]) - parse_time(first["deliver_at"])
            )
            measured_delta = timedelta(milliseconds=decision["latency_ms"])
            self.assertLessEqual(abs(published_delta - measured_delta), timedelta(milliseconds=1))
            self.assertEqual(
                parse_time(action["expires_at"]) - parse_time(action["published_at"]),
                timedelta(minutes=120),
            )
            restarted = UserAgent(
                PACK, Database(Path(folder) / "state.db"),
                {"U-TEST": "agent"}, {}, latency_budget_ms=5000,
            )
            repeated, repeated_action = restarted.process_trigger(first, [])
            self.assertIsNone(repeated_action)
            self.assertEqual(repeated["decision_id"], decision["decision_id"])
            self.assertEqual(len(database.export_rows("actions", ["action_id"])), 1)
            second = event(
                "page-trigger-2", "U-TEST", "login_success",
                "2026-10-01T12:10:00Z",
            )
            second["app_dialect"] = "d1"
            second_decision, second_action = agent.process_trigger(second, [])
            self.assertIsNone(second_action)
            self.assertEqual(
                second_decision["reason"], "active_personalization_still_live"
            )

    def test_replay_items_are_sorted_by_delivery_time(self):
        items = replay_items(PACK)
        times = [item[0] for item in items]
        self.assertEqual(times, sorted(times))
        trigger_items = [item for item in items if item[1] == 2]
        self.assertEqual(len(trigger_items), 2769)


class EvaluationTests(unittest.TestCase):
    def test_g07_requires_play_started_for_same_content_slug(self):
        goal = {
            "goal_id": "G07",
            "success": {"event": "play_started"},
        }
        start = parse_time("2026-10-01T12:00:00Z")
        deadline = parse_time("2026-10-04T12:00:00Z")
        catalogue = {
            "title-same": {"content_slug": "series-a"},
            "title-other": {"content_slug": "series-b"},
        }
        events = [
            {
                "event_id": "wrong-title-play",
                "event": "play_started",
                "received_at": "2026-10-02T12:00:00Z",
                "properties": {"title_id": "title-other"},
            },
            {
                "event_id": "matching-title-completion",
                "event": "episode_completed",
                "received_at": "2026-10-02T13:00:00Z",
                "properties": {"title_id": "title-same"},
            },
        ]
        succeeded, evidence = _success(
            goal, start, deadline, "series-a", events, catalogue
        )
        self.assertFalse(succeeded)
        self.assertEqual(evidence, [])

        events.append({
            "event_id": "matching-series-play",
            "event": "play_started",
            "received_at": "2026-10-02T14:00:00Z",
            "properties": {"title_id": "title-same"},
        })
        succeeded, evidence = _success(
            goal, start, deadline, "series-a", events, catalogue
        )
        self.assertTrue(succeeded)
        self.assertEqual([item["event_id"] for item in evidence],
                         ["matching-series-play"])

    def test_pilot_itt_uses_randomized_assignment(self):
        with tempfile.TemporaryDirectory() as folder:
            report = evaluate_pilot(PACK, Path(folder) / "pilot.json")
            self.assertEqual(report["groups"]["treatment"]["assigned_users_with_outcome"],
                             1912)
            self.assertEqual(report["groups"]["holdout"]["assigned_users_with_outcome"],
                             1979)
            self.assertEqual(report["groups"]["treatment"]["succeeded"], 349)
            self.assertEqual(report["groups"]["holdout"]["succeeded"], 387)
            self.assertFalse(report["positive_impact_supported_by_interval"])

    def test_outcome_verification_is_separate_from_action(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = Database(root / "state.db")
            with (PACK / "replay" / "triggers.jsonl").open(encoding="utf-8") as source:
                trigger = json.loads(next(line for line in source if line.strip()))
            user_id = trigger["user_id"]
            database.save_goal(
                user_id, "G01", "2026-10-01T00:00:00Z",
                "2026-10-02T00:00:00Z", "play_started",
                {"event": "play_started"},
            )
            output = root / "outcomes.jsonl"
            output.write_text(json.dumps({
                "event_id": "verified-play",
                "user_id": user_id,
                "event": "play_started",
                "received_at": "2026-10-01T12:00:00Z",
                "properties": {"title_id": "T0001"},
            }) + "\n", encoding="utf-8")
            actions = root / "actions.jsonl"
            actions.write_text("", encoding="utf-8")
            report = evaluate(
                PACK, database, actions, output, root / "evaluation.json"
            )
            self.assertIn(report["groups"]["agent"]["succeeded"], (0, 1))
            saved = database.export_rows("outcomes", ["category"])
            if report["per_user_outcomes"][user_id]["goal_id"] == "G01":
                self.assertEqual(report["per_user_outcomes"][user_id]["category"],
                                 "succeeded")
                self.assertEqual(saved[0]["category"], "succeeded")
                self.assertEqual(database.get_goal(user_id)["status"], "succeeded")

    def test_corrected_goal_rule_controls_outcome_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = Database(root / "state.db")
            with (PACK / "replay" / "triggers.jsonl").open(encoding="utf-8") as source:
                trigger = json.loads(next(line for line in source if line.strip()))
            user_id = trigger["user_id"]
            database.save_goal(
                user_id, "G03", "2026-10-01T12:00:00Z",
                "2026-10-03T12:00:00Z", "play_started",
                {
                    "event": "play_started",
                    "condition": "on a later IST calendar day",
                    "window_hours": 48,
                },
                rule_version="control_correction",
            )
            outcomes = root / "outcomes.jsonl"
            outcomes.write_text("\n".join([
                json.dumps({
                    "event_id": "same-day-open",
                    "user_id": user_id,
                    "event": "app_open",
                    "received_at": "2026-10-01T13:00:00Z",
                    "properties": {},
                }),
                json.dumps({
                    "event_id": "corrected-play",
                    "user_id": user_id,
                    "event": "play_started",
                    "received_at": "2026-10-01T19:00:00Z",
                    "properties": {"title_id": "T0001"},
                }),
            ]) + "\n", encoding="utf-8")
            actions = root / "actions.jsonl"
            actions.write_text("", encoding="utf-8")
            report = evaluate(
                PACK, database, actions, outcomes, root / "evaluation.json"
            )
            self.assertEqual(
                report["per_user_outcomes"][user_id]["category"], "succeeded"
            )
            self.assertEqual(
                report["per_user_outcomes"][user_id]["evidence"][0]["event"],
                "play_started",
            )


if __name__ == "__main__":
    unittest.main()
