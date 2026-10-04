"""Configuration — defaults mirror profiles.json. A valid profiles.json next to
the parent package overrides any subset. Invalid file => fatal (fail-closed)."""


import json
from copy import deepcopy
from pathlib import Path


DEFAULT_CONFIG = {
    "pool_profiles": {
        "stable_stable": {
            "weights": {"fee_yield": 35.0, "turnover": 15.0, "depth": 15.0,
                        "depeg_safety": 25.0, "volatility_fit": 10.0},
            "vol_peak_pct": 0.5,
            "apr_cap_pct": 100.0,
        },
        "stable_bluechip": {
            "weights": {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                        "volatility_fit": 20.0, "depeg_safety": 15.0},
            "vol_peak_pct": 8.0,
            "apr_cap_pct": 300.0,
        },
        "bluechip_bluechip": {
            "weights": {"fee_yield": 30.0, "turnover": 20.0, "depth": 15.0,
                        "volatility_fit": 35.0},
            "vol_peak_pct": 10.0,
            "apr_cap_pct": 300.0,
        },
    },
    "position_profiles": {
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
    },
    "pool_thresholds": {"open": 70.0, "watch": 55.0},
    "position_thresholds": {"close": 40.0, "review": 60.0},
    "constants": {
        "depth_min_usd": 50000.0,
        "depth_max_usd": 5000000.0,
        "depeg_zero_dist": 0.005,
        "stale_grace_days": 30.0,
        "stale_zero_days": 120.0,
        "collect_min_usd": 5.0,
        "collect_pct_of_value": 0.01,
        "turnover_max": 5.0,
        "unknown_credit": 0.5,
        "il_zero_pct": 10.0,
        "il_conc_full_width_ticks": 2000,
        "il_conc_max": 4.0,
        "fee_expect_max_days": 30.0,
    },
    "dynamic": {
        "enabled": True,
        "percentile": {
            "enabled": True,
            "components": ["fee_yield", "turnover"],
            "history_scans": 60,
            "min_samples": 20,
            "blend_alpha": 0.7,
        },
        "regime": {
            "enabled": True,
            "history_scans": 30,
            "high_vol_mult": 2.0,
            "low_vol_mult": 0.5,
            "fee_boom_apr_pct": 100.0,
            "shifts": {
                "low_vol": {
                    "fee_yield": 1.15,
                    "turnover": 1.0,
                    "depth": 1.05,
                    "volatility_fit": 0.7,
                    "depeg_safety": 1.0,
                },
                "high_vol": {
                    "fee_yield": 0.8,
                    "turnover": 1.05,
                    "depth": 1.0,
                    "volatility_fit": 1.4,
                    "depeg_safety": 1.3,
                },
                "fee_boom": {
                    "fee_yield": 0.7,
                    "turnover": 1.2,
                    "depth": 1.2,
                    "volatility_fit": 1.0,
                    "depeg_safety": 1.1,
                },
                "cracked_peg": {
                    "fee_yield": 0.6,
                    "turnover": 1.0,
                    "depth": 1.0,
                    "volatility_fit": 0.9,
                    "depeg_safety": 2.0,
                },
                "neutral": {},
            },
        },
        "thresholds": {
            "enabled": True,
            "min_pools": 10,
            "open_quantile": 0.90,
            "watch_quantile": 0.70,
            "floor_open": 55.0,
            "ceil_open": 90.0,
            "floor_watch": 40.0,
            "ceil_watch": 80.0,
        },
        "position_pnl": {
            "enabled": True,
            "horizon_days": 1.0,
            "entry_cost_bps": 50.0,
            "exit_cost_bps": 50.0,
            "claim_cost_usd": 0.02,
            "min_pnl_margin_usd": 0.01,
        },
    },
}


class ConfigError(Exception):
    """Raised when profiles.json is present but invalid."""


