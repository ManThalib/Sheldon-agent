#!/usr/bin/env python3
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import lp_scoring
from capital import summarize_wallet
from run_cycle import _load_rotation_state, _save_rotation_state

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

def evaluate_pilot_candidate(recommendation: dict, cfg: dict, rot_state: dict, wallet_mtime: float) -> dict:
    config = _load_pilot_config(cfg)
    reasons = []
    best_expected = recommendation.get("best_expected_usd_24h", 0)
    if best_expected < max(1.0, 0.005 * wallet_mtime if wallet_mtime else 0):
        reasons.append("Economic advantage insufficient")
    if best_expected <= 0:
        reasons.append("Expected PnL not positive after switching costs")
    rotations_today = len([n for n in rot_state.get("rotations", {}).values() if n.get("cycle_day") == _today_str()])
    if rotations_today >= config["max_rotations_pilot"]:
        reasons.append(f"Daily rotation cap exceeded: {rotations_today} >= {config['max_rotations_pilot']}")
    allowed = len(reasons) == 0
    return {"allowed": allowed, "reason": "; ".join(reasons) if reasons else "Pilot guardrails passed", "max_usd": config["max_rotation_usd"], "approved_by_george": False}

def _today_str():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--recommendation", default=None)
    ap.add_argument("--cfg", default=None)
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--signals-dir", default="/data/.openclaw/workspace-agents/george/agents/meteora-dlmm/signals/pending")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--max-usd", type=float, default=50.0)
    args = ap.parse_args()
    cfg = lp_scoring.get_config() if args.cfg is None else lp_scoring.load_config(args.cfg)
    config = _load_pilot_config(cfg)
    rot_state = _load_rotation_state(args.state_dir)
    wallet = summarize_wallet({"wallet": None, "total_usd": 0.0, "idle_usdc": 0.0, "dust_total_usdc": 0.0, "dust_assets": [], "reserved_total_usdc": 0.0, "deployable_usdc": 0.0})
    wallet_mtime = 0.0
    if args.recommendation and os.path.exists(args.recommendation):
        with open(args.recommendation) as f:
            report = json.load(f)
        recs = report.get("recommendations", [])
        if not recs:
            print("No recommendations found.")
            return 0
        recommendation = recs[0]
    else:
        print("Error: --recommendation required.")
        return 1
    if args.approve:
        # Simplified activation - just evaluate
        evaluation = evaluate_pilot_candidate(recommendation, cfg, rot_state, wallet_mtime)
        print(f"Evaluation: allowed={evaluation['allowed']}, reason={evaluation['reason']}")
        print(f"Max USD: ${evaluation['max_usd']}, George approval required: {config['pilot_approval_required']}")
    else:
        evaluation = evaluate_pilot_candidate(recommendation, cfg, rot_state, wallet_mtime)
        print(f"Evaluation (no approve): allowed={evaluation['allowed']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
