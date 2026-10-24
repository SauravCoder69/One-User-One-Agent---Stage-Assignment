import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
TRIGGER_EVENTS = {"app_open", "login_success"}
PLAY_EVENTS = {"play_started"}
SUPPORTED_GOALS = {
    "G01", "G02", "G03", "G04", "G05", "G06",
    "G07", "G08", "G09", "G12",
}


def parse_time(value):
    if not value:
        return None
    if len(value) == 10:
        value += "T00:00:00Z"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stable_bucket(value, modulo):
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


class Catalogue:
    def __init__(self, pack):
        self.by_id = {}
        self.by_slug = defaultdict(list)
        with (pack / "data" / "catalogue.csv").open(
            encoding="utf-8-sig", newline=""
        ) as source:
            for row in csv.DictReader(source):
                title = dict(row)
                title["genres_list"] = [
                    value for value in row["genres"].split("|") if value
                ]
                title["is_premium_bool"] = row["is_premium"].lower() == "true"
                self.by_id[row["title_id"]] = title
                self.by_slug[row["content_slug"]].append(title)

        self.episodes = defaultdict(list)
        with (pack / "data" / "episodes.csv").open(
            encoding="utf-8-sig", newline=""
        ) as source:
            for row in csv.DictReader(source):
                self.episodes[row["content_slug"]].append(row)
        for rows in self.episodes.values():
            rows.sort(key=lambda row: (int(row["season"]), int(row["episode"])))

    def available(self, title_id, at):
        title = self.by_id.get(title_id)
        if not title:
            return False
        start = parse_time(title["available_from"])
        end = parse_time(title["available_until"])
        if start is None or (end is not None and end <= start):
            return False
        return start <= at and (end is None or at < end)

    def dialect_titles(self, dialect, at):
        if not dialect or dialect in {"NA", "d7"}:
            return []
        return [
            title for title in self.by_id.values()
            if title["dialect"] == dialect and self.available(title["title_id"], at)
        ]


