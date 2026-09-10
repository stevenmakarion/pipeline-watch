#!/usr/bin/env python3
"""Regression tests for the two defects found on 2026-09-09.

Both were found by replaying scenarios against the real module rather than by
reading it, and both are the same family of bug: a monitor that reports health it
has not established.

DEFECT 1 - THE ALERT COULD NEVER FIRE.
    The edge condition was `now != prev and streak >= need`, but streak is reset to
    1 on every status change, so at the exact moment `now != prev` was true, streak
    was always exactly 1. With the shipped default of confirm=2 the test was
    `1 >= 2`. The two clauses were mutually exclusive. A replay of
    healthy -> sustained failure -> recovery produced ZERO alerts on the default
    config and two with confirm=1, which is what made it visible.
    Fixed by separating OBSERVED state (status/streak) from CONFIRMED state, and
    paging when an observation has held `need` polls and differs from the last
    reported state.

DEFECT 2 - THE MONITOR FAILED OPEN.
    An exception inside a check returned ok=True, with the rationale that "monitor
    bugs are not outages". For a monitor that is backwards. An exception means the
    check COULD NOT LOOK, and rendering could-not-look as found-nothing manufactures
    silence that reads as health. Fixed to fail closed.

Run: python3 test_alerting.py
"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("pw", "pipeline_watch.py")
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)

_sent = []
pw.alert = lambda text: _sent.append(text.splitlines()[0])

_outcome = {"ok": True, "raise": False}


def _fake(_cfg):
    if _outcome["raise"]:
        raise RuntimeError("probe exploded")
    return _outcome["ok"], "synthetic", 1


pw.CHECKS["fake"] = _fake
# NOTE: no "confirm" key. These tests deliberately exercise the SHIPPED DEFAULT,
# because the original bug was invisible to anyone who set confirm=1 in their config.
CFG = {"checks": [{"name": "svc", "type": "fake"}]}

T, F = True, False


def replay(sequence):
    _sent.clear()
    state = {}
    for ok, raises in sequence:
        _outcome["ok"], _outcome["raise"] = ok, raises
        pw.run_once(CFG, state, quiet=True)
    return list(_sent), state


def main():
    failures = []

    alerts, _ = replay([(T, F)] * 3 + [(F, F)] * 3 + [(T, F)] * 3)
    if len(alerts) < 2:
        failures.append(f"sustained outage + recovery should page twice, got {len(alerts)}")

    alerts, _ = replay([(T, F)] * 3 + [(F, F)] + [(T, F)] * 3)
    if alerts:
        failures.append(f"a single-poll blip must not page, got {alerts}")

    alerts, state = replay([(T, F)] * 3 + [(F, T)] * 3)
    if not alerts:
        failures.append("a raised check exception must page, got none")
    if state["svc"]["status"] != "fail":
        failures.append(f"a raised exception must leave status=fail, got {state['svc']['status']!r}")

    for f in failures:
        print(f"FAIL  {f}")
    if failures:
        return 1
    print("PASS  4/4 — alerts fire on the shipped default, blips stay silent, "
          "check exceptions fail closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
