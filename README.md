# pipeline-watch

A dead-man watchman for automation pipelines — the monitoring that catches the *quiet*
failures, which are the expensive ones.

```
$ python3 pipeline_watch.py --once
[2026-08-17 15:06:55] checking 4 target(s)
  OK   llm-inference       ok (200, 42ms)
  OK   browser-cdp        ok (200, 15ms)
  OK   trade-journal      baseline 2286175879 bytes
  OK   nightly-etl         beat 599 min ago
  -> 0 failing
```

## The problem it exists for

Uptime monitors catch loud failures: the server is down, the endpoint 500s. Everyone has
that. But automation pipelines mostly die *quietly*:

- the API returns **200 OK with an empty payload** — green dashboard, no data
- the nightly job **stopped running three weeks ago** — nothing errored, nothing ran
- the ingest log **exists and is fresh but stopped growing** — the writer died, the file
  didn't
- the cron **exits 0 without doing anything** — success mask on a failed job

None of those page anyone. All of them cost money. This watches for all four.

| Check | Catches |
|---|---|
| `http` | down, wrong status, **200 with the wrong body**, slow |
| `heartbeat` | a job that should have checked in and didn't |
| `growth` | an artifact that exists, is fresh, and stopped growing |
| `command` | anything else — graded on what it **prints**, not its exit code |

## Four design laws, learned running this in production

**1. Verify by artifact, never by exit code.** A cron that exits 0 without writing anything
is a failed job wearing a success mask. Check the *output*.

**2. Alert on the edge, not the state.** Notify on OK→FAIL and FAIL→OK. Never every poll —
that is how humans learn to ignore your alerts, and then the one that matters is ignored
too.

**3. An alert must carry its evidence.** Not "something broke" — *what* was checked, what
was expected, what was actually seen:

```
🔴 client-api-health is FAIL
check: http · https://api.example.com/health
evidence: 200 OK but the body is missing '"status":"ok"' — a healthy-looking dead endpoint
at 2026-08-17 15:06:55
```

That paragraph saves the on-call ten minutes at 3am.

**4. Fail open on the monitor itself.** A bug in the watchman must never masquerade as a
client outage. Check errors are swallowed and reported, not raised as failures.

Plus a **flap guard**: a check must agree with itself N consecutive polls before it pages,
so a single blip doesn't wake anyone.

## Usage

```bash
cp checks.example.json checks.json     # point it at real targets
python3 pipeline_watch.py --once       # single pass, exit 1 if anything fails
python3 pipeline_watch.py              # daemon, --interval seconds

*/5 * * * * /usr/bin/python3 /path/to/pipeline_watch.py --once
```

State persists at `~/.local/share/pipeline-watch/state.json`. Alerts route to Telegram in
this build; `alert()` is four lines to swap for Slack, PagerDuty, or SMS.

No third-party dependencies. Python 3.9+.

MIT licensed.