class MessagingPolicy:
    FORBIDDEN = ("last chance", "hurry", "only today", "free forever", "guaranteed")

    def __init__(self, pack):
        self.config = json.loads(
            (pack / "config" / "interventions.json").read_text(encoding="utf-8")
        )
        pricing = json.loads(
            (pack / "config" / "pricing.json").read_text(encoding="utf-8")
        )
        self.prices = {str(plan["price_inr"]) for plan in pricing["plans"]}
        self.templates = {
            item["template_id"]: item
            for item in self.config["whatsapp"]["approved_templates"]
        }

    def check_page(self, page, catalogue, at):
        issues = []
        dialect = page.get("dialect")
        if not dialect or dialect not in {"d1", "d2", "d3", "d4", "d5", "d6"}:
            issues.append("unsupported_dialect")
        if len(page.get("rows", [])) > 6:
            issues.append("too_many_rows")
        seen_slugs = set()
        for row in page.get("rows", []):
            title_ids = row.get("title_ids", [])
            if row.get("row_type") not in self.config["home_page"]["rows"]["row_types"]:
                issues.append("unsupported_row_type")
            if not 3 <= len(title_ids) <= 10:
                issues.append("invalid_row_size")
            for title_id in title_ids:
                title = catalogue.by_id.get(title_id)
                if not title:
                    issues.append("unknown_title")
                elif title["dialect"] != dialect:
                    issues.append("title_dialect_mismatch")
                elif not catalogue.available(title_id, at):
                    issues.append("title_unavailable")
                elif title["content_slug"] in seen_slugs:
                    issues.append("duplicate_content_slug")
                else:
                    seen_slugs.add(title["content_slug"])
        hero_id = page.get("hero_title_id")
        if hero_id:
            title = catalogue.by_id.get(hero_id)
            if not title:
                issues.append("unknown_hero")
            elif title["dialect"] != dialect:
                issues.append("hero_dialect_mismatch")
            elif not catalogue.available(hero_id, at):
                issues.append("hero_unavailable")
            elif title["content_slug"] in seen_slugs:
                issues.append("hero_duplicates_row_content")
        return sorted(set(issues))

    def check_message(self, message, profile, at, messages_today=0,
                      catalogue=None, app_dialect=None, opted_out_channels=()):
        issues = []
        channel = message.get("type")
        if channel not in {"push", "whatsapp", "in_app_message"}:
            return ["unsupported_message_type"]
        if channel == "push" and profile.get("notifications_opt_in") != "true":
            issues.append("push_no_consent")
        if channel == "push" and "push" in opted_out_channels:
            issues.append("push_opted_out")
        if channel == "whatsapp" and profile.get("whatsapp_opt_in") != "true":
            issues.append("whatsapp_no_consent")
        if channel == "whatsapp" and "whatsapp" in opted_out_channels:
            issues.append("whatsapp_opted_out")
        if channel in {"push", "whatsapp"}:
            local_time = at.astimezone(IST).time()
            if local_time >= time(22) or local_time < time(8):
                issues.append("quiet_hours")
            if messages_today >= 1:
                issues.append("combined_daily_cap")
        if channel == "in_app_message" and messages_today >= 1:
            issues.append("in_app_daily_cap")

        text = message.get("text", {})
        title_text = text.get("title", "")
        body_text = text.get("body", "")
        if len(title_text) > 40:
            issues.append("title_too_long")
        if len(body_text) > 120:
            issues.append("body_too_long")
        combined = (title_text + " " + body_text).lower()
        if any(phrase in combined for phrase in self.FORBIDDEN):
            issues.append("forbidden_phrase")
        amounts = re.findall(
            r"(?:₹\s*|rs\.?\s*|inr\s*)(\d+)|\b(\d+)\s*(?:inr|rupees?)\b",
            combined,
            re.IGNORECASE,
        )
        mentioned_prices = [first or second for first, second in amounts]
        if any(amount not in self.prices for amount in mentioned_prices):
            issues.append("unsupported_price")

        title_id = message.get("title_id")
        if title_id:
            title = catalogue.by_id.get(title_id) if catalogue else None
            if not title:
                issues.append("unknown_message_title")
            elif app_dialect and title["dialect"] != app_dialect:
                issues.append("message_title_dialect_mismatch")
            elif catalogue and not catalogue.available(title_id, at):
                issues.append("message_title_unavailable")
        elif channel in {"push", "whatsapp"} and message.get("recommends_title"):
            issues.append("message_missing_title")

        if channel == "whatsapp":
            template = self.templates.get(message.get("template_id"))
            if not template:
                issues.append("unapproved_template")
            else:
                variables = message.get("variables", {})
                if set(variables) != set(template["variables"]):
                    issues.append("invalid_template_variables")
                elif catalogue:
                    title_name = variables.get("title_name")
                    if title_name:
                        if not title_id:
                            issues.append("template_title_id_required")
                        matches = [
                            title for title in catalogue.by_id.values()
                            if title["title"] == title_name
                            and title["language"] == template["language"]
                        ]
                        if not matches:
                            issues.append("template_title_not_in_catalogue")
                        elif title_id and not any(
                            title["title_id"] == title_id for title in matches
                        ):
                            issues.append("template_title_id_mismatch")
                    dialect_name = variables.get("dialect_name")
                    if dialect_name and not title_id:
                        issues.append("template_title_id_required")
                    elif dialect_name and title_id:
                        title = catalogue.by_id.get(title_id)
                        if not title or dialect_name != title["dialect"]:
                            issues.append("template_dialect_not_in_catalogue")
                    episode_number = variables.get("episode_number")
                    if episode_number is not None:
                        title = catalogue.by_id.get(title_id) if title_id else None
                        valid_numbers = {
                            episode["episode"] for episode in
                            catalogue.episodes.get(
                                title["content_slug"] if title else "", []
                            )
                        }
                        if not title or str(episode_number) not in valid_numbers:
                            issues.append("template_episode_not_in_catalogue")
                    plan_price = variables.get("plan_price")
                else:
                    plan_price = None
                if template and set(message.get("variables", {})) == set(template["variables"]):
                    variables = message["variables"]
                    if not catalogue and any(
                        name in variables for name in
                        ("title_name", "dialect_name", "episode_number")
                    ):
                        issues.append("template_catalogue_required")
                    if plan_price is not None:
                        price_match = re.search(r"\d+", str(plan_price))
                        if not price_match or price_match.group() not in self.prices:
                            issues.append("template_price_not_in_pricing")
                    if "text" in message:
                        rendered = template["text"]
                        for name in template["variables"]:
                            rendered = rendered.replace(
                                "{{" + name + "}}", str(variables[name])
                            )
                        if message["text"] != rendered:
                            issues.append("unapproved_template_copy")
        return sorted(set(issues))


