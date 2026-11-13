"""E-mail intake: a mailbox as trigger source.

Standard library only (imaplib + email). The runtime polls a
:class:`MailSource`; every fetched message becomes a trigger payload for
templates whose trigger declares ``source = "email"``. Attachments are
saved into the case file store *before* the case starts, so nothing is
lost between fetch and start.

:class:`ImapSource` is the real implementation (IMAP4 over SSL, UNSEEN
messages, marked seen after a successful hand-over). Tests use a fake -
IMAP servers are integration territory, the contract is the protocol.

Credentials come from the environment only (AITMATION_IMAP_*); they never
appear in a template, a payload or a model context (ARCHITECTURE §7).
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import mimetypes
import re
import smtplib
import ssl
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Protocol

log = logging.getLogger("aitmation.mail")


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    sender: str
    subject: str
    body_text: str
    attachments: list[Path] = field(default_factory=list)
    # To, Cc and Delivered-To addresses, lower case - which address of the
    # business the mail went to (belege@…, rechnungen@…) routes it.
    recipients: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "source": "email",
            "message_id": self.message_id,
            "sender": self.sender,
            "subject": self.subject,
            "body_text": self.body_text,
            "attachments": [str(p) for p in self.attachments],
            "recipients": list(self.recipients),
        }


class MailSource(Protocol):
    """Something that yields new messages exactly once each."""

    def fetch_new(self, attachment_dir: Path) -> list[MailMessage]: ...


class ImapSource:
    """UNSEEN messages from one IMAP folder, marked seen after hand-over."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        folder: str = "INBOX",
        port: int = 993,
    ) -> None:
        self.host, self.port = host, port
        self.user, self._password = user, password
        self.folder = folder

    def check(self) -> None:
        """Login and folder selection only - the setup UI's connection test."""
        with imaplib.IMAP4_SSL(self.host, self.port) as imap:
            imap.login(self.user, self._password)
            status, _ = imap.select(self.folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"folder {self.folder!r} not selectable")

    def fetch_new(self, attachment_dir: Path) -> list[MailMessage]:
        out: list[MailMessage] = []
        with imaplib.IMAP4_SSL(self.host, self.port) as imap:
            imap.login(self.user, self._password)
            imap.select(self.folder)
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                log.error("IMAP search failed: %s", status)
                return out
            for num in data[0].split():
                status, parts = imap.fetch(num, "(RFC822)")
                if status != "OK" or not parts or parts[0] is None:
                    log.error("IMAP fetch failed for message %s", num)
                    continue
                message = parse_message(parts[0][1], attachment_dir)
                out.append(message)
                imap.store(num, "+FLAGS", "\\Seen")
        return out


