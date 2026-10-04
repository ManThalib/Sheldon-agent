def _signal_usdc_committed(sig: dict) -> float:
    """USDC amount a queued Sheldon signal will consume, 0 if unknown."""
    from capital import USDC_MINT
    action = sig.get("action")
    if action == "open":
        return float(sig.get("position_usd") or 0.0)
    if action == "add_liquidity":
        return float(sig.get("position_usd") or 0.0)
    if action == "swap":
        if sig.get("input_mint") != USDC_MINT:
            return 0.0
        try:
            return int(sig.get("amount") or 0) / 10**6
        except (TypeError, ValueError):
            return 0.0
    return 0.0