class MockModelProvider:
    def __init__(self, faults):
        self.faults = faults

    def recommend(self, trigger):
        fault = self.faults.get(trigger["event_id"])
        if fault:
            kind = fault["fault"]
            if kind == "timeout":
                return None, "model_timeout", {
                    "simulated_timeout_seconds": fault.get("seconds", 0)
                }
            return fault.get("model_output"), kind, {}
        return {"action": "home_page"}, None, {}


class CallBudgetExceeded(RuntimeError):
    pass


class ToolCallError(RuntimeError):
    pass


class DecisionCallBudget:
    def __init__(self, model_limit, tool_limit):
        self.model_limit = model_limit
        self.tool_limit = tool_limit
        self.model_calls = 0
        self.tool_calls = 0

    def call_model(self, provider, trigger):
        if self.model_calls >= self.model_limit:
            raise CallBudgetExceeded("model call limit exceeded")
        self.model_calls += 1
        return provider.recommend(trigger)

    def call_tool(self, tool, *args, **kwargs):
        if self.tool_calls >= self.tool_limit:
            raise CallBudgetExceeded("tool call limit exceeded")
        self.tool_calls += 1
        try:
            return tool(*args, **kwargs)
        except (TimeoutError, ConnectionError) as error:
            raise ToolCallError("tool execution failed") from error


