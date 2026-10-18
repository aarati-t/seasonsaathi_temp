"""Alert delivery (SPEC F2): ALERTS_MODE=local|pubsub.

publish(alert) interface:
- LocalAlerts: single insert into the alerts table (via='local'). Used now.
- PubSubAlerts: publishes to topic PUBSUB_TOPIC (stress-alerts); stub until build block 6 (P6).
receive_push(envelope): the /api/alerts/receive handler. Accepts a Pub/Sub push envelope
{"message": {"data": base64(JSON alert), "messageId": ...}, "subscription": ...}; works locally too.
recent_alert(): dedupe helper (same plot + same stress level within alerts.dedupe_days).
"""
import base64
import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from core.settings import thresholds
from repo.base import Repo

ALERT_FIELDS = ["alert_id", "plot_id", "hh_id", "stress_level", "next_step", "text_mr", "text_hi", "fact_ids",
                "data_as_of", "request_id", "created_at", "via", "message_id"]


def _row(alert: dict, via: str) -> dict:
    row = {k: alert.get(k) for k in ALERT_FIELDS}
    row["via"] = via
    row["created_at"] = row["created_at"] or datetime.now(timezone.utc).isoformat()
    if isinstance(row["fact_ids"], list):
        row["fact_ids"] = json.dumps(row["fact_ids"])
    return row


class AlertSink(ABC):
    via: str

    @abstractmethod
    def publish(self, alert: dict) -> dict:
        """Deliver one alert; returns the stored/sent row."""


class LocalAlerts(AlertSink):
    via = "local"

    def __init__(self, repo: Repo):
        self.repo = repo

    def publish(self, alert: dict) -> dict:
        row = _row(alert, self.via)
        self.repo.insert("alerts", [row])
        return row


class PubSubAlerts(AlertSink):
    """Publishes the alert JSON to topic PUBSUB_TOPIC; the push subscription delivers it to /api/alerts/receive,
    which stores it (one insert, deduped on messageId). Nothing is inserted at publish time."""
    via = "pubsub"

    def __init__(self, topic: str | None = None, project: str | None = None, publisher=None):
        self.topic = topic or os.getenv("PUBSUB_TOPIC", "stress-alerts")
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
        self._publisher = publisher

    @property
    def publisher(self):
        if self._publisher is None:
            from google.cloud import pubsub_v1
            self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    def envelope(self, alert: dict) -> dict:
        """What the push subscription will deliver (used by tests and local replay)."""
        data = base64.b64encode(json.dumps(_row(alert, self.via), ensure_ascii=False, default=str).encode()).decode()
        return {"message": {"data": data, "messageId": alert.get("alert_id")},
                "subscription": f"projects/{self.project}/subscriptions/{self.topic}-push"}

    def publish(self, alert: dict) -> dict:
        row = _row(alert, self.via)
        path = self.publisher.topic_path(self.project, self.topic)
        data = json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")
        message_id = self.publisher.publish(path, data, alert_id=row["alert_id"], plot_id=row["plot_id"]).result(timeout=30)
        return {**row, "message_id": message_id, "published_to": path}


def get_sink(repo: Repo) -> AlertSink:
    return PubSubAlerts() if os.getenv("ALERTS_MODE", "local").lower() == "pubsub" else LocalAlerts(repo)


def decode_push(envelope: dict) -> dict:
    """Pub/Sub push envelope -> alert dict (with message_id). Raises ValueError with a clear message on bad input."""
    msg = (envelope or {}).get("message")
    if not isinstance(msg, dict) or "data" not in msg:
        raise ValueError("not a Pub/Sub push envelope: expected {'message': {'data': <base64 JSON>}}")
    try:
        alert = json.loads(base64.b64decode(msg["data"]).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"message.data is not base64-encoded JSON: {exc}") from None
    if not isinstance(alert, dict) or not alert.get("alert_id") or not alert.get("plot_id"):
        raise ValueError("alert JSON needs alert_id and plot_id")
    alert["message_id"] = str(msg.get("messageId") or msg.get("message_id") or "") or None
    return alert


def receive_push(envelope: dict, repo: Repo) -> dict:
    """Store a pushed alert once. Pub/Sub retries redeliver the same messageId: skipped (and the same alert_id
    is skipped too, e.g. a local replay)."""
    alert = decode_push(envelope)
    if alert["message_id"] and repo.fetch_where("alerts", message_id=alert["message_id"]):
        return {"stored": False, "reason": "duplicate message_id", "alert_id": alert["alert_id"],
                "message_id": alert["message_id"]}
    if repo.fetch_where("alerts", alert_id=alert["alert_id"]):
        return {"stored": False, "reason": "duplicate alert_id", "alert_id": alert["alert_id"]}
    row = _row(alert, "pubsub")
    repo.insert("alerts", [row])
    return {"stored": True, "alert_id": row["alert_id"], "message_id": row["message_id"]}


def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def recent_alert(repo: Repo, plot_id: str, stress_level: str, now: datetime | None = None) -> dict | None:
    """Latest alert for the same plot + stress level within alerts.dedupe_days, else None."""
    days = thresholds()["alerts"]["dedupe_days"]
    now = now or datetime.now(timezone.utc)
    rows = [r for r in repo.fetch_where("alerts", plot_id=plot_id, stress_level=stress_level)
            if r.get("created_at") and now - _as_dt(r["created_at"]) < timedelta(days=days)]
    return max(rows, key=lambda r: _as_dt(r["created_at"])) if rows else None


def alerts_for(repo: Repo, hh_id: str | None = None) -> list[dict]:
    rows = repo.fetch_where("alerts", hh_id=hh_id) if hh_id else repo.fetch_all("alerts")
    out = []
    for r in sorted(rows, key=lambda r: str(r.get("created_at")), reverse=True):
        r = dict(r)
        if isinstance(r.get("fact_ids"), str):
            try:
                r["fact_ids"] = json.loads(r["fact_ids"])
            except ValueError:
                pass
        r["created_at"] = str(r.get("created_at"))
        out.append(r)
    return out
