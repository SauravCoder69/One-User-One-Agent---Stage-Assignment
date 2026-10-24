import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "agent.db"


class Database:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
            """)
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS event_payloads (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT,
                    sent_at TEXT,
                    received_at TEXT NOT NULL,
                    deliver_at TEXT,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_payloads_user_received
                    ON event_payloads(user_id, received_at);
                CREATE TABLE IF NOT EXISTS goal_instances (
                    user_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    success_event TEXT,
                    condition_json TEXT NOT NULL,
                    content_slug TEXT,
                    rule_version TEXT NOT NULL DEFAULT 'source'
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    trigger_event_id TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    goal_id TEXT,
                    arm TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS decisions_user_trigger
                    ON decisions(user_id, trigger_event_id);
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS controls (
                    control_key TEXT PRIMARY KEY,
                    control_type TEXT NOT NULL,
                    at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fault_results (
                    trigger_event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    fault TEXT NOT NULL,
                    result TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    user_id TEXT PRIMARY KEY,
                    goal_id TEXT,
                    category TEXT NOT NULL CHECK(category IN
                        ('succeeded', 'failed', 'inconclusive')),
                    verified_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_metrics (
                    metric TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
            """)

    def ingest_events(self, events, source):
        rows = []
        for event in events:
            required = ("event_id", "user_id", "event", "received_at")
            missing = [key for key in required if not event.get(key)]
            if missing:
                raise ValueError("Event missing required fields: " + ", ".join(missing))
            rows.append((
                event["event_id"], event["user_id"], event["event"],
                event.get("occurred_at"), event.get("sent_at"),
                event["received_at"], event.get("deliver_at"), source,
                json.dumps(event, separators=(",", ":"), ensure_ascii=False),
            ))
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany("""
                INSERT OR IGNORE INTO processed_events
                    (event_id, user_id, event_type, received_at)
                VALUES (?, ?, ?, ?)
            """, [(r[0], r[1], r[2], r[5]) for r in rows])
            payload_cursor = connection.executemany("""
                INSERT OR IGNORE INTO event_payloads
                    (event_id, user_id, event_type, occurred_at, sent_at,
                     received_at, deliver_at, source, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            return max(payload_cursor.rowcount, 0)

    def has_event(self, event_id):
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM event_payloads WHERE event_id = ?", (event_id,)
            ).fetchone() is not None

    def events_for_user(self, user_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT payload_json FROM event_payloads
                WHERE user_id = ? ORDER BY received_at, event_id
            """, (user_id,)).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def all_events(self):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT payload_json FROM event_payloads
                ORDER BY received_at, event_id
            """).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def event_source_summary(self, user_id, at):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT event_type, source, COUNT(*) AS event_count,
                       MAX(received_at) AS latest_received_at
                FROM event_payloads
                WHERE user_id = ? AND received_at <= ?
                GROUP BY event_type, source
                ORDER BY event_type, source
            """, (user_id, at)).fetchall()
        return [dict(row) for row in rows]

    def get_goal(self, user_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM goal_instances WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_goal(self, user_id, goal_id, started_at, deadline_at, success_event,
                  condition, content_slug=None, rule_version="source"):
        with self.connect() as connection:
            connection.execute("""
                INSERT INTO goal_instances
                    (user_id, goal_id, started_at, deadline_at, status,
                     success_event, condition_json, content_slug, rule_version)
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    goal_id=excluded.goal_id, started_at=excluded.started_at,
                    deadline_at=excluded.deadline_at, status='open',
                    success_event=excluded.success_event,
                    condition_json=excluded.condition_json,
                    content_slug=excluded.content_slug,
                    rule_version=excluded.rule_version
            """, (user_id, goal_id, started_at, deadline_at, success_event,
                  json.dumps(condition, separators=(",", ":")), content_slug,
                  rule_version))

    def update_goal_rule(self, goal_id, success_event, condition, rule_version):
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE goal_instances
                SET success_event=?, condition_json=?, rule_version=?
                WHERE goal_id=? AND status='open'
            """, (success_event, json.dumps(condition, separators=(",", ":")),
                  rule_version, goal_id))
            return cursor.rowcount

    def close_goal(self, user_id, status):
        with self.connect() as connection:
            connection.execute(
                "UPDATE goal_instances SET status=? WHERE user_id=? AND status='open'",
                (status, user_id),
            )

    def record_decision(self, decision):
        with self.connect() as connection:
            connection.execute("""
                INSERT OR REPLACE INTO decisions
                    (decision_id, user_id, trigger_event_id, decided_at, latency_ms,
                     goal_id, arm, outcome, reason, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decision["decision_id"], decision["user_id"],
                decision["trigger_event_id"], decision["decided_at"],
                decision["latency_ms"], decision.get("goal_id"),
                decision["arm"], decision["outcome"], decision["reason"],
                json.dumps(decision, separators=(",", ":"), ensure_ascii=False),
            ))

    def decision_for_trigger(self, trigger_event_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT details_json FROM decisions WHERE trigger_event_id=?
                ORDER BY decided_at LIMIT 1
            """, (trigger_event_id,)).fetchone()
        return json.loads(row["details_json"]) if row else None

    def record_action(self, action, status="published"):
        with self.connect() as connection:
            connection.execute("""
                INSERT OR IGNORE INTO actions
                    (action_id, decision_id, user_id, action_type, published_at,
                     status, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                action["action_id"], action["decision_id"], action["user_id"],
                action["type"], action["published_at"], status,
                json.dumps(action, separators=(",", ":"), ensure_ascii=False),
            ))

    def record_control(self, control):
        key = control["type"] + "|" + control["at"]
        with self.connect() as connection:
            connection.execute("""
                INSERT OR IGNORE INTO controls
                    (control_key, control_type, at, payload_json)
                VALUES (?, ?, ?, ?)
            """, (key, control["type"], control["at"],
                  json.dumps(control, separators=(",", ":"), ensure_ascii=False)))

    def record_fault(self, trigger_event_id, user_id, fault, result, details=None):
        with self.connect() as connection:
            connection.execute("""
                INSERT OR REPLACE INTO fault_results
                    (trigger_event_id, user_id, fault, result, details_json)
                VALUES (?, ?, ?, ?, ?)
            """, (trigger_event_id, user_id, fault, result,
                  json.dumps(details or {}, separators=(",", ":"))))

    def save_outcome(self, user_id, goal_id, category, verified_at, reason, evidence):
        with self.connect() as connection:
            connection.execute("""
                INSERT OR REPLACE INTO outcomes
                    (user_id, goal_id, category, verified_at, reason, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, goal_id, category, verified_at, reason,
                  json.dumps(evidence, separators=(",", ":"))))

    def save_metric(self, metric, value):
        with self.connect() as connection:
            connection.execute("""
                INSERT OR REPLACE INTO run_metrics(metric, value_json)
                VALUES (?, ?)
            """, (metric, json.dumps(value, separators=(",", ":"))))

    def export_rows(self, table, columns):
        allowed = {
            "decisions": {"decision_id", "user_id", "trigger_event_id", "decided_at",
                          "latency_ms", "goal_id", "arm", "outcome", "reason",
                          "details_json"},
            "actions": {"action_id", "decision_id", "user_id", "action_type",
                        "published_at", "status", "payload_json"},
            "goal_instances": {"user_id", "goal_id", "started_at", "deadline_at",
                               "status", "success_event", "condition_json",
                               "content_slug", "rule_version"},
            "fault_results": {"trigger_event_id", "user_id", "fault", "result",
                              "details_json"},
            "outcomes": {"user_id", "goal_id", "category", "verified_at",
                         "reason", "evidence_json"},
        }
        if table not in allowed or any(column not in allowed[table] for column in columns):
            raise ValueError("Unsupported export selection")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT " + ", ".join(columns) + " FROM " + table
            ).fetchall()
        return [dict(row) for row in rows]


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    return Database(DB_PATH)


def process_event_batch(events):
    return Database(DB_PATH).ingest_events(events, "historical")