class UserAgent:
    def __init__(self, pack, database, arms, faults, latency_budget_ms=200):
        self.pack = pack
        self.database = database
        self.arms = arms
        self.catalogue = Catalogue(pack)
        self.policy = MessagingPolicy(pack)
        self.goal_config = json.loads(
            (pack / "config" / "goals.json").read_text(encoding="utf-8")
        )
        self.goals = {goal["goal_id"]: goal for goal in self.goal_config["goals"]}
        self.profiles = self._load_profiles()
        self.histories = defaultdict(list)
        for event in database.all_events():
            self.histories[event["user_id"]].append(event)
        self.model = MockModelProvider(faults)
        self.latency_budget_ms = latency_budget_ms
        limits = self.policy.config.get("decision_limits", {})
        self.model_call_limit = int(limits.get("max_model_calls", 8))
        self.tool_call_limit = int(limits.get("max_tool_calls", 20))
        self.opted_out = defaultdict(set)
        self.message_sends = defaultdict(set)
        for user_id, events in self.histories.items():
            for event in events:
                properties = event.get("properties") or {}
                if event["event"] == "notification_opt_out":
                    self.opted_out[user_id].add(properties.get("channel"))
                if event["event"] in {"message_delivered", "message_clicked"}:
                    channel = properties.get("channel")
                    if channel in {"push", "whatsapp"}:
                        self.message_sends[user_id].add(
                            (self._event_time(event).astimezone(IST).date(), channel)
                        )

    def _load_profiles(self):
        profiles = defaultdict(list)
        with (self.pack / "data" / "profiles_snapshot.csv").open(
            encoding="utf-8-sig", newline=""
        ) as source:
            for row in csv.DictReader(source):
                profiles[row["user_id"]].append(row)
        return profiles

    @staticmethod
    def _event_time(event):
        return parse_time(event.get("received_at") or event.get("deliver_at"))

    def _profile(self, user_id):
        return self._profile_resolution(user_id)[0]

    def _profile_resolution(self, user_id):
        rows = self.profiles.get(user_id, [])
        if not rows:
            return {}, {"source": "data/profiles_snapshot.csv", "updated_at": None}, []
        selected = max(rows, key=lambda row: (
            row.get("updated_at", ""),
            row.get("subscription_status", ""),
            row.get("preferred_dialect", ""),
        ))
        fields = (
            "subscription_status", "preferred_dialect", "preferred_language",
            "notifications_opt_in", "whatsapp_opt_in",
        )
        conflicts = []
        for field in fields:
            values = sorted({row.get(field, "") for row in rows})
            if len(values) > 1:
                conflicts.append({
                    "field": field,
                    "values": values,
                    "resolution": "latest_updated_at_then_deterministic_tiebreak",
                    "selected_value": selected.get(field, ""),
                })
        evidence = {
            "source": "data/profiles_snapshot.csv",
            "updated_at": selected.get("updated_at"),
            "snapshot_at": selected.get("snapshot_at"),
            "conflicting_rows": len(rows) > 1,
        }
        return selected, evidence, conflicts

    def _recommend(self, budget, trigger):
        try:
            return budget.call_model(self.model, trigger), None, {}
        except CallBudgetExceeded as error:
            return None, "model_call_limit_exceeded", {"error": str(error)}
        except (TimeoutError, ConnectionError) as error:
            return None, "model_provider_unavailable", {"error": str(error)}

    @staticmethod
    def _call_tool(budget, tool, *args, **kwargs):
        try:
            return budget.call_tool(tool, *args, **kwargs), None
        except (ToolCallError, CallBudgetExceeded) as error:
            return None, str(error)

    def _status(self, user_id, now, profile):
        status = profile.get("subscription_status", "unknown")
        updated = parse_time(profile.get("updated_at"))
        latest = None
        for event in self.histories.get(user_id, []):
            at = self._event_time(event)
            if at > now or (updated and at <= updated):
                continue
            if event["event"] in {"purchase_completed", "subscription_renewed"}:
                latest = ("active", at)
            elif event["event"] == "subscription_lapsed":
                latest = ("lapsed", at)
        return latest[0] if latest else status

    def _signup_time(self, user_id, profile):
        candidates = []
        value = parse_time(profile.get("signup_at"))
        if value:
            candidates.append(value)
        candidates.extend(
            self._event_time(event)
            for event in self.histories.get(user_id, [])
            if event["event"] == "signup_completed"
        )
        return min(candidates) if candidates else None

    def _plays(self, user_id, now, exclude_event_id=None):
        return [
            event for event in self.histories.get(user_id, [])
            if event["event"] == "play_started"
            and event["event_id"] != exclude_event_id
            and self._event_time(event) <= now
        ]

    def _eligible(self, goal_id, user_id, now, trigger, profile):
        goal = self.goals[goal_id]
        if goal_id not in SUPPORTED_GOALS:
            return False, "unmeasurable_success_signal"
        history = [
            event for event in self.histories.get(user_id, [])
            if event["event_id"] != trigger["event_id"]
            and self._event_time(event) <= now
        ]
        plays = [event for event in history if event["event"] == "play_started"]
        completions = [
            event for event in history if event["event"] == "episode_completed"
        ]
        signup = self._signup_time(user_id, profile)
        status = self._status(user_id, now, profile)

        if goal_id == "G01":
            return (
                signup is not None
                and timedelta(0) <= now - signup <= timedelta(days=7)
                and not plays,
                "signup_not_within_7_days_or_prior_play",
            )
        if goal_id == "G02":
            return not completions, "prior_episode_completion"
        if goal_id == "G03":
            return (
                signup is not None
                and timedelta(0) <= now - signup <= timedelta(days=2),
                "signup_not_within_2_days",
            )
        if goal_id == "G04":
            return status == "trial_active", "subscription_not_trial_active"
        if goal_id == "G05":
            return status == "never", "subscription_not_never_paid"
        if goal_id == "G06":
            return status == "lapsed", "subscription_not_lapsed"
        if goal_id == "G07":
            played_by_slug = defaultdict(list)
            for event in plays:
                title = self.catalogue.by_id.get(
                    (event.get("properties") or {}).get("title_id")
                )
                if title and title["content_type"] in {"show", "micro_drama"}:
                    played_by_slug[title["content_slug"]].append(event)
            for slug, events in played_by_slug.items():
                last_play = max(self._event_time(event) for event in events)
                episode_ids = {
                    event.get("properties", {}).get("episode_id")
                    for event in completions
                    if self.catalogue.by_id.get(
                        (event.get("properties") or {}).get("title_id"), {}
                    ).get("content_slug") == slug
                }
                all_episodes = {
                    episode["episode_id"] for episode in self.catalogue.episodes[slug]
                }
                if all_episodes - episode_ids and now - last_play >= timedelta(days=3):
                    return True, None, slug
            return False, "no_series_with_unwatched_episode_and_3_day_gap"
        if goal_id == "G08":
            if status != "active":
                return False, "subscription_not_active"
            cutoff = now - timedelta(days=30)
            has_recent_release = any(
                cutoff.date() <= date.fromisoformat(title["release_date"][:10])
                <= now.date()
                for title in self.catalogue.dialect_titles(
                    trigger.get("app_dialect"), now
                )
            )
            return has_recent_release, "no_recent_available_release"
        if goal_id == "G09":
            if status != "active":
                return False, "subscription_not_active"
            days = {
                self._event_time(event).astimezone(IST).date()
                for event in plays
                if now - self._event_time(event) <= timedelta(days=14)
            }
            return len(days) <= 1, "plays_on_more_than_1_day_in_14_days"
        if goal_id == "G12":
            recent_opens = [
                event for event in history
                if event["event"] == "app_open"
                and timedelta(0) <= now - self._event_time(event) < timedelta(days=14)
            ]
            return not recent_opens, "app_open_within_previous_14_days"
        return False, "unsupported_goal"

    def _start_or_get_goal(self, user_id, now, trigger, profile):
        self.last_goal_checks = []
        active = self.database.get_goal(user_id)
        if active and active["status"] == "open":
            if now <= parse_time(active["deadline_at"]):
                self.last_goal_checks.append({
                    "goal_id": active["goal_id"],
                    "result": "reused_open_goal",
                    "deadline_at": active["deadline_at"],
                    "success_signal": active["success_event"],
                })
                return active["goal_id"], None, active.get("content_slug")
            self.database.close_goal(user_id, "expired")

        reasons = []
        for goal in self.goal_config["goals"]:
            result = self._eligible(
                goal["goal_id"], user_id, now, trigger, profile
            )
            if len(result) == 3:
                eligible, reason, slug = result
            else:
                eligible, reason = result
                slug = None
            if eligible:
                success = goal["success"]
                deadline = now + timedelta(hours=success["window_hours"])
                self.database.save_goal(
                    user_id, goal["goal_id"], iso(now), iso(deadline),
                    success.get("event"), success, slug,
                )
                self.last_goal_checks.append({
                    "goal_id": goal["goal_id"],
                    "result": "eligible",
                    "reason": reason,
                })
                return goal["goal_id"], None, slug
            reasons.append({"goal_id": goal["goal_id"], "reason": reason})
            self.last_goal_checks.append({
                "goal_id": goal["goal_id"],
                "result": "ineligible",
                "reason": reason,
            })
        return None, reasons, None

    def _make_page(self, user_id, trigger, goal_id, goal_slug, now):
        profile = self._profile(user_id)
        dialect = trigger.get("app_dialect")
        candidates = self.catalogue.dialect_titles(dialect, now)
        if not candidates:
            return None, "no_available_titles_for_current_dialect"
        preferred_genres = Counter()
        for event in self._plays(user_id, now):
            title = self.catalogue.by_id.get(
                (event.get("properties") or {}).get("title_id")
            )
            if title:
                preferred_genres.update(title["genres_list"])
        payment_goals = {"G04", "G05", "G06"}
        candidates.sort(key=lambda title: (
            0 if goal_slug and title["content_slug"] == goal_slug else 1,
            0 if goal_id in payment_goals and title["is_premium_bool"] else 1,
            -max([preferred_genres[genre] for genre in title["genres_list"]] or [0]),
            title["title_id"],
        ))
        selected = []
        seen = set()
        for title in candidates:
            if title["content_slug"] not in seen:
                selected.append(title)
                seen.add(title["content_slug"])
            if len(selected) == 10:
                break
        if len(selected) < 4:
            return None, "fewer_than_3_valid_titles"

        hero = selected.pop(0)
        page = {
            "type": "home_page",
            "user_id": user_id,
            "dialect": dialect,
            "hero_title_id": hero["title_id"],
            "rows": [{
                "row_type": "next_episode" if goal_slug else "popular_in_dialect",
                "title_ids": [title["title_id"] for title in selected[:10]],
            }],
        }
        issues = self.policy.check_page(page, self.catalogue, now)
        if issues:
            return None, "page_validation_failed:" + ",".join(issues)
        return page, None

    def _has_live_personalization(self, user_id, dialect, now):
        rows = self.database.export_rows("actions", ["payload_json"])
        for row in rows:
            action = json.loads(row["payload_json"])
            published = parse_time(action.get("published_at"))
            expires = parse_time(action.get("expires_at"))
            if (
                action.get("user_id") == user_id
                and action.get("type") == "home_page"
                and action.get("dialect") == dialect
                and published is not None
                and expires is not None
                and published <= now < expires
            ):
                return True
        return False

    def _active_dependency(self, controls, dependency, now):
        for control in controls:
            if control.get("dependency") != dependency:
                continue
            start = parse_time(control["at"])
            end = parse_time(control.get("until"))
            if start <= now and (end is None or now < end):
                return control
        return None

    def apply_control(self, control):
        self.database.record_control(control)
        kind = control["type"]
        if kind == "title_withdrawn":
            title = self.catalogue.by_id.get(control["title_id"])
            if title:
                current_end = parse_time(title["available_until"])
                withdrawn_at = parse_time(control["at"])
                if current_end is None or withdrawn_at < current_end:
                    title["available_until"] = control["at"]
        elif kind == "goal_rule_corrected":
            goal_id = control["goal_id"]
            success = dict(control["new_success"])
            success.setdefault(
                "window_hours", self.goals[goal_id]["success"]["window_hours"]
            )
            self.goals[goal_id]["success"] = success
            self.database.update_goal_rule(
                goal_id, success.get("event"), success, "control_correction"
            )

    def process_trigger(self, trigger, controls):
        user_id = trigger["user_id"]
        started = datetime.now(UTC)
        now = parse_time(trigger.get("deliver_at") or trigger["received_at"])
        arm = self.arms.get(user_id, "unknown")
        goal_id = None
        reason = "no_eligible_measurable_goal"
        details = {}
        checks = [
            {"check": "trigger_type", "result": trigger.get("event")},
            {"check": "randomized_arm", "result": arm},
        ]
        assumptions = []
        conflicts = []
        profile_evidence = None
        budget = DecisionCallBudget(self.model_call_limit, self.tool_call_limit)
        action = None

        database_error = None
        try:
            is_duplicate = self.database.has_event(trigger["event_id"])
        except sqlite3.Error as error:
            is_duplicate = False
            database_error = error
            reason = "database_unavailable"
            details["database_error"] = str(error)
            checks.append({"check": "database", "result": "safe_fallback"})
        if is_duplicate:
            reason = "duplicate_trigger_event_id"
            checks.append({"check": "event_idempotency", "result": "duplicate"})
            previous = self.database.decision_for_trigger(trigger["event_id"])
            if previous:
                return previous, None
        elif database_error is None:
            try:
                self.database.ingest_events([trigger], "trigger")
                self.histories[user_id].append(trigger)
                checks.append({"check": "event_idempotency", "result": "new_event"})
            except sqlite3.Error as error:
                database_error = error
                reason = "database_unavailable"
                details["database_error"] = str(error)
                checks.append({"check": "database", "result": "safe_fallback"})

        injected_fault = None
        if not is_duplicate and trigger["event_id"] in self.model.faults:
            try:
                _, injected_fault, fault_details = budget.call_model(
                    self.model, trigger
                )
            except (TimeoutError, ConnectionError) as error:
                injected_fault = "model_provider_unavailable"
                fault_details = {"error": str(error)}
            except CallBudgetExceeded as error:
                injected_fault = "model_call_limit_exceeded"
                fault_details = {"error": str(error)}
            self.database.record_fault(
                trigger["event_id"], user_id, injected_fault,
                "safe_fallback_no_action", fault_details,
            )
            details.update(fault_details)
            checks.append({"check": "injected_model_fault", "result": injected_fault})

        if database_error is not None:
            pass
        elif arm == "holdout":
            pass
        if trigger.get("event") not in TRIGGER_EVENTS:
            reason = "not_a_decision_trigger"
            checks.append({"check": "supported_trigger", "result": "rejected"})
        elif is_duplicate:
            pass
        else:
            checks.append({"check": "supported_trigger", "result": "accepted"})
            store_control = self._active_dependency(
                controls, "user_data_store", now
            )
            if store_control:
                if stable_bucket(trigger["event_id"], 5) == 0:
                    reason = "user_data_store_timeout"
                    details["simulated_timeout_rate"] = 0.2
                else:
                    reason = "user_data_store_latency_exceeds_budget"
                    details["simulated_read_latency_ms"] = 8000
                checks.append({"check": "user_data_store", "result": reason})
            else:
                try:
                    profile, profile_evidence, conflicts = self._profile_resolution(user_id)
                    checks.append({
                        "check": "profile_snapshot",
                        "result": "selected" if profile else "missing",
                        "source": profile_evidence["source"],
                        "updated_at": profile_evidence["updated_at"],
                    })
                    if conflicts:
                        checks.append({
                            "check": "profile_conflict_resolution",
                            "result": "resolved_deterministically",
                            "fields": [item["field"] for item in conflicts],
                        })
                    goal_id, ineligible, goal_slug = self._start_or_get_goal(
                        user_id, now, trigger, profile
                    )
                    checks.extend(self.last_goal_checks)
                except sqlite3.Error as error:
                    reason = "database_unavailable"
                    details["database_error"] = str(error)
                    checks.append({"check": "database", "result": "safe_fallback"})
                    goal_id, goal_slug, ineligible = None, None, None
                if reason == "database_unavailable":
                    pass
                elif arm == "holdout":
                    reason = "holdout_no_action"
                    checks.append({"check": "holdout_policy", "result": "no_action"})
                    if goal_id is None and ineligible:
                        details["goal_checks"] = ineligible
                elif injected_fault:
                    reason = "model_fault:" + injected_fault
                elif goal_id is None:
                    details["goal_checks"] = ineligible
                else:
                    try:
                        has_live_page = self._has_live_personalization(
                            user_id, trigger.get("app_dialect"), now
                        )
                    except sqlite3.Error as error:
                        has_live_page = False
                        reason = "database_unavailable"
                        details["database_error"] = str(error)
                        checks.append({"check": "database", "result": "safe_fallback"})
                    if reason == "database_unavailable":
                        pass
                    elif has_live_page:
                        reason = "active_personalization_still_live"
                        checks.append({"check": "active_page", "result": "suppressed"})
                    else:
                        checks.append({"check": "active_page", "result": "none"})
                        provider_outage = self._active_dependency(
                            controls, "model_provider", now
                        )
                        if provider_outage:
                            reason = "model_provider_unavailable"
                            checks.append({"check": "model_provider", "result": "outage"})
                        else:
                            model_result, model_error, model_details = self._recommend(
                                budget, trigger
                            )
                            details.update(model_details)
                            output = model_result[0] if model_result else None
                            if model_error:
                                reason = model_error
                                checks.append({"check": "model_call", "result": model_error})
                            elif not isinstance(output, dict) or output.get("action") != "home_page":
                                reason = "invalid_or_unsupported_model_recommendation"
                                checks.append({"check": "model_output", "result": "rejected"})
                            else:
                                checks.append({"check": "model_output", "result": "home_page"})
                                page, page_error = self._make_page(
                                    user_id, trigger, goal_id, goal_slug, now
                                )
                                if page_error:
                                    reason = page_error
                                    checks.append({"check": "page_policy", "result": page_error})
                                else:
                                    reason = "personalized_home_page_for_current_goal"
                                    checks.append({"check": "page_policy", "result": "passed"})
                                    action = page

        elapsed_ms = (datetime.now(UTC) - started).total_seconds() * 1000
        if elapsed_ms > self.latency_budget_ms and action:
            action = None
            reason = "latency_budget_exceeded"
            checks.append({"check": "latency_budget", "result": "exceeded"})
        elif elapsed_ms <= self.latency_budget_ms:
            checks.append({"check": "latency_budget", "result": "within_budget"})

        try:
            goal = self.database.get_goal(user_id)
            history_sources = self.database.event_source_summary(
                user_id, iso(now)
            )
        except sqlite3.Error as error:
            goal = None
            history_sources = []
            action = None
            reason = "database_unavailable"
            details["database_error"] = str(error)
            checks.append({"check": "database", "result": "safe_fallback"})
        if goal:
            goal_id = goal["goal_id"]
            goal_deadline = goal["deadline_at"]
            success_signal = {
                "event": goal["success_event"],
                "condition": json.loads(goal["condition_json"]),
                "rule_version": goal["rule_version"],
            }
            if goal_id == "G06":
                assumptions.append(
                    "G06 lapsed recency is unspecified; lapsed status is used."
                )
            if goal_id == "G07":
                assumptions.append(
                    "G07 unwatched status uses catalogue episodes, completion events, and a three-day play gap."
                )
        else:
            goal_deadline = None
            success_signal = None

        decision_id = "D-" + trigger["event_id"]
        decision = {
            "decision_id": decision_id,
            "user_id": user_id,
            "trigger_event_id": trigger["event_id"],
            "decided_at": iso(datetime.now(UTC)),
            "latency_ms": round(elapsed_ms, 3),
            "latency_budget_ms": self.latency_budget_ms,
            "goal_id": goal_id,
            "arm": arm,
            "outcome": "action" if action and arm != "holdout" else "no_action",
            "reason": reason,
            "goal_deadline_at": goal_deadline,
            "goal_success_signal": success_signal,
            "confidence": {
                "level": "not_estimated",
                "reason": "The mock provider is not probabilistic; outcome confidence is not estimated.",
            },
            "assumptions": assumptions,
            "conflicts": conflicts,
            "checks": checks,
            "evidence": {
                "trigger": {
                    "event_id": trigger["event_id"],
                    "event": trigger.get("event"),
                    "received_at": trigger.get("received_at"),
                    "deliver_at": trigger.get("deliver_at"),
                    "app_dialect": trigger.get("app_dialect"),
                },
                "profile": profile_evidence,
                "history_sources": history_sources,
                "model_calls": budget.model_calls,
                "tool_calls": budget.tool_calls,
            },
            "details": details,
        }
        if action and arm != "holdout":
            action["action_id"] = "A-" + trigger["event_id"]
            action["decision_id"] = decision_id
            action["trigger_event_id"] = trigger["event_id"]
            publish_at = now + timedelta(milliseconds=max(elapsed_ms, 0))
            action["published_at"] = iso(publish_at)
            action["expires_at"] = iso(publish_at + timedelta(minutes=120))
            page_errors = self.policy.check_page(
                action, self.catalogue, publish_at
            )
            if page_errors:
                action = None
                reason = "publication_time_validation_failed:" + ",".join(page_errors)
                decision["outcome"] = "no_action"
                decision["reason"] = reason
                checks.append({"check": "publication_policy", "result": page_errors})
            else:
                action["expires_at"] = iso(publish_at + timedelta(minutes=120))
                action["goal_id"] = goal_id
                action["reason"] = reason
                action["evidence"] = {
                    "trigger_event_id": trigger["event_id"],
                    "app_dialect": trigger.get("app_dialect"),
                    "arm": arm,
                }
                checks.append({"check": "publication_policy", "result": "passed"})
        self.database.record_decision(decision)
        if action and arm != "holdout":
            self.database.record_action(action)
        return decision, action
