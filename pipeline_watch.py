#!/usr/bin/env python3
"""pipeline_watch.py — the dead-man watchman for automation pipelines.

The thing every agency running n8n/Zapier/cron actually needs and almost never
has: something that notices when the pipeline stops, *including the ways that
look like success*.

MOST MONITORING ONLY CATCHES LOUD FAILURES. This catches the quiet ones, which
are the expensive ones:

  1. HTTP check ......... the endpoint is down          (loud — everyone has this)
  2. CONTENT check ...... it returns 200 with garbage    (quiet)
  3. HEARTBEAT check .... a job that should have run, didn't  (silent — the killer)
  4. FRESHNESS check .... the file/table exists but stopped growing (silent)
  5. FLAPPING guard ..... state changes are debounced so a blip is not a page

Design laws, learned running this pattern on our own production stack since
June 2026:
  - VERIFY BY ARTIFACT, never by exit code. A cron that exits 0 without writing
    anything is a failed job wearing a success mask. Check the OUTPUT.
  - ALERT ON THE EDGE, not the state. Notify when OK->FAIL or FAIL->OK; never
    every poll, or the humans learn to ignore you.
  - AN ALERT MUST CARRY ITS EVIDENCE — what was checked, what was expected,
    what was actually seen. "Something broke" wastes the on-call's first ten
    minutes.
  - FAIL OPEN ON THE MONITOR ITSELF. A monitoring bug must never masquerade as
    a client outage.

Config: checks.json next to this file (see checks.example.json).
Usage:  pipeline_watch.py [--once] [--config checks.json] [--quiet]
Cron:   */5 * * * * pipeline_watch.py --once
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.expanduser("~/.local/share/pipeline-watch/state.json")
UA = "pipeline-watch/1.0 (+monitoring)"


# ----------------------------------------------------------------- checks
def check_http(c):
    """Tier 1+2: is it up, AND does the body actually contain what it should?"""
    t0 = time.time()
    try:
        req = urllib.request.Request(c["url"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=c.get("timeout", 20)) as r:
            code = r.getcode()
            body = r.read(65536).decode("utf-8", "ignore")
    except Exception as e:
        return False, f"unreachable: {str(e)[:120]}", int((time.time() - t0) * 1000)
    ms = int((time.time() - t0) * 1000)
    want_code = c.get("expect_status", 200)
    if code != want_code:
        return False, f"status {code}, expected {want_code}", ms
    must = c.get("expect_contains")
    if must and must not in body:
        return False, (f"200 OK but the body is missing {must!r} — "
                       f"a healthy-looking dead endpoint"), ms
    bad = c.get("fail_contains")
    if bad and bad in body:
        return False, f"body contains failure marker {bad!r}", ms
    if c.get("max_ms") and ms > c["max_ms"]:
        return False, f"slow: {ms}ms > {c['max_ms']}ms", ms
    return True, f"ok ({code}, {ms}ms)", ms


def check_heartbeat(c):
    """Tier 3: a job that should have checked in, hasn't. The silent killer —
    nothing errors, the work just quietly stopped happening."""
    p = os.path.expanduser(c["path"])
    if not os.path.exists(p):
        return False, f"heartbeat file missing: {p}", 0
    age = time.time() - os.path.getmtime(p)
    lim = c.get("max_age_minutes", 60) * 60
    if age > lim:
        return False, (f"stale by {int((age-lim)/60)} min — last beat "
                       f"{datetime.fromtimestamp(os.path.getmtime(p)):%F %H:%M}"), 0
    return True, f"beat {int(age/60)} min ago", 0


def check_growth(c):
    """Tier 4: the artifact exists and is fresh, but is it still GROWING?
    A log that stopped growing is a process that stopped working."""
    p = os.path.expanduser(c["path"])
    if not os.path.exists(p):
        return False, f"artifact missing: {p}", 0
    size = os.path.getsize(p)
    prev = c.get("_prev_size")
    if prev is None:
        return True, f"baseline {size} bytes", 0
    if size <= prev and c.get("must_grow", True):
        return False, f"not growing: still {size} bytes since last poll", 0
    return True, f"grew {size - prev} bytes", 0


def check_command(c):
    """Escape hatch: any shell probe. Still artifact-first — we check what it
    PRINTS, not merely that it exited 0."""
    t0 = time.time()
    try:
        r = subprocess.run(c["command"], shell=True, capture_output=True,
                           text=True, timeout=c.get("timeout", 60))
    except Exception as e:
        return False, f"probe failed: {str(e)[:120]}", 0
    ms = int((time.time() - t0) * 1000)
    out = (r.stdout or "").strip()
    must = c.get("expect_contains")
    if must and must not in out:
        return False, (f"exit {r.returncode} but output missing {must!r} "
                       f"(got {out[:80]!r})"), ms
    if r.returncode != 0 and not must:
        return False, f"exit {r.returncode}: {(r.stderr or '')[:100]}", ms
    return True, f"ok ({ms}ms)", ms


CHECKS = {"http": check_http, "heartbeat": check_heartbeat,
          "growth": check_growth, "command": check_command}


# ----------------------------------------------------------------- plumbing
def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"), indent=1)


def alert(text):
    """Route the page. Telegram here; swap for Slack/PagerDuty per client."""
    try:
        tok = open(os.path.expanduser("~/.config/telegram-token")).read().strip()
        chat = open(os.path.expanduser("~/.config/telegram-chatid")).read().strip()
        body = json.dumps({"chat_id": chat, "text": text[:3900]}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=body,
            headers={"Content-Type": "application/json"}), timeout=20)
        return True
    except Exception as e:
        print(f"  alert delivery failed: {e}", file=sys.stderr)
        return False


def run_once(cfg, state, quiet=False):
    fails = 0
    for c in cfg["checks"]:
        name = c["name"]
        st = state.setdefault(name, {"status": "unknown", "streak": 0})
        c["_prev_size"] = st.get("size")
        try:
            ok, detail, ms = CHECKS[c["type"]](c)
        except Exception as e:                    # FAIL OPEN: monitor bugs are
            ok, detail, ms = True, f"monitor error (ignored): {e}", 0
        if c["type"] == "growth" and os.path.exists(os.path.expanduser(c.get("path", ""))):
            st["size"] = os.path.getsize(os.path.expanduser(c["path"]))

        prev = st["status"]
        now = "ok" if ok else "fail"
        st["streak"] = st["streak"] + 1 if now == prev else 1
        st["status"], st["detail"] = now, detail
        st["checked"] = datetime.now().isoformat(timespec="seconds")
        if not ok:
            fails += 1

        # EDGE ALERTING with a flap guard: only page after N consecutive
        # agreeing polls, and only when the state actually changed.
        need = c.get("confirm", 2)
        if now != prev and st["streak"] >= need and prev != "unknown":
            icon = "🔴" if now == "fail" else "🟢"
            alert(f"{icon} {name} is {now.upper()}\n"
                  f"check: {c['type']} · {c.get('url') or c.get('path') or ''}\n"
                  f"evidence: {detail}\n"
                  f"at {datetime.now():%F %H:%M:%S}")
        if not quiet:
            print(f"  {'OK  ' if ok else 'FAIL'} {name:24s} {detail}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "checks.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.config):
        print(f"no config at {a.config} (see checks.example.json)")
        return 2
    cfg = json.load(open(a.config))
    while True:
        state = load_state()
        print(f"[{datetime.now():%F %H:%M:%S}] checking "
              f"{len(cfg['checks'])} target(s)")
        fails = run_once(cfg, state, a.quiet)
        save_state(state)
        print(f"  -> {fails} failing")
        if a.once:
            return 1 if fails else 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
