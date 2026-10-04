"""Universe policy — stablecoins and high-caps only, no memes."""


STABLECOINS = {
    "USDC", "USDT", "USDG"
}

HIGH_CAPS = {
    "SOL", "WSOL",
    "CBT", "WBTC", "CBBTC",
    "ETH", "WETH",
    "XRP", "cbRXP",
    "ZEC",
    "TAO",
    "XMR",
    "BMT",
    "QNT",
    "VIRTUAL",
    "ZBCN",
    "GRASS"
}

SUPPORTED_DEXES = {"meteora", "raydium", "orca"}


def _is_stable(sym: str) -> bool:
    return sym in STABLECOINS


def _is_high_cap(sym: str) -> bool:
    return sym in HIGH_CAPS


def classify_pair(record: dict):
    """Return (pair_class, sym_x, sym_y).

    pair_class is one of: "stable_stable", "stable_bluechip",
    "bluechip_bluechip", "off_universe", "unknown".
    """
    sym_x = (record.get("token_x_symbol") or "").upper() or None
    sym_y = (record.get("token_y_symbol") or "").upper() or None
    name = record.get("name") or record.get("pool_name") or ""

    in_x = sym_x in STABLECOINS or sym_x in HIGH_CAPS if sym_x else False
    in_y = sym_y in STABLECOINS or sym_y in HIGH_CAPS if sym_y else False

    if not sym_x or not sym_y:
        # Try to extract from name
        if name:
            import re
            _PAIR_SPLIT_RE = re.compile(r"[-/]")
            base = name.split("(", 1)[0].strip()
            parts = [p.strip().upper() for p in _PAIR_SPLIT_RE.split(base) if p.strip()]
            if len(parts) >= 2:
                sym_x = parts[0]
                sym_y = parts[1]
                in_x = sym_x in STABLECOINS or sym_x in HIGH_CAPS
                in_y = sym_y in STABLECOINS or sym_y in HIGH_CAPS
        if not sym_x or not sym_y:
            return "unknown", sym_x, sym_y

    if not in_x or not in_y:
        return "off_universe", sym_x, sym_y

    x_stable = sym_x in STABLECOINS
    y_stable = sym_y in STABLECOINS
    if x_stable and y_stable:
        return "stable_stable", sym_x, sym_y
    if x_stable or y_stable:
        return "stable_bluechip", sym_x, sym_y
    return "bluechip_bluechip", sym_x, sym_y


def is_off_universe(pair_class: str) -> bool:
    """Return True if the pair class is off-universe or unknown."""
    return pair_class in ("off_universe", "unknown")