def parse_message(raw: bytes, attachment_dir: Path) -> MailMessage:
    """Parse an RFC822 message; save attachments into ``attachment_dir``."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    body_text = body.get_content() if body else ""

    attachments: list[Path] = []
    attachment_dir.mkdir(parents=True, exist_ok=True)
    for part in msg.iter_attachments():
        name = part.get_filename() or "attachment.bin"
        # Never trust a filename from the wire: strip any path components.
        safe_name = Path(name).name or "attachment.bin"
        target = attachment_dir / f"{uuid.uuid4().hex}_{safe_name}"
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        target.write_bytes(payload)
        attachments.append(target)

    from email.utils import getaddresses

    headers = [str(v) for key in ("To", "Cc", "Delivered-To") for v in msg.get_all(key, [])]
    recipients = sorted({addr.strip().lower() for _, addr in getaddresses(headers) if addr})
    return MailMessage(
        message_id=msg.get("Message-Id", f"<missing-{uuid.uuid4().hex}>").strip(),
        sender=str(msg.get("From", "")),
        subject=str(msg.get("Subject", "")),
        body_text=body_text if isinstance(body_text, str) else "",
        attachments=attachments,
        recipients=recipients,
    )


def addressed_to(recipients: list[str], wanted: str) -> bool:
    """Whether a mail went to one of ``wanted`` (comma separated): a full
    address, ``@domain``, or a local part (``belege`` matches
    ``belege@firma.ch`` and the plus form ``info+belege@firma.ch``)."""
    for want in (w.strip().lower() for w in wanted.split(",")):
        if not want:
            continue
        for rcpt in recipients:
            local, _, domain = rcpt.partition("@")
            if "@" in want and not want.startswith("@"):
                if rcpt == want:
                    return True
            elif want.startswith("@"):
                if f"@{domain}" == want:
                    return True
            elif local == want or local.split("+", 1)[-1] == want or local.endswith("+" + want):
                return True
    return False


# -- outbound (ADR-0048) -----------------------------------------------------
#
# The ``mail`` action of a process: one message, recipients and text bound
# like tool inputs, attachments only from the case's own files. It has an
# outside effect, so it sits behind a gate (ADR-0007) and is stubbed or
# logged under the ``test``/``shadow`` configurations like any external
# tool. The SMTP settings are the notification settings (one sender per
# instance); the recipient allowlist is the operator's, never the
# process's.

ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
MAX_RECIPIENTS = 10


class MailError(ValueError):
    """The message cannot be built or sent as requested."""


@dataclass(frozen=True)
class OutgoingMail:
    to: tuple[str, ...]
    subject: str
    body: str
    cc: tuple[str, ...] = ()
    reply_to: str = ""
    in_reply_to: str = ""
    attachments: tuple[Path, ...] = ()

    @property
    def recipients(self) -> tuple[str, ...]:
        return self.to + self.cc


def addresses(value: object, *, limit: int = MAX_RECIPIENTS) -> tuple[str, ...]:
    """``"a@x.ch, b@y.ch"``, a list, or a display form ``Name <a@x.ch>`` →
    bare addresses, each checked, duplicates dropped."""
    raw: list[str]
    if isinstance(value, str):
        raw = [v for v in re.split(r"[,;\n]", value) if v.strip()]
    elif isinstance(value, list | tuple):
        raw = [str(v) for v in value if str(v).strip()]
    else:
        raise MailError(f"recipients: expected text or a list, got {type(value).__name__}")
    out: list[str] = []
    for item in raw:
        _name, addr = parseaddr(item.strip())
        addr = addr.strip().lower()
        if not ADDRESS.match(addr):
            raise MailError(f"recipient {item.strip()!r} is not an address")
        if addr not in out:
            out.append(addr)
    if len(out) > limit:
        raise MailError(f"more than {limit} recipients")
    return tuple(out)


def recipients_allowed(recipients: tuple[str, ...], allowlist: tuple[str, ...]) -> str | None:
    """The first recipient outside the operator's allowlist (addresses or
    ``@domain`` entries), or None. An empty allowlist allows everything."""
    if not allowlist:
        return None
    entries = {e.strip().lower() for e in allowlist if e.strip()}
    for addr in recipients:
        domain = "@" + addr.rsplit("@", 1)[-1]
        if addr not in entries and domain not in entries:
            return addr
    return None


def build_message(sender: str, mail: OutgoingMail, *, max_attachment_bytes: int) -> EmailMessage:
    if not mail.to:
        raise MailError("no recipient")
    if not mail.subject.strip():
        raise MailError("empty subject")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(mail.to)
    if mail.cc:
        message["Cc"] = ", ".join(mail.cc)
    if mail.reply_to:
        message["Reply-To"] = mail.reply_to
    if mail.in_reply_to:
        message["In-Reply-To"] = mail.in_reply_to
        message["References"] = mail.in_reply_to
    message["Subject"] = " ".join(mail.subject.split())[:200]
    message.set_content(mail.body)
    total = 0
    for path in mail.attachments:
        if not path.is_file():
            raise MailError(f"attachment {path.name!r} is not a file")
        data = path.read_bytes()
        total += len(data)
        if total > max_attachment_bytes:
            raise MailError(f"attachments exceed {max_attachment_bytes // (1024 * 1024)} MB")
        kind, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (kind or "application/octet-stream").partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
    return message


def smtp_deliver(host: str, port: int, security: str, user: str, password: str | None,
                 message: EmailMessage) -> None:
    """Standard-library SMTP: STARTTLS (default) or implicit TLS."""
    try:
        if security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                host, port, timeout=30, context=ssl.create_default_context()
            )
        else:
            client = smtplib.SMTP(host, port, timeout=30)
        with client:
            if security == "starttls":
                client.starttls(context=ssl.create_default_context())
            if user and password:
                client.login(user, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{host}:{port}: {exc}") from exc


__all__ = [
    "ImapSource",
    "MailError",
    "MailMessage",
    "MailSource",
    "OutgoingMail",
    "addressed_to",
    "addresses",
    "build_message",
    "parse_message",
    "recipients_allowed",
    "smtp_deliver",
]
