"""Notification queueing and delivery.

Two things here are cost decisions rather than engineering ones, and on this
budget that makes them the important ones.

**Channel selection is a bill.** Telegram is free and SMS is not. Every message
that can go over Telegram does; SMS is reserved for the one case where Telegram
cannot work — a login code for someone who does not yet have a Telegram link.
At 1,500 users a careless notification design is the difference between $0 and
$60 a month, and it is the only line item on the infra bill that grows with the
user count.

**Quiet hours are honoured in the user's own timezone.** A push at 02:00 does not
get read, it gets the app muted. Anything not urgent is deferred to 08:00 local;
`otp` and `safety` are exempt because a login code at 2 a.m. was asked for.
"""

from __future__ import annotations

import datetime as dt
import json
import zoneinfo
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

log = structlog.get_logger()

QUIET_START = 22          # 22:00 local
QUIET_END = 8             # 08:00 local
MAX_ATTEMPTS = 3

# Templates that ignore quiet hours. Deliberately short, and it should stay that
# way: everything on this list is something the user is waiting for right now.
URGENT = frozenset({"auth.otp", "safety.action_taken", "competition.starting_soon"})

# Tiyin per message, for the cost column. SMS is the number to watch.
COST_MINOR = {"sms": 4_500, "telegram": 0, "push": 0, "in_app": 0, "email": 0}


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: int
    locale: str
    timezone: str
    phone: str | None
    telegram_user_id: int | None


def queue(session: Session, *, user_id: int, template: str, params: dict,
          dedupe_key: str | None = None, channel: str | None = None,
          now: dt.datetime | None = None) -> int | None:
    """Insert one notification, deduplicated.

    `dedupe_key` carries a partial UNIQUE index, so a redelivered job cannot
    message anyone twice. That is the mechanism the at-least-once relay depends
    on, not a nicety.
    """
    moment = now or dt.datetime.now(dt.UTC)
    recipient = _recipient(session, user_id)
    if recipient is None:
        return None

    picked = channel or _channel(recipient, template)
    scheduled = _schedule(recipient, template, moment)
    return session.scalar(text("""
        INSERT INTO notifications (user_id, channel, template, params, locale,
                                   dedupe_key, scheduled_at)
        VALUES (:u, :c, :t, CAST(:p AS jsonb), :locale, :d, :at)
        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
        RETURNING id
    """).bindparams(u=user_id, c=picked, t=template, p=json.dumps(params),
                    locale=recipient.locale, d=dedupe_key, at=scheduled))


def _recipient(session: Session, user_id: int) -> Recipient | None:
    row = session.execute(text("""
        SELECT id, locale, timezone, phone, telegram_user_id
        FROM users WHERE id = :u AND deleted_at IS NULL AND status = 'active'
    """).bindparams(u=user_id)).mappings().first()
    if row is None:
        return None
    return Recipient(user_id=row["id"], locale=row["locale"],
                     timezone=row["timezone"] or "Asia/Tashkent",
                     phone=row["phone"], telegram_user_id=row["telegram_user_id"])


def _channel(recipient: Recipient, template: str) -> str:
    """Telegram if we can reach them, SMS only if we must.

    The `must` is exactly one case: a login code for an account with no Telegram
    link. Everything else degrades to in-app, which costs nothing and is read the
    next time they open the app — which for a study product is soon.
    """
    if recipient.telegram_user_id:
        return "telegram"
    if template == "auth.otp" and recipient.phone:
        return "sms"
    return "in_app"


def _schedule(recipient: Recipient, template: str, now: dt.datetime) -> dt.datetime:
    if template in URGENT:
        return now
    try:
        zone = zoneinfo.ZoneInfo(recipient.timezone)
    except Exception:                                          # pragma: no cover
        zone = zoneinfo.ZoneInfo("Asia/Tashkent")
    local = now.astimezone(zone)
    if QUIET_END <= local.hour < QUIET_START:
        return now
    # Next 08:00 local. A notification generated at 23:30 waits eight and a half
    # hours and is read; sent immediately it is dismissed from a lock screen.
    target = local.replace(hour=QUIET_END, minute=0, second=0, microsecond=0)
    if local.hour >= QUIET_START:
        target += dt.timedelta(days=1)
    return target.astimezone(dt.UTC)


# ── delivery ─────────────────────────────────────────────────────────

class Transport:
    """The seam a real provider slots into.

    Kept as a protocol with a logging default so the queue, the retry logic and
    the cost accounting are all exercised by tests without an account anywhere.
    """

    def send(self, *, channel: str, recipient: Recipient, template: str,
             params: dict, locale: str) -> str | None:
        log.info("notification_sent", channel=channel, template=template,
                 user_id=recipient.user_id, locale=locale)
        return None


def deliver(session: Session, transport: Transport, *, now: dt.datetime,
            limit: int = 100) -> tuple[int, int]:
    """Send everything due. Returns (sent, failed).

    `FOR UPDATE SKIP LOCKED` so two workers can drain the queue together without
    sending anything twice — which for SMS would be a duplicated charge as well
    as a duplicated message.
    """
    rows = session.execute(text("""
        SELECT n.id, n.user_id, n.channel, n.template, n.params, n.locale, n.attempts
        FROM notifications n
        WHERE n.status = 'queued' AND n.scheduled_at <= :now AND n.attempts < :max
        ORDER BY n.scheduled_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    """).bindparams(now=now, max=MAX_ATTEMPTS, limit=limit)).mappings().all()

    sent = failed = 0
    for row in rows:
        recipient = _recipient(session, row["user_id"])
        if recipient is None:
            # Deleted or suspended between queueing and sending. Suppressed, not
            # failed: there is nothing to retry.
            session.execute(text(
                "UPDATE notifications SET status = 'suppressed' WHERE id = :id"
            ).bindparams(id=row["id"]))
            continue
        try:
            transport.send(channel=row["channel"], recipient=recipient,
                           template=row["template"], params=row["params"] or {},
                           locale=row["locale"])
        except Exception as exc:
            failed += 1
            session.execute(text("""
                UPDATE notifications
                SET attempts = attempts + 1, failed_reason = :why,
                    status = CASE WHEN attempts + 1 >= :max THEN 'failed'
                                  ELSE 'queued' END,
                    scheduled_at = :next
                WHERE id = :id
            """).bindparams(why=f"{type(exc).__name__}: {exc}"[:300], max=MAX_ATTEMPTS,
                            next=now + dt.timedelta(minutes=5 * (row["attempts"] + 1)),
                            id=row["id"]))
            continue
        sent += 1
        session.execute(text("""
            UPDATE notifications
            SET status = 'sent', sent_at = :now, attempts = attempts + 1,
                cost_minor = :cost
            WHERE id = :id
        """).bindparams(now=now, cost=COST_MINOR.get(row["channel"], 0), id=row["id"]))

    if rows:
        log.info("notifications_delivered", sent=sent, failed=failed)
    return sent, failed


def monthly_sms_cost(session: Session) -> int:
    """Tiyin spent on SMS this month.

    Surfaced because it is the only user-linear line on the infra bill, and the
    first sign of a notification design going wrong is this number moving.
    """
    return int(session.scalar(text("""
        SELECT coalesce(sum(cost_minor), 0) FROM notifications
        WHERE channel = 'sms' AND sent_at >= date_trunc('month', now())
    """)) or 0)
