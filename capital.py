"""Wallet scan loader and capital summary for Sheldon.

Reads Missy's wallet_screener output and turns it into the idle USDC and
dust-token amounts Sheldon needs for position sizing and signal generation.
"""

from typing import Any, Dict, List

# --------------------------------------------------------------------------
# Token mints
# --------------------------------------------------------------------------
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Tokens that should never be treated as dust (owner-editable list).
# Default includes native SOL (gas) and any owner-designated long-term holds.
RESERVED_MINTS: set = {
    SOL_MINT,
    "3ArcxqLtXMmBnWbbtfwQgVL3MNnDsggzgGDtXMnjpump",
}

# Dust rule: non-SOL, non-USDC, value above this threshold.
DUST_MIN_USD = 1.0


def summarize_wallet(data: Dict[str, Any]) -> Dict[str, Any]:
    """Build a capital summary from a Missy wallet scan.

    Returns:
        {
            "wallet": <pubkey or None>,
            "total_usd": <scan total>,
            "idle_usdc": <USDC value_usd>,
            "dust_total_usdc": <sum of dust values>,
            "dust_assets": [...],
            "reserved_total_usdc": <value of reserved/non-dust non-USDC>,
            "deployable_usdc": idle_usdc + dust_total_usdc,
            "errors": []
        }
    """
    assets = data.get("assets") or []
    wallet = data.get("wallet")
    total_usd = float(data.get("total_usd") or 0.0)

    idle_usdc = 0.0
    dust_assets: List[Dict[str, Any]] = []
    reserved_total = 0.0

    for asset in assets:
        mint = (asset.get("mint") or "").strip()
        value = float(asset.get("total_value_usd") or 0.0)
        is_native_sol = bool(asset.get("is_native_sol"))

        if mint == USDC_MINT:
            idle_usdc += value
            continue

        if is_native_sol or mint in RESERVED_MINTS:
            reserved_total += value
            continue

        if value > DUST_MIN_USD:
            dust_assets.append({
                "mint": mint,
                "symbol": asset.get("symbol") or "",
                "decimals": int(asset.get("decimals") or 0),
                "amount_raw": str(asset.get("amount_raw") or "0"),
                "amount_ui": float(asset.get("amount_ui") or 0.0),
                "price_usd": float(asset.get("price_usd") or 0.0),
                "value_usd": value,
            })

    dust_total = sum(a["value_usd"] for a in dust_assets)

    # Wallet capital summary and multi-wallet tag.
    wallet_id = (data.get("wallet_id") or "main").strip() or "main"
    return {
        "wallet": wallet,
        "wallet_id": wallet_id,
        "total_usd": total_usd,
        "idle_usdc": idle_usdc,
        "dust_total_usdc": dust_total,
        "dust_assets": dust_assets,
        "reserved_total_usdc": reserved_total,
        "deployable_usdc": idle_usdc + dust_total,
        "errors": [],
    }