def _validate_config(cfg: dict) -> None:
    """Fail-closed validation. Raises ConfigError on any violation."""
    for section in ("pool_profiles", "position_profiles"):
        profiles = cfg.get(section)
        if not isinstance(profiles, dict) or not profiles:
            raise ConfigError(f"{section}: missing or empty")
        for name, prof in profiles.items():
            w = prof.get("weights") if isinstance(prof, dict) else None
            if not isinstance(w, dict) or not w:
                raise ConfigError(f"{section}.{name}: weights missing")
            total = sum(float(v) for v in w.values())
            if abs(total - 100.0) > 0.01:
                raise ConfigError(
                    f"{section}.{name}: weights sum to {total}, expected 100")
            for key, val in w.items():
                if float(val) < 0:
                    raise ConfigError(f"{section}.{name}.{key}: negative weight")

    pt = cfg.get("pool_thresholds") or {}
    if not ("watch" in pt and "open" in pt and float(pt["watch"]) <= float(pt["open"])):
        raise ConfigError("pool_thresholds: need watch <= open")
    st = cfg.get("position_thresholds") or {}
    if not ("close" in st and "review" in st and float(st["close"]) <= float(st["review"])):
        raise ConfigError("position_thresholds: need close <= review")

    c = cfg.get("constants") or {}
    if float(c.get("depth_min_usd", 0)) <= 0 or \
       float(c["depth_min_usd"]) >= float(c.get("depth_max_usd", 0)):
        raise ConfigError("constants: need 0 < depth_min_usd < depth_max_usd")
    if float(c.get("stale_grace_days", 0)) >= float(c.get("stale_zero_days", 0)):
        raise ConfigError("constants: need stale_grace_days < stale_zero_days")
    for key in ("depeg_zero_dist", "il_zero_pct", "fee_expect_max_days"):
        if float(c.get(key, 0)) <= 0:
            raise ConfigError(f"constants.{key}: must be > 0")

    d = cfg.get("dynamic") or {}
    if d:
        if not isinstance(d, dict):
            raise ConfigError("dynamic: expected object")
        pctl = d.get("percentile") or {}
        if pctl:
            alpha = float(pctl.get("blend_alpha", 0.7))
            if not 0.0 <= alpha <= 1.0:
                raise ConfigError("dynamic.percentile.blend_alpha must be in [0,1]")
        thr = d.get("thresholds") or {}
        if thr:
            for q in ("open_quantile", "watch_quantile"):
                if not 0.0 <= float(thr.get(q, 0.5)) <= 1.0:
                    raise ConfigError(f"dynamic.thresholds.{q} must be in [0,1]")
            if float(thr.get("watch_quantile", 0.5)) > float(thr.get("open_quantile", 0.5)):
                raise ConfigError("dynamic.thresholds.watch_quantile must <= open_quantile")


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, val in (override or {}).items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = deepcopy(val)
    return out


def load_config(path: str) -> dict:
    """Load profiles.json over defaults. Missing file => defaults.
    Invalid JSON or failed validation => ConfigError."""
    p = Path(path)
    if not p.exists():
        return deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path}: unreadable ({exc})") from exc
    if not isinstance(user, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    cfg = _deep_merge(DEFAULT_CONFIG, user)
    _validate_config(cfg)
    return cfg


_CONFIG_PATH = None
_CONFIG = None


def init_config(path: str = None) -> dict:
    """Initialize the global config. If path is given, load from there;
    otherwise look for profiles.json beside this package."""
    global _CONFIG_PATH, _CONFIG
    if path is not None:
        _CONFIG_PATH = str(path)
    else:
        _CONFIG_PATH = str(Path(__file__).parent / "profiles.json")
    _CONFIG = load_config(_CONFIG_PATH)
    return _CONFIG


def set_config(cfg: dict) -> None:
    """Replace the active configuration (used by tests). Validates first."""
    _validate_config(cfg)
    global _CONFIG
    _CONFIG = cfg


def get_config() -> dict:
    if _CONFIG is None:
        init_config()
    return _CONFIG