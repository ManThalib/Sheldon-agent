#!/usr/bin/env python3
"""Sheldon Phase E — Approval-Gated Pilot."""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import lp_scoring
from capital import summarize_wallet
from run_cycle import (
    _load_rotation_state,
    _save_rotation_state,
)
from planner import compare_4way, check_guardrails
from readiness import load_raw_wallet, load_and_resolve, gate_prep_swaps


PILOT_DEFAULTS = {
    "max_rotation_usd": 50.0,
    "max_rotations_pilot": 1,
    "pilot_approval_required": True,
    "george_agent_dir": "/data/.openclaw/workspace-agents/george/agents/meteora-dlmm",
    "min_hold_hours": 12,
    "min_pnl_margin_usd": 1.0,
}


def _load_pilot_config(cfg: dict) -> dict:
    pilot_cfg = cfg.get("pilot", {})
    config = dict(PILOT_DEFAULTS)
    config.update({k: v for k, v in pilot_cfg.items() if k in PILOT_DEFAULTS})
    return config


def evaluate_pilot_candidate(
    recommendation: dict,
    cfg: dict,
    rot_state: dict,
    wallet_mtime: float,
) -> dict:
    config = _load_pilot_config(cfg)
    reasons = []

    best_expected = recommendation.get("best_expected_usd_24h", 0)
    if best_expected < max(1.0, 0.005 * wallet_mtime if wallet_mtime else 0):
        reasons.append("Economic advantage insufficient")

    if best_expected <= 0:
        reasons.append("Expected PnL not positive after switching costs")

    rotations_today = len([
        n for n in rot_state.get("rotations", {}).values()
        if n.get("cycle_day") == _today_str(),
    ])
    if rotations_today >= config["max_rotations_pilot"]:
        reasons.append(f"Daily rotation cap exceeded: {rotations_today} >= {config['max_rotations_pilot']}")

    allowed = len(reasons) == 0
    return {
        "allowed": allowed,
        "reason": "; ".join(reasons) if reasons else "Pilot guardrails passed",
        "max_usd": config["max_rotation_usd"],
        "approved_by_george": False,
    }


def _today_str():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
