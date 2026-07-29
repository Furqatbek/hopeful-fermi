"""The relay-and-tick loop. One process, one job: keep the queue fed.

Deliberately NOT a cron library. Dramatiq has no scheduler, and the options are a
third dependency, a system crontab that has to be kept in sync with the code, or
about sixty lines here. Sixty lines that live beside what they schedule, in the
same repository, deployed by the same `docker compose up`, is the right answer at
this size — and it means the deploy story stays "two processes".

    python -m app.workers.scheduler     # this loop: relay + periodic ticks
    dramatiq app.workers.actors         # the actor pool

Everything this loop does is enqueue. It holds no business logic and no state
that matters, so it can be killed at any moment: on restart the relay finds the
same undispatched rows and the periodic jobs simply run again — which is safe,
because every one of them is idempotent and guarded by an advisory lock.
"""

from __future__ import annotations

import datetime as dt
import signal
import time

import structlog

log = structlog.get_logger()

# How often each periodic job runs. Chosen against what the user notices:
# a competition state that is 5 s late is invisible; analytics 15 minutes stale
# is fine; a speaking slot that opens 30 s late is a room full of people waiting.
INTERVALS: dict[str, dt.timedelta] = {
    "relay": dt.timedelta(seconds=1),
    "competitions": dt.timedelta(seconds=5),
    "speaking": dt.timedelta(seconds=15),
    "notifications": dt.timedelta(seconds=30),
    "sweep": dt.timedelta(minutes=5),
    "analytics": dt.timedelta(minutes=15),
}

_running = True


def _stop(signum, _frame) -> None:
    """Finish the current pass, then exit. A relay killed mid-batch is safe —
    undispatched rows stay undispatched — but a clean stop keeps the logs
    readable, which matters more than it sounds at 2 a.m."""
    global _running
    _running = False
    log.info("scheduler_stopping", signal=signum)


def run_forever(*, tick_seconds: float = 1.0) -> None:
    from app.workers import broker

    broker.configure()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last: dict[str, dt.datetime] = {}
    log.info("scheduler_started", intervals={k: v.total_seconds()
                                             for k, v in INTERVALS.items()})
    while _running:
        started = dt.datetime.now(dt.UTC)
        for job, interval in INTERVALS.items():
            if job in last and started - last[job] < interval:
                continue
            last[job] = started
            _safely(job)
        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
        time.sleep(max(0.0, tick_seconds - elapsed))
    log.info("scheduler_stopped")


def _safely(job: str) -> None:
    """One job failing must never stop the loop.

    A relay that dies because the analytics refresh raised is a system where a
    reporting bug silently stops student notifications — which is precisely the
    kind of coupling one process is meant to avoid, and it costs a try/except.
    """
    try:
        pass_once(job)
    except Exception as exc:
        log.error("scheduler_job_failed", job=job, error=str(exc)[:300])


def pass_once(job: str) -> None:
    """A single pass of one job. Extracted so tests drive it without the loop."""
    from app.workers import actors
    from app.workers.runtime import unit_of_work

    if job == "relay":
        from app.workers import relay

        with unit_of_work() as session:
            relay.drain(session, actors.dispatch_all)
    elif job == "competitions":
        actors.tick_competitions.send()
    elif job == "speaking":
        actors.match_speaking.send()
    elif job == "notifications":
        actors.deliver_notifications.send()
    elif job == "sweep":
        actors.sweep.send()
    elif job == "analytics":
        actors.refresh_analytics.send()
    else:                                                      # pragma: no cover
        raise KeyError(f"unknown scheduled job {job!r}")


if __name__ == "__main__":                                     # pragma: no cover
    import structlog.contextvars

    structlog.contextvars.bind_contextvars(process="scheduler")
    run_forever()
