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

# Stablecoins that are spendable as quote liquidity without a swap.
# ONLY mints listed here may contribute to ``deployable_usdc``. This is a
# deliberate allow-list, not a deny-list: a volatile asset (WBTC, JUP, mSOL,
# an unknown mint) must never inflate deployable capital, because that money
# cannot be spent until it is swapped, and the swap can fail, slip, or be
# rejected by the funding gate. Dust assets already drive a separate
# dust-swap signal; counting them here as well double-counted the same USD.
#
# USDC is the settlement mint for every open. Add another stable only after
# verifying its mint address and that the executor can quote it.
STABLE_MINTS: set = {
    USDC_MINT,
}

# Dust rule: non-SOL, non-stable, value above this threshold.
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

    Capital is split by *spendability*, not by size:

    - ``idle_usdc`` / ``deployable_stable_usd`` — stablecoins from
      ``STABLE_MINTS``. This is the only capital that can fund an open
      without a swap, so it is the only capital counted as deployable.
    - ``liquidation_required_usd`` — volatile assets above ``DUST_MIN_USD``.
      Real value, but it must be sold before it can be spent; it drives a
      dust-swap signal instead of being counted as deployable.
    - ``deployable_sol_usd`` — SOL surplus above the rent/fee reserve.

    Returns:
        {
            "wallet": <pubkey or None>,
            "total_usd": <scan total>,
            "idle_usdc": <USDC-mint value>,
            "idle_stable_usd": <all stablecoin value>,
            "idle_sol_raw_lamports": <raw lamports of idle SOL>,
            "idle_sol_usd": <USD value of idle SOL>,
            "stable_assets": [...],
            "dust_assets": <volatile assets above the dust floor>,
            "dust_total_usdc": <volatile total; NOT spendable>,
            "liquidation_required_usd": <same as dust_total_usdc>,
            "reserved_total_usdc": <value of reserved/non-dust non-stable>,
            "deployable_usdc": idle_stable_usd,   # stablecoins only
            "deployable_stable_usd": idle_stable_usd,
            "deployable_sol_raw_lamports": <excess SOL lamports after reserve>,
            "deployable_sol_usd": <excess SOL USD after reserve>,
            "deployable_total_usd": <stable + deployable SOL, no dust>,
            "errors": []
        }
    """
    assets = data.get("assets") or []
    wallet = data.get("wallet")
    total_usd = float(data.get("total_usd") or 0.0)

    idle_usdc = 0.0
    idle_stable_usd = 0.0
    idle_sol_raw_lamports = 0  # raw lamports, summed from SOL assets
    idle_sol_usd = 0.0  # USD value of idle SOL
    stable_assets: List[Dict[str, Any]] = []
    dust_assets: List[Dict[str, Any]] = []
    reserved_total = 0.0

    for asset in assets:
        mint = (asset.get("mint") or "").strip()
        value = float(asset.get("total_value_usd") or 0.0)
        is_native_sol = bool(asset.get("is_native_sol"))

        if mint in STABLE_MINTS:
            idle_stable_usd += value
            if mint == USDC_MINT:
                idle_usdc += value
            stable_assets.append({
                "mint": mint,
                "symbol": asset.get("symbol") or "",
                "value_usd": value,
            })
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
    # Deployable USD excludes volatile dust by construction: only stablecoins
    # and SOL surplus are spendable without a swap.
    deployable_total_usd = idle_stable_usd + deployable_sol_usd

    # Wallet capital summary and multi-wallet tag.
    wallet_id = (data.get("wallet_id") or "main").strip() or "main"
    return {
        "wallet": wallet,
        "wallet_id": wallet_id,
        "total_usd": total_usd,
        "idle_usdc": idle_usdc,
        "idle_stable_usd": idle_stable_usd,
        "idle_sol_raw_lamports": idle_sol_raw_lamports,
        "idle_sol_usd": idle_sol_usd,
        "stable_assets": stable_assets,
        "dust_total_usdc": dust_total,
        "dust_assets": dust_assets,
        "liquidation_required_usd": dust_total,
        "reserved_total_usdc": reserved_total,
        "deployable_usdc": idle_stable_usd,   # stablecoins only
        "deployable_stable_usd": idle_stable_usd,
        "deployable_sol_raw_lamports": deployable_sol_raw_lamports,
        "deployable_sol_usd": deployable_sol_usd,
        "deployable_total_usd": deployable_total_usd,
        "pending_release_usdc": 0.0,
        "errors": [],
    }