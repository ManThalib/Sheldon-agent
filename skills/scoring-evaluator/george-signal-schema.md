# George Signal Schema Notes (for Sheldon)

George (the executor) reads signals from `agents/meteora-dlmm/signals/pending/`
in his workspace (`/data/.openclaw/workspace-agents/george/`). Each signal is
one JSON file matching this schema.

Supported DEX values: `meteora`, `raydium`, `orca`.

```json
{
  "signal_id": "unique-id",
  "action": "open | close | claim_fees | claim_rewards",
  "dex": "meteora | raydium | orca",
  "pool_address": "So1ana...addr",
  "position_id": "optional-for-close",
  "side": "bidirectional | spot_one | spot_bidirectional",
  "bin_range": { "lower": 100, "upper": 130 },
  "liquidity": { "amount_x": "1000000", "amount_y": "500000" },
  "max_slippage_bps": 100,
  "reason": "one-line from analyst",
  "created_at": "ISO-8601"
}
```

Rules when writing signals for George:
- Schema-exact. George rejects malformed signals rather than fixing them.
- `signal_id`: `sheldon-<UTC timestamp>-<n>`.
- `dex`: required. Must be one of `meteora`, `raydium`, or `orca`.
- `bin_range` units depend on DEX:
  - `meteora`: DLMM bin IDs (integers).
  - `raydium` / `orca`: CLMM ticks (integers).
- `liquidity.amount_x` / `amount_y`: raw token amounts in smallest token units
  (e.g., lamports for SPL, considering decimals).
- `created_at`: ISO-8601 UTC. George rejects signals older than
  `signal_max_age_seconds` (300s in his config) — conclusions must be written
  promptly after each scan.
- `max_slippage_bps`: never looser than the rail value (100).
- One file per action. HOLD / no-action cycles: write no signal file, only log.
- `position_id` is required for close actions.
