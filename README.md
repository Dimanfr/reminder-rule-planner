# reminder-rule-planner

Turns purchases into a plan of follow-up reminders: which customer, for which
product, when, on which channel. A shop that sells insoles, compression
stockings or service contracts can remind customers at the right moment,
only with their consent, and running the planner again never sends anything
twice.

A pure function in standard-library Python: data in, a list of actions out.
Storing reminders and sending them are left to the caller.

## Why it was built

The source is a CRM reminder service with file-based intake (watchdog). Shops
upload their sales exports, and the service reminds customers to replace or
service what they bought. The planner is where most of the business rules
live, and where mistakes are expensive: a reminder sent twice, at night,
without consent, or months too late makes the shop look careless and can
break data-protection rules.

## How it works

For each purchase:

1. **Rule.** The product name is matched against keyword rules, and the longest
   matching keyword wins ("Sporteinlagen" → sport insoles, 30 days, not
   insoles, 45 days). No match and no default → skipped with a reason.
2. **Already final?** If a reminder for this purchase and rule is `sent`,
   `delivered`, `cancelled` or `failed`, it is kept as it is and never
   planned again.
3. **Consent.** There must be a granted consent with scope `reminders` that
   covers the customer's preferred channel, both today and on the planned
   send date. A consent without a `channels` field covers every channel; an
   explicit empty list covers none. If consent was withdrawn or ends before
   the send date, a pending reminder is cancelled.
4. **Due date.** Purchase date plus the rule's days, counted from when the
   customer bought, not from when the record was imported. If the due date is
   more than `max_overdue_days` in the past, the reminder is skipped (or
   cancelled) instead of being sent late.
5. **Send time.** The channel's send time on the due date (or today if
   slightly overdue), in the shop's time zone, moved past excluded weekdays.
   Send times inside quiet hours are rejected when the configuration is loaded.
6. **Action.** `create`, `update` (time or channel changed), `keep`
   (unchanged), `cancel` or `skip`, always with a reason.

```
$ python3 planner.py --config examples/rules.json --data examples/sample_data.json --today 2026-11-11
skip    P5     C4     service_check  -         -                          no consent for the preferred channel 'whatsapp'
create  P3     C2     compression    sms       2026-11-30T09:30:00+01:00  180 days after purchase
skip    P6     C5     insoles        letter    -                          stale: due 2026-09-15, more than 14 days ago
keep    P8     C1     insoles        email     2026-10-16T10:00:00+02:00  already sent; never re-planned
update  P9     C2     insoles        sms       2026-11-16T09:30:00+01:00  time or channel changed
skip    P4     C3     insoles        -         -                          consent expired
create  P1     C1     insoles        email     2026-12-04T10:00:00+01:00  45 days after purchase
create  P2     C1     insoles_sport  email     2026-11-19T10:00:00+01:00  30 days after purchase
skip    P7     C5     -              -         -                          no rule for this product
```

P3 falls due on Saturday, 28 November, and moves to Monday because weekends
are excluded in the example configuration.

## Run it

Python 3.10+ (tested with 3.14), no dependencies:

```sh
python3 planner.py --config examples/rules.json --data examples/sample_data.json --today 2026-11-11 [--json]
python3 -m unittest discover -s tests -t . -v
```

The example data is synthetic. Among the 16 tests:

- a second run on the planner's own output changes nothing;
- final reminders are never re-planned;
- the due date follows the purchase date;
- a weekend shift across the change to summer time comes out right;
- consent expiry, withdrawal and channel-specific consent are handled;
- invalid configurations are rejected.

## Status

| | |
|---|---|
| **Original use** | Planning step of a CRM reminder service with file-based intake (watchdog), a local prototype from 2025–2026, on a database with a task scheduler. Tested locally with mock providers; never deployed, no real customer data. |
| **This public example** | A rewrite of the planning rules as a pure function, with 16 tests (11/2026). Compared with the original: the due date is counted from the purchase date (the original used the record's creation time); a re-run no longer resets an already sent reminder to pending; consent expiry (also on the send date) and channel-specific consent are checked; stale reminders are skipped; product rules come from configuration (longest match) instead of being hard-coded; send times are time-zone aware. Message texts and sending are not included. |

## Authorship

I define requirements, coordinate AI coding agents, test behaviour and operate the resulting systems; the agents implement the code.

The first version of the reminder service was written by me with Cursor. This
public rewrite was implemented by AI coding agents to my specification.

## Limitations

- Planning works by date. A reminder planned for today at 10:00 that is
  planned at 15:00 is due immediately, and the sending side decides what to
  do with it.
- One reminder per purchase and rule. Repeating reminders (every 6 months)
  are not modelled.
- Keyword rules are simple substring matches on the product name. Product
  IDs or categories from the shop system would be more reliable where they
  exist.
- It does not check legal requirements beyond the consent records it is
  given. Consent can change after planning, so the sending side should check
  it again right before sending.

## Licence

MIT, see `LICENSE`.
