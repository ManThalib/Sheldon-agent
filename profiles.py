"""Scoring profiles and tunable constants for the Sheldon LP engine.

This module intentionally exports constants only.  Profiles, thresholds and
universe lists are hard-coded and frozen; no dynamic or adaptive logic lives
here.
"""

from typing import Any, Dict

# --------------------------------------------------------------------------
# Universe policy — stablecoins and high-caps only, no memes.
# Anything not in these sets is gated out before scoring.
# --------------------------------------------------------------------------
STABLECOINS = {
    "USDC", "USDT", "USDS", "PYUSD", "DAI", "FDUSD", "EURC", "USDH", "USX",
}

HIGH_CAPS = {
    "SOL", "WSOL", "WBTC", "CBBTC", "WETH", "ETH",
    "JITOSOL", "JSOL", "MSOL", "BSOL", "JUP", "ZEC",
}

# --------------------------------------------------------------------------
# Pool LP Opportunity profiles.  Component weights sum to 100 per profile.
# --------------------------------------------------------------------------
POOL_PROFILES: Dict[str, Dict[str, Any]] = {
    "stable_stable": {
        "weights": {"fee_yield": 35.0, "turnover": 15.0, "depth": 15.0,
                    "depeg_safety": 25.0, "volatility_fit": 10.0},
        "vol_peak_pct": 0.5,     # stable pairs should barely move
        "apr_cap_pct": 100.0,    # stable APRs are low; 100%+ is exceptional
    },
    "stable_bluechip": {
        "weights": {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                    "volatility_fit": 20.0, "depeg_safety": 15.0},
        "vol_peak_pct": 8.0,
        "apr_cap_pct": 300.0,
    },
    # bluechip/bluechip behaves like stable/bluechip minus the depeg leg;
    # its weight is folded into volatility_fit.
    "bluechip_bluechip": {
        "weights": {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                    "volatility_fit": 35.0},
        "vol_peak_pct": 10.0,
        "apr_cap_pct": 300.0,
    },
}

# --------------------------------------------------------------------------
# Position Health profiles.  Component weights sum to 100 per profile.
# --------------------------------------------------------------------------
POSITION_PROFILES: Dict[str, Dict[str, Any]] = {
    "stable_stable": {
        "weights": {"range_status": 35.0, "fee_capture": 25.0,
                    "depeg_exposure": 25.0, "staleness": 15.0},
    },
    "stable_bluechip": {
        "weights": {"range_status": 30.0, "fee_capture": 20.0,
                    "il_risk": 30.0, "staleness": 20.0},
    },
    "bluechip_bluechip": {
        "weights": {"range_status": 30.0, "fee_capture": 20.0,
                    "il_risk": 30.0, "staleness": 20.0},
    },
}

# --------------------------------------------------------------------------
# Verdict thresholds.
# --------------------------------------------------------------------------
POOL_THRESHOLDS = {"open": 70.0, "watch": 55.0}
POSITION_THRESHOLDS = {"close": 40.0, "review": 60.0}

# --------------------------------------------------------------------------
# Scoring constants.
# --------------------------------------------------------------------------
# Depth mapping: TVL USD range for log-scale scoring.
DEPTH_MIN_USD = 50_000.0
DEPTH_MAX_USD = 5_000_000.0

# A stable is considered depegged (zero credit) at this distance from $1.
DEPEG_ZERO_DIST = 0.005  # 0.5%

# Position staleness: healthy long-lived positions are NOT decayed; the
# component only erodes confidence that entry assumptions still hold.
STALE_GRACE_DAYS = 30.0
STALE_ZERO_DAYS = 120.0

# Fee collection: worth claiming when fees exceed this share of position
# value (plus a small absolute floor for tiny/unknown positions).
COLLECT_MIN_USD = 5.0
COLLECT_PCT_OF_VALUE = 0.01  # 1%

# --------------------------------------------------------------------------
# Missing-data defaults for position components.
#
# These are conservative "fail-suspicious" values.  They keep a position with
# missing data from instantly collapsing to a CLOSE, while still penalising it
# enough that repeated missing data should be investigated.
# --------------------------------------------------------------------------
MISSING_DEFAULT_RANGE_STATUS = 0.50   # bounds unknown: could be in or out
MISSING_DEFAULT_FEE_CAPTURE = 0.30    # fee data missing: mild penalty
MISSING_DEFAULT_IL_RISK = 0.40        # IL unknown: moderate penalty
MISSING_DEFAULT_STALENESS = 0.50      # age unknown: neutral
MISSING_DEFAULT_DEPEG_EXPOSURE = 0.50 # peg status unknown: half credit

# --------------------------------------------------------------------------
# Sentinel values that indicate Orca raw fee data is unreliable.
# --------------------------------------------------------------------------
U64_MAX = 18446744073709551615
U32_MAX = 4294967295
FEE_SENTINELS = (U64_MAX, U64_MAX - 1, U32_MAX, U32_MAX - 1)


# --------------------------------------------------------------------------
# Profile sanity checks (import-time).
# --------------------------------------------------------------------------
def _check_profile_weights() -> None:
    for name, profile in POOL_PROFILES.items():
        total = sum(profile["weights"].values())
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"Pool profile '{name}' weights sum to {total}, not 100.0")
    for name, profile in POSITION_PROFILES.items():
        total = sum(profile["weights"].values())
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"Position profile '{name}' weights sum to {total}, not 100.0")


_check_profile_weights()
