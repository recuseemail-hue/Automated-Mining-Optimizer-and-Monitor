"""Deterministic scoring of telemetry windows. No LLM involved anywhere in here.

A Window summarises a set of gpu_samples rows. compare() judges a trial window against a baseline
window for a goal and returns a verdict the trial manager and optimizer act on.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from typing import Iterable

UNSTABLE_THROTTLE = {"hw_slowdown", "hw_thermal", "power_brake"}


@dataclass
class Window:
    n: int = 0
    hashrate: float | None = None     # median H/s
    power_w: float | None = None      # mean
    eff: float | None = None          # H/s per W
    core_max: float | None = None
    memj_max: float | None = None
    rej_rate: float | None = None     # rejected / (accepted + rejected) over the window delta
    acc_delta: int = 0
    rej_delta: int = 0
    throttle_hits: int = 0
    span_s: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def window_stats(rows: Iterable) -> Window:
    rows = list(rows)
    w = Window(n=len(rows))
    if not rows:
        return w
    hr = [r["hashrate"] for r in rows if r["hashrate"] is not None]
    pw = [r["power_w"] for r in rows if r["power_w"] is not None]
    core = [r["core_c"] for r in rows if r["core_c"] is not None]
    memj = [r["memj_c"] for r in rows if r["memj_c"] is not None]
    w.hashrate = statistics.median(hr) if hr else None
    w.power_w = statistics.fmean(pw) if pw else None
    w.eff = (w.hashrate / w.power_w) if (w.hashrate is not None and w.power_w) else None
    w.core_max = max(core) if core else None
    w.memj_max = max(memj) if memj else None
    acc = [r["accepted"] for r in rows if r["accepted"] is not None]
    rej = [r["rejected"] for r in rows if r["rejected"] is not None]
    if len(acc) >= 2:
        w.acc_delta = max(0, acc[-1] - acc[0])
    if len(rej) >= 2:
        w.rej_delta = max(0, rej[-1] - rej[0])
    tot = w.acc_delta + w.rej_delta
    w.rej_rate = (w.rej_delta / tot) if tot else (0.0 if acc else None)
    for r in rows:
        t = r["throttle"]
        if t:
            try:
                flags = set(json.loads(t)) if isinstance(t, str) else set(t)
            except ValueError:
                flags = set()
            if flags & UNSTABLE_THROTTLE:
                w.throttle_hits += 1
    w.span_s = rows[-1]["ts"] - rows[0]["ts"]
    return w


def score(w: Window, goal: str) -> float | None:
    if goal == "max-hashrate":
        return w.hashrate
    if goal == "target-temp":
        return w.eff
    return w.eff          # max-efficiency and the hourly default


@dataclass
class Verdict:
    verdict: str                      # better|worse|same|unstable|insufficient
    delta_pct: float | None
    reasons: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def compare(base: Window, trial: Window, *, goal: str = "max-efficiency", targets: dict | None = None,
            min_improve_pct: float = 1.0, worse_margin_pct: float = 3.0, min_samples: int = 4,
            max_rej_rate_increase: float = 0.02) -> Verdict:
    targets = targets or {}
    reasons: list[str] = []
    if trial.n < min_samples or base.n < min_samples:
        return Verdict("insufficient", None, [f"samples base={base.n} trial={trial.n} < {min_samples}"])

    # stability first: any of these ends the trial regardless of score
    if trial.throttle_hits > 0 and trial.throttle_hits > base.throttle_hits:
        reasons.append(f"hw throttle flags in {trial.throttle_hits} samples")
    if trial.rej_rate is not None and base.rej_rate is not None and trial.rej_rate - base.rej_rate > max_rej_rate_increase:
        reasons.append(f"reject rate {trial.rej_rate:.3f} vs {base.rej_rate:.3f}")
    if trial.hashrate is not None and base.hashrate and trial.hashrate < base.hashrate * (1 - worse_margin_pct / 100) \
            and goal != "target-temp":
        reasons.append(f"hashrate {trial.hashrate:.3g} < baseline {base.hashrate:.3g} by >{worse_margin_pct}%")
    core_t, memj_t = targets.get("core_c_max"), targets.get("memj_c_max")
    if core_t and trial.core_max is not None and trial.core_max > core_t:
        reasons.append(f"core {trial.core_max:.0f}C > target {core_t}")
    if memj_t and trial.memj_max is not None and trial.memj_max > memj_t:
        reasons.append(f"memj {trial.memj_max:.0f}C > target {memj_t}")
    if reasons:
        return Verdict("unstable", None, reasons)

    s0, s1 = score(base, goal), score(trial, goal)
    if s0 is None or s1 is None or s0 == 0:
        return Verdict("insufficient", None, ["score unavailable"])
    delta = (s1 - s0) / s0 * 100
    if goal == "target-temp" and base.core_max and trial.core_max:
        # cooler counts as improvement when efficiency did not fall
        if trial.core_max < base.core_max - 1 and delta >= -worse_margin_pct:
            return Verdict("better", delta, [f"core {trial.core_max:.0f}C vs {base.core_max:.0f}C, eff {delta:+.1f}%"])
    if delta >= min_improve_pct:
        return Verdict("better", delta, [f"{goal} {delta:+.1f}%"])
    if delta <= -worse_margin_pct:
        return Verdict("worse", delta, [f"{goal} {delta:+.1f}%"])
    return Verdict("same", delta, [f"{goal} {delta:+.1f}% (< {min_improve_pct}%)"])
