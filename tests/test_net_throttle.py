"""Per-host politeness limiter in jobhunt.net."""

from __future__ import annotations

import threading
import time

from jobhunt import net


def _reset(monkeypatch, interval: float) -> None:
    monkeypatch.setattr(net, "MIN_INTERVAL_SECONDS", interval)
    monkeypatch.setattr(net, "_next_slot_by_host", {})


def test_same_host_requests_are_spaced_by_min_interval(monkeypatch):
    _reset(monkeypatch, 0.2)
    t0 = time.monotonic()
    net._throttle("https://a.example.com/one")
    net._throttle("https://A.example.com/two")  # host match is case-insensitive
    net._throttle("https://a.example.com/three")
    assert time.monotonic() - t0 >= 0.4 - 0.01


def test_different_hosts_do_not_wait_on_each_other(monkeypatch):
    _reset(monkeypatch, 0.5)
    t0 = time.monotonic()
    for host in ("x.wd5.myworkdayjobs.com", "y.wd5.myworkdayjobs.com", "z.wd1.myworkdayjobs.com"):
        net._throttle(f"https://{host}/wday/cxs/jobs")
    assert time.monotonic() - t0 < 0.2


def test_concurrent_callers_to_one_host_never_share_a_slot(monkeypatch):
    _reset(monkeypatch, 0.1)
    stamps: list[float] = []
    lock = threading.Lock()

    def hit():
        net._throttle("https://one.example.com/")
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=hit) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(g >= 0.1 - 0.02 for g in gaps), gaps
