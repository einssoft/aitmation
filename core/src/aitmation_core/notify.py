"""Notifications: the person learns that a decision is waiting.

"The human decides" (principle 2) only works when the human notices.
This module watches the open gates and produces notifications in three
stages - **new** (a gate opened), **reminder** (nobody decided within
``remind_after_hours``), **escalation** (still open after
``escalate_after_hours`` or past the gate's own deadline; goes to the
deputy in ``escalate_to``). A template may tighten the thresholds for its
gates with a ``[policy.sla]`` block (``remind_after_hours``,
``escalate_after_hours``, ``escalate_to``) - bindings may only ever
shorten them (ADR-0014: policies get stricter, never looser).

Every notification is a row in ``notifications`` (migration 0008) before
anything is sent: the shell shows them, tests inspect them, and a mail
that could not be sent is visible instead of lost. Delivery is e-mail
over SMTP (standard library), rebuilt from the settings without a
restart; the password lives in the secrets file.

Texts are rendered here in the instance language (ADR-0022 §3: messages
to people are keys + parameters - for e-mail the rendering has to happen
in the core, so a small table of four languages lives below). Contents
stay minimal: which mandant, which template, since when, where to decide.
No document content ever leaves in a notification.
"""

# ruff: noqa: E501  (message table)
from __future__ import annotations

import logging
import smtplib
import sqlite3
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

log = logging.getLogger("aitmation.notify")

KINDS = ("new", "reminder", "escalation")
MAX_ATTEMPTS = 3

_TEXTS = {
    "de": {
        "new": (
            "Neue Pendenz: {title}",
            "In {instance} wartet eine Entscheidung: {title} ({template}), seit {since}.\n\nEntscheiden: {url}",
        ),
        "reminder": (
            "Erinnerung: {title} wartet seit {hours} Stunden",
            "In {instance} wartet {title} ({template}) seit {since} auf eine Entscheidung.\n\nEntscheiden: {url}",
        ),
        "escalation": (
            "Eskalation: {title} ist seit {hours} Stunden offen",
            "In {instance} ist {title} ({template}) seit {since} nicht entschieden. Sie erhalten diese Nachricht als Vertretung.\n\nEntscheiden: {url}",
        ),
        "test": (
            "aitmation: Testnachricht",
            "Die Benachrichtigungen von {instance} funktionieren. Entscheidungen: {url}",
        ),
        "footer": "\n\nDiese Nachricht enthält keine Dokumentinhalte. aitmation - {instance}",
    },
    "fr": {
        "new": (
            "Nouvelle tâche : {title}",
            "Dans {instance}, une décision est en attente : {title} ({template}), depuis {since}.\n\nDécider : {url}",
        ),
        "reminder": (
            "Rappel : {title} attend depuis {hours} heures",
            "Dans {instance}, {title} ({template}) attend une décision depuis {since}.\n\nDécider : {url}",
        ),
        "escalation": (
            "Escalade : {title} est ouvert depuis {hours} heures",
            "Dans {instance}, {title} ({template}) n'est pas décidé depuis {since}. Vous recevez ce message en tant que suppléant.\n\nDécider : {url}",
        ),
        "test": (
            "aitmation : message de test",
            "Les notifications de {instance} fonctionnent. Décisions : {url}",
        ),
        "footer": "\n\nCe message ne contient aucun contenu de document. aitmation - {instance}",
    },
    "it": {
        "new": (
            "Nuova pratica: {title}",
            "In {instance} è in attesa una decisione: {title} ({template}), da {since}.\n\nDecidere: {url}",
        ),
        "reminder": (
            "Promemoria: {title} attende da {hours} ore",
            "In {instance} {title} ({template}) attende una decisione da {since}.\n\nDecidere: {url}",
        ),
        "escalation": (
            "Escalation: {title} è aperto da {hours} ore",
            "In {instance} {title} ({template}) non è deciso da {since}. Riceve questo messaggio come sostituto.\n\nDecidere: {url}",
        ),
        "test": (
            "aitmation: messaggio di prova",
            "Le notifiche di {instance} funzionano. Decisioni: {url}",
        ),
        "footer": "\n\nQuesto messaggio non contiene contenuti di documenti. aitmation - {instance}",
    },
    "en": {
        "new": (
            "New pending item: {title}",
            "In {instance} a decision is waiting: {title} ({template}), since {since}.\n\nDecide: {url}",
        ),
        "reminder": (
            "Reminder: {title} has been waiting for {hours} hours",
            "In {instance} {title} ({template}) has been waiting for a decision since {since}.\n\nDecide: {url}",
        ),
        "escalation": (
            "Escalation: {title} open for {hours} hours",
            "In {instance} {title} ({template}) has not been decided since {since}. You receive this as the deputy.\n\nDecide: {url}",
        ),
        "test": ("aitmation: test message", "Notifications from {instance} work. Decisions: {url}"),
        "footer": "\n\nThis message carries no document content. aitmation - {instance}",
    },
}


