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
    SOL_MINT
}

# Dust rule: non-SOL, non-USDC, value above this threshold.
DUST_MIN_USD = 1.0

# SOL reserve: lamports that are never spent (rent + ATA + fees floor).
SOL_RESERVE_LAMPORTS = 20_000_000  # 0.02 SOL


# --------------------------------------------------------------------------
# Helper: check whether an asset is SOL-related
# --------------------------------------------------------------------------
def _is_sol_asset(asset: Dict[str, Any]) -> bool:
    """True when the asset is native SOL (is_native_sol flag or SOL mint)."""
    mint = (asset.get("mint") or "").strip()
    return bool(asset.get("is_native_sol") or mint == SOL_MINT)


def summarize_wallet(data: Dict[str, Any]) -> Dict[str, Any]:
    """Build a capital summary from a Missy wallet scan.

    Returns:
        {
            "wallet": <pubkey or None>,
            "total_usd": <scan total>,
            "idle_usdc": <USDC value_usd>,
            "idle_sol_raw_lamports": <raw lamports of idle SOL>,
            "idle_sol_usd": <USD value of idle SOL>,
            "dust_total_usdc": <sum of dust values>,
            "reserved_total_usdc": <value of reserved/non-dust non-USDC>,
            "deployable_usdc": idle_usdc + dust_total_usdc,       # unchanged: USD only
            "deployable_sol_raw_lamports": <excess SOL lamports after reserve>,
            "deployable_sol_usd": <excess SOL USD after reserve>,
            "deployable_total_usd": <total deployable including SOL>,
            "errors": []
        }
    """
    assets = data.get("assets") or []
    wallet = data.get("wallet")
    total_usd = float(data.get("total_usd") or 0.0)

    idle_usdc = 0.0
    idle_sol_raw_lamports = 0  # raw lamports, summed from SOL assets
    idle_sol_usd = 0.0  # USD value of idle SOL
    dust_assets: List[Dict[str, Any]] = []
    reserved_total = 0.0

    for asset in assets:
        mint = (asset.get("mint") or "").strip()
        value = float(asset.get("total_value_usd") or 0.0)
        is_native_sol = bool(asset.get("is_native_sol"))

        if mint == USDC_MINT:
            idle_usdc += value
            continue

        if _is_sol_asset(asset):
            # Idle SOL: accumulate raw lamports and USD value
            sol_raw = float(asset.get("amount_raw") or 0)
            idle_sol_raw_lamports += sol_raw
            idle_sol_usd += value
            # SOL is the reserve mint (RESERVED_MINTS = {SOL_MINT}): its USD
            # value counts as reserved and is never deployable USD. The
            # lamports-level reserve netting happens via
            # deployable_sol_raw_lamports downstream.
            reserved_total += value
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

    # Deployable SOL: net of reserve (lamports)
    deployable_sol_raw_lamports = max(0.0, idle_sol_raw_lamports - SOL_RESERVE_LAMPORTS)
    # Convert reserve lamports to USD equivalent for the deployable USD field
    sol_price = 0.0
    for a in reversed(assets):
        if _is_sol_asset(a):
            sol_price = float(a.get("price_usd") or 0)
            break
    deployable_sol_usd = idle_sol_usd - (SOL_RESERVE_LAMPORTS / 1e9) * sol_price
    deployable_sol_usd = max(0.0, deployable_sol_usd)  # cannot be negative
    deployable_total_usd = idle_usdc + dust_total + deployable_sol_usd

    # Wallet capital summary and multi-wallet tag.
    wallet_id = (data.get("wallet_id") or "main").strip() or "main"
    return {
        "wallet": wallet,
        "wallet_id": wallet_id,
        "total_usd": total_usd,
        "idle_usdc": idle_usdc,
        "idle_sol_raw_lamports": idle_sol_raw_lamports,
        "idle_sol_usd": idle_sol_usd,
        "dust_total_usdc": dust_total,
        "dust_assets": dust_assets,
        "reserved_total_usdc": reserved_total,
        "deployable_usdc": idle_usdc + dust_total,       # unchanged: USD only
        "deployable_sol_raw_lamports": deployable_sol_raw_lamports,
        "deployable_sol_usd": deployable_sol_usd,
        "deployable_total_usd": deployable_total_usd,
        "pending_release_usdc": 0.0,
        "errors": [],
    }