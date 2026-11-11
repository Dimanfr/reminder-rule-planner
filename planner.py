"""Plan follow-up reminders from purchases: which customer, which product, when, on which channel.

A shop sells products that need a follow-up after a known interval (insoles
after ~45 days, a service check after a year). This planner turns purchases
into a reminder plan, and it is safe to run again and again:

* the interval comes from configurable product rules (longest keyword match wins);
* the due date is counted from the purchase date, not from when the record was imported;
* a reminder is planned only with a valid consent for reminders on that channel;
* send times avoid quiet hours and configured weekdays, in the shop's time zone;
* reminders that are already sent or cancelled are never touched again;
* purchases so old that the reminder would be long overdue are skipped, not sent at once.

The planner is a pure function: data in, a list of actions out. Storage and
sending belong to the caller. Standard library only.

    plan = plan_reminders(config, purchases, customers, consents, existing, today=date(2026, 11, 11))
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CHANNELS = ("email", "sms", "whatsapp", "letter")
FINAL_STATUSES = {"sent", "delivered", "cancelled", "failed"}


@dataclass(frozen=True)
class Rule:
    key: str
    keywords: tuple[str, ...]
    days: int


@dataclass(frozen=True)
class Config:
    rules: tuple[Rule, ...]
    default_rule: Rule | None          # None: products without a matching rule get no reminder
    timezone: str = "Europe/Berlin"
    send_times: dict | None = None     # channel -> "HH:MM"
    quiet_start: time = time(20, 0)
    quiet_end: time = time(8, 0)
    no_send_weekdays: tuple[int, ...] = (6,)  # Monday = 0 ... Sunday = 6
    max_overdue_days: int = 14


@dataclass(frozen=True)
class Action:
    action: str            # create | update | keep | cancel | skip
    purchase_id: str
    customer_id: str
    rule_key: str | None
    channel: str | None
    send_at: str | None    # ISO 8601 with offset
    reason: str


def load_config(path: Path) -> Config:
    d = json.loads(path.read_text(encoding="utf-8"))
    rules = tuple(Rule(r["key"], tuple(k.casefold() for k in r["keywords"]), int(r["days"])) for r in d["rules"])
    default = d.get("default_days")
    cfg = Config(
        rules=rules,
        default_rule=Rule("default", (), int(default)) if default is not None else None,
        timezone=d.get("timezone", "Europe/Berlin"),
        send_times=d.get("send_times") or {c: "10:00" for c in CHANNELS},
        quiet_start=time.fromisoformat(d.get("quiet_hours", {}).get("start", "20:00")),
        quiet_end=time.fromisoformat(d.get("quiet_hours", {}).get("end", "08:00")),
        no_send_weekdays=tuple(d.get("no_send_weekdays", [6])),
        max_overdue_days=int(d.get("max_overdue_days", 14)),
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    keys = [r.key for r in cfg.rules]
    if len(keys) != len(set(keys)):
        raise ValueError("rule keys must be unique")
    for r in cfg.rules + ((cfg.default_rule,) if cfg.default_rule else ()):
        if r.days <= 0:
            raise ValueError(f"rule {r.key}: days must be > 0")
    if len(set(cfg.no_send_weekdays)) >= 7:
        raise ValueError("no_send_weekdays excludes every day")
    for ch, t in (cfg.send_times or {}).items():
        if ch not in CHANNELS:
            raise ValueError(f"unknown channel {ch!r}")
        if in_quiet_hours(time.fromisoformat(t), cfg):
            raise ValueError(f"send time {t} for {ch} is inside quiet hours")
    ZoneInfo(cfg.timezone)


def match_rule(product: str, cfg: Config) -> Rule | None:
    """The rule whose keyword is the longest substring of the product name; ties go to the earlier rule."""
    name = (product or "").casefold()
    best, best_len = None, 0
    for rule in cfg.rules:
        for kw in rule.keywords:
            if kw and kw in name and len(kw) > best_len:
                best, best_len = rule, len(kw)
    return best or cfg.default_rule


def in_quiet_hours(t: time, cfg: Config) -> bool:
    if cfg.quiet_start <= cfg.quiet_end:
        return cfg.quiet_start <= t < cfg.quiet_end
    return t >= cfg.quiet_start or t < cfg.quiet_end


def send_time(due: date, channel: str, cfg: Config) -> datetime:
    """Local send time on the due date, moved forward past excluded weekdays."""
    tz = ZoneInfo(cfg.timezone)
    t = time.fromisoformat((cfg.send_times or {}).get(channel, "10:00"))
    day = due
    while day.weekday() in cfg.no_send_weekdays:
        day += timedelta(days=1)
    return datetime.combine(day, t, tzinfo=tz)


def consent_channel(customer: dict, consents: list[dict], on: date) -> tuple[str | None, str]:
    """Channel to use on date `on`, or (None, reason). The preferred channel must itself be consented.

    A consent without a "channels" field covers every channel; an explicit empty
    list covers none.
    """
    valid = [c for c in consents
             if c.get("scope") == "reminders" and c.get("status") == "granted"
             and (not c.get("expires_on") or date.fromisoformat(c["expires_on"]) >= on)]
    if not valid:
        expired = any(c.get("scope") == "reminders" and c.get("status") == "granted" for c in consents)
        return None, "consent expired" if expired else "no consent for reminders"
    allowed = set()
    for c in valid:
        channels = c.get("channels")
        allowed |= set(CHANNELS if channels is None else channels)
    preferred = customer.get("preferred_channel")
    if preferred in allowed:
        return preferred, "ok"
    return None, f"no consent for the preferred channel {preferred!r}"


def plan_reminders(cfg: Config, purchases: list[dict], customers: list[dict], consents: list[dict],
                   existing: list[dict], today: date) -> list[Action]:
    by_customer = {c["customer_id"]: c for c in customers}
    consents_by_customer: dict[str, list[dict]] = {}
    for c in consents:
        consents_by_customer.setdefault(c["customer_id"], []).append(c)
    current = {(e["purchase_id"], e["rule_key"]): e for e in existing}

    plan = []
    for p in sorted(purchases, key=lambda p: (p["purchase_date"], p["purchase_id"])):
        pid, cid = p["purchase_id"], p["customer_id"]
        rule = match_rule(p.get("product", ""), cfg)
        if rule is None:
            plan.append(Action("skip", pid, cid, None, None, None, "no rule for this product"))
            continue
        prev = current.get((pid, rule.key))
        if prev and prev.get("status") in FINAL_STATUSES:
            plan.append(Action("keep", pid, cid, rule.key, prev.get("channel"), prev.get("send_at"),
                               f"already {prev['status']}; never re-planned"))
            continue
        customer = by_customer.get(cid)
        if customer is None:
            plan.append(Action("skip", pid, cid, rule.key, None, None, "unknown customer"))
            continue
        channel, why = consent_channel(customer, consents_by_customer.get(cid, []), today)
        if channel is None:
            action = "cancel" if prev else "skip"
            plan.append(Action(action, pid, cid, rule.key, None, None, why))
            continue
        due = date.fromisoformat(p["purchase_date"]) + timedelta(days=rule.days)
        if (today - due).days > cfg.max_overdue_days:
            action = "cancel" if prev else "skip"
            plan.append(Action(action, pid, cid, rule.key, channel, None,
                               f"stale: due {due.isoformat()}, more than {cfg.max_overdue_days} days ago"))
            continue
        send_at = send_time(max(due, today), channel, cfg)
        on_send_day, _ = consent_channel(customer, consents_by_customer.get(cid, []), send_at.date())
        if on_send_day != channel:
            action = "cancel" if prev else "skip"
            plan.append(Action(action, pid, cid, rule.key, channel, None,
                               f"consent ends before the send date {send_at.date().isoformat()}"))
            continue
        at = send_at.isoformat()
        if prev is None:
            plan.append(Action("create", pid, cid, rule.key, channel, at, f"{rule.days} days after purchase"))
        elif prev.get("send_at") == at and prev.get("channel") == channel:
            plan.append(Action("keep", pid, cid, rule.key, channel, at, "unchanged"))
        else:
            plan.append(Action("update", pid, cid, rule.key, channel, at, "time or channel changed"))
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan follow-up reminders (dry run: prints the plan).")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True, help="JSON with purchases, customers, consents, existing")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    d = json.loads(args.data.read_text(encoding="utf-8"))
    plan = plan_reminders(cfg, d["purchases"], d["customers"], d.get("consents", []), d.get("existing", []), args.today)
    if args.json:
        print(json.dumps([asdict(a) for a in plan], indent=1, ensure_ascii=False))
    else:
        for a in plan:
            print(f"{a.action:7} {a.purchase_id:6} {a.customer_id:6} {a.rule_key or '-':14} "
                  f"{a.channel or '-':9} {a.send_at or '-':26} {a.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