class NotifyError(RuntimeError):
    """Sending failed."""


@dataclass(frozen=True)
class MailSettings:
    host: str = ""
    port: int = 587
    user: str = ""
    sender: str = ""
    security: str = "starttls"  # starttls | ssl | none

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


@dataclass(frozen=True)
class Thresholds:
    remind_after_hours: float = 24.0
    escalate_after_hours: float = 72.0

    def tightened_by(self, sla: dict) -> Thresholds:
        """A template's [policy.sla] may only shorten the waiting times."""
        remind = self.remind_after_hours
        escalate = self.escalate_after_hours
        try:
            if "remind_after_hours" in sla:
                remind = min(remind, float(sla["remind_after_hours"]))
            if "escalate_after_hours" in sla:
                escalate = min(escalate, float(sla["escalate_after_hours"]))
        except (TypeError, ValueError):
            pass
        return Thresholds(remind, escalate)


Sender = Callable[[MailSettings, str | None, str, str, str], None]
"""(settings, password, to, subject, body) -> None; raises NotifyError."""


def smtp_send(
    settings: MailSettings, password: str | None, to: str, subject: str, body: str
) -> None:
    """Plain SMTP through the standard library. STARTTLS by default."""
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        if settings.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=20, context=ssl.create_default_context()
            )
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=20)
        with client:
            if settings.security == "starttls":
                client.starttls(context=ssl.create_default_context())
            if settings.user and password:
                client.login(settings.user, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise NotifyError(f"{settings.host}:{settings.port}: {exc}") from exc


def render(kind: str, lang: str, **params) -> tuple[str, str]:
    table = _TEXTS.get(lang) or _TEXTS["de"]
    subject, body = table[kind]
    return subject.format(**params), body.format(**params) + table["footer"].format(**params)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _recipients(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


class Notifier:
    """Decides what to notify about and sends what is pending."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        instance_title: str,
        language: str,
        base_url: str,
        recipients: str,
        deputies: str,
        thresholds: Thresholds,
        mail: MailSettings,
        password: str | None,
        sender: Sender | None = None,
    ) -> None:
        self.conn = conn
        self.instance_title = instance_title
        self.language = language if language in _TEXTS else "de"
        self.base_url = base_url.rstrip("/")
        self.recipients = _recipients(recipients)
        self.deputies = _recipients(deputies) or self.recipients
        self.thresholds = thresholds
        self.mail = mail
        self.password = password
        self.send = sender or smtp_send

    @property
    def active(self) -> bool:
        return self.mail.configured and bool(self.recipients)

    # -- deciding ----------------------------------------------------------

    def observe(
        self, gates: list[dict], sla_of: Callable[[str], dict], now: datetime | None = None
    ) -> int:
        """Look at the open gates and queue what is due. Returns queued count.

        ``gates`` are the runtime's open gates (case_id, step_id, started_at,
        deadline_at, template_id, title); ``sla_of(template_id)`` returns the
        template's sla policy block.
        """
        now = now or datetime.now(UTC)
        queued = 0
        for gate in gates:
            started = _parse(gate["started_at"])
            age_hours = (now - started).total_seconds() / 3600.0
            thresholds = self.thresholds.tightened_by(sla_of(gate["template_id"]) or {})
            overdue = gate.get("deadline_at") and _parse(gate["deadline_at"]) <= now
            due: list[tuple[str, list[str]]] = [("new", self.recipients)]
            if age_hours >= thresholds.remind_after_hours:
                due.append(("reminder", self.recipients))
            if overdue or age_hours >= thresholds.escalate_after_hours:
                due.append(("escalation", self.deputies))
            for kind, recipients in due:
                if self._exists(gate["case_id"], gate["step_id"], kind):
                    continue
                subject, body = render(
                    kind,
                    self.language,
                    title=gate.get("title") or gate["step_id"],
                    template=gate["template_id"],
                    instance=self.instance_title,
                    since=started.astimezone().strftime("%d.%m.%Y %H:%M"),
                    hours=int(age_hours),
                    url=f"{self.base_url}/app/" if self.base_url else "/app/",
                )
                for to in recipients or ["-"]:
                    self._queue(kind, gate["case_id"], gate["step_id"], to, subject, body, now)
                    queued += 1
        return queued

    def _exists(self, case_id: str, step_id: str, kind: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM notifications WHERE case_id = ? AND step_id = ? AND kind = ? LIMIT 1",
            (case_id, step_id, kind),
        ).fetchone()
        return row is not None

    def _queue(self, kind, case_id, step_id, to, subject, body, now) -> None:
        status = "pending" if self.active and to != "-" else "unsent"
        self.conn.execute(
            "INSERT INTO notifications (id, at, kind, case_id, step_id, recipient, subject, body,"
            " status, attempts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (uuid.uuid4().hex, _iso(now), kind, case_id, step_id, to, subject, body, status),
        )
        self.conn.commit()

    # -- sending -----------------------------------------------------------

    def deliver(self, limit: int = 20) -> int:
        """Send pending notifications; failures are recorded and retried up
        to MAX_ATTEMPTS, then left visible as failed."""
        if not self.active:
            return 0
        rows = self.conn.execute(
            "SELECT id, recipient, subject, body, attempts FROM notifications"
            " WHERE status = 'pending' AND attempts < ? ORDER BY at LIMIT ?",
            (MAX_ATTEMPTS, limit),
        ).fetchall()
        sent = 0
        for row in rows:
            try:
                self.send(self.mail, self.password, row["recipient"], row["subject"], row["body"])
            except NotifyError as exc:
                attempts = row["attempts"] + 1
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                self.conn.execute(
                    "UPDATE notifications SET attempts = ?, status = ?, error = ? WHERE id = ?",
                    (attempts, status, str(exc)[:300], row["id"]),
                )
                self.conn.commit()
                log.warning(
                    "notification to %s failed (%d/%d): %s",
                    row["recipient"],
                    attempts,
                    MAX_ATTEMPTS,
                    exc,
                )
                continue
            self.conn.execute(
                "UPDATE notifications SET status = 'sent', sent_at = ?, attempts = attempts + 1,"
                " error = NULL WHERE id = ?",
                (_iso(datetime.now(UTC)), row["id"]),
            )
            self.conn.commit()
            sent += 1
        return sent

    def send_test(self, to: str) -> None:
        subject, body = render(
            "test",
            self.language,
            instance=self.instance_title,
            url=f"{self.base_url}/app/" if self.base_url else "/app/",
        )
        self.send(self.mail, self.password, to, subject, body)


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, at, kind, case_id, step_id, recipient, subject, status, attempts, sent_at,"
        " error FROM notifications ORDER BY at DESC LIMIT ?",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [dict(r) for r in rows]


def state_of(conn: sqlite3.Connection, case_id: str, step_id: str) -> dict:
    """Which stages a gate has reached - for the pending list."""
    rows = conn.execute(
        "SELECT kind, MIN(at) AS at FROM notifications WHERE case_id = ? AND step_id = ?"
        " GROUP BY kind",
        (case_id, step_id),
    ).fetchall()
    return {r["kind"]: r["at"] for r in rows}


def cleanup(conn: sqlite3.Connection, retention_days: int) -> int:
    cutoff = _iso(datetime.now(UTC) - timedelta(days=retention_days))
    cursor = conn.execute("DELETE FROM notifications WHERE at < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount
