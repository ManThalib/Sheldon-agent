"""Verdict and threshold policy shared between pool and position scoring.

Contains:
- pool_verdict: map a pool score to a verdict (OPEN_CANDIDATE / WATCH / IGNORE)
- _position_policy_close: CLOSE verdict for off-universe/unknown pairs
- _score_position_core: shared position scoring logic used by both
  score_position (local) and score_position_missy (Missy features).
"""


def pool_verdict(score: float, thresholds: dict) -> str:
    """Map a pool score to a verdict given thresholds."""
    if score >= thresholds["open"]:
        return "OPEN_CANDIDATE"
    if score >= thresholds["watch"]:
        return "WATCH"
    return "IGNORE"


def _position_policy_close(record: dict, pool_name, pair_class, sym_x, sym_y) -> dict:
    """Return a CLOSE verdict record for off-universe / unknown pairs."""
    return {"position_id": record.get("position_id") or record.get("position_address"),
            "pool": pool_name,
            "pool_address": record.get("pool_address"),
            "pair_class": pair_class, "pair": [sym_x, sym_y],
            "score": 0.0, "components": {}, "verdict": "CLOSE",
            "collect_fees": False,
            "data_quality": {"unknown_components": [], "policy_close": True},
            "reason": "off-universe pair: policy is stables/high-caps only",
            "note": "off-universe pair: policy is stables/high-caps only"}