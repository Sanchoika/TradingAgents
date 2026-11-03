"""Brent alerts settings, read from an optional TOML file (separate from demo.toml)."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Bump when a rule below changes meaning. Every signal records it with the config hash,
# so results from different rule sets are never pooled silently.
RULES_VERSION = "brent-v1"

# Fields that define the signal and paper-fill rules. Changing any of them changes the
# config hash written on each signal.
RULE_FIELDS = (
    "instrument", "range_low", "range_high", "validity_seconds", "min_usable_seconds",
    "max_quote_age_seconds", "max_spread", "break_atr", "zone_atr", "stop_atr",
    "fail_stop_atr", "atr_period", "arm_hours", "max_hold_hours", "cooldown_minutes",
    "min_r_spreads", "human_latency_seconds", "financing_pct_year", "fx_loss_pct",
    "blackouts", "close_buffer_minutes",
)


@dataclass
class BrentConfig:
    instrument: str = "BCO_USD"
    # Operating range for NEW entry suggestions (USD). Never changed by the code.
    range_low: float = 100.0
    range_high: float = 120.0

    # Entry validity: the clock starts at the quote time of the final check.
    validity_seconds: int = 60
    min_usable_seconds: int = 30     # suppress if less than this is left after expected delivery
    max_quote_age_seconds: float = 10.0
    max_spread: float = 0.08         # USD; 2026 median ~0.04, p90 0.13 around 17:00 ET

    # Setup rules (fractions of daily ATR). Chosen before any test; not tuned.
    break_atr: float = 0.10          # a 15-min close this far beyond yesterday's high/low is a break
    zone_atr: float = 0.15           # entry zone width beside the level
    stop_atr: float = 0.25           # breakout stop: this far back through the level
    fail_stop_atr: float = 0.05      # failed-break stop: this far beyond the trigger bar's extreme
    atr_period: int = 14
    arm_hours: float = 4.0           # an armed setup that has not alerted by then is dropped
    max_hold_hours: float = 8.0      # holding period (paper exit), separate from validity
    cooldown_minutes: int = 15       # no new actionable alert this soon after the last one
    min_r_spreads: float = 4.0       # stop distance must be at least this many spreads
    close_buffer_minutes: int = 15   # no entries this close to the daily or weekly close

    # Paper fills (frozen with the rules version).
    human_latency_seconds: int = 20  # earliest hypothetical fill after the alert is delivered
    financing_pct_year: float = 45.0  # charged as a cost on either side (prereg-21 step 2)
    fx_loss_pct: float = 1.0          # CAD account: winners x (1 - this), losers x (1 + this)

    # Event blackouts, New York time: "weekday HH:MM-HH:MM" (Mon=0) or "YYYY-MM-DD HH:MM-HH:MM".
    blackouts: list[str] = field(default_factory=lambda: ["2 10:15-10:45"])  # EIA WPSR Wed 10:30

    # Sizing is shown only when a risk % is approved for this module; otherwise omitted.
    risk_pct: float | None = None

    # News checks (phase 1: logged only) and the bounded model call
    llm_provider: str = "chatgpt"    # existing repo integration (ChatGPT plan sign-in)
    llm_model: str = "gpt-6-luna"
    llm_per_hour: int = 6            # ceilings, not targets
    llm_per_day: int = 40
    llm_quiet_et: list[str] = field(default_factory=lambda: ["07:30", "10:30"])  # morning bot
    news_poll_seconds: int = 180

    # Operations
    poll_seconds: float = 3.0
    notify_command: str = ""         # e.g. "hermes send --to telegram --quiet"
    send_expiry_notice: bool = True
    news_symbols: list[str] = field(default_factory=lambda: ["BZ=F", "CL=F"])
    journal_dir: str = str(Path.home() / ".tradingagents" / "brent")
    site_dir: str = ""               # optional folder that also gets index.html (status page)
    notes_dir: str = ""              # optional Obsidian folder for a daily Brent note

    def __post_init__(self):
        self.journal_dir = str(Path(self.journal_dir).expanduser())
        if not 0 < self.range_low < self.range_high:
            raise ValueError("range_low must be below range_high")
        if self.min_usable_seconds >= self.validity_seconds:
            raise ValueError("min_usable_seconds must be below validity_seconds")
        if self.risk_pct is not None and not 0 < self.risk_pct <= 1:
            raise ValueError("risk_pct must be in (0, 1]; keep risk small")
        for b in self.blackouts:
            parse_blackout(b)

    def config_hash(self) -> str:
        rules = {k: asdict(self)[k] for k in RULE_FIELDS}
        return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:12]


def parse_blackout(text: str) -> tuple[str, str, str]:
    """'2 10:15-10:45' or '2026-11-26 12:00-12:45' -> (day, start, end)."""
    try:
        day, span = text.split()
        start, end = span.split("-")
        for t in (start, end):
            h, m = t.split(":")
            assert 0 <= int(h) < 24 and 0 <= int(m) < 60
        if len(day) == 1:
            assert 0 <= int(day) <= 6
        else:
            assert len(day) == 10
    except (ValueError, AssertionError) as exc:
        raise ValueError(f"bad blackout {text!r}; use '2 10:15-10:45' or 'YYYY-MM-DD HH:MM-HH:MM'") from exc
    return day, start, end


def load_config(path: str | Path | None) -> BrentConfig:
    if path is None:
        return BrentConfig()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    unknown = sorted(set(data) - set(BrentConfig.__dataclass_fields__))
    if unknown:
        raise ValueError(f"unknown settings in {path}: {', '.join(unknown)}")
    return BrentConfig(**data)
