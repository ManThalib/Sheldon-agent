# Pool_features v1 comparison — Missy fixture 2026-10-29

Fixture: `/Users/manthalib/Missy-agent/pool_screener/tests/fixtures/pool_scan_2026-10-29.json`
(67 v1 records, frozen clock, captured **without** RPC tick enrichment).
Ranker config: `pool_ranker` defaults with `enabled: true`; gate:
`pool_gate` defaults with `enabled: true`, `require_range_ready: true`.

> **Fixture caveat:** most records have `range_ready: false`, so the full v1
> open flow keeps only the 6 pools with a live tick/bin. That is the fixture,
> not a regression. Gate counts are reported at both levels below so the two
> effects (safety vs deployability) stay separable.

| Set | Count |
|---|---|
| v1 records | 67 |
| legacy score gate (`score >= 90`, eligible) | 11 |
| Missy `safety.eligible` | 26 |
| safety + `range_ready` (v1 gate) | 6 |
| safety eligible but `range_ready=false` | 20 |
| Missy safety rejects | 41 |

### Kept after the v1 gate (6)

| pool_address | name | dex | score | net_daily_pct |
|---|---|---|---|---|
| `2TAgfogn8JRwwTnsGKUa6WUT5Xdv1iMEzfEDMor68X9C` | ZEC-SOL | orca | 97.51 | 0.2344 |
| `Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE` | SOL-USDC | orca | 96.91 | 0.0729 |
| `GTHKH8s82ZR8GTSFZ1dUu6wfdxhy59wpMShxzG5zjiPm` | ZEC-USDC | orca | 96.06 | 0.1798 |
| `HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM` | BTC-USDC | orca | 96.00 | 0.0598 |
| `HktfL7iwGKT5QHjywQkcDnZXScoh811k7akrMZJkCcEF` | SOL-WHETH | orca | 91.40 | 0.0641 |
| `CeaZcxBNLpJWtxzt58qQmfMBtJY8pQLvursXTJYGQpbN` | SOL-BTC | orca | 84.73 | 0.0973 |

### Safety-eligible but rejected by `range_ready` (20)

| pool_address | name | dex | score | reject reason |
|---|---|---|---|---|
| `5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6` | SOL-USDC | meteora | 95.84 | no live tick/bin (`range_ready=false`) |
| `BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y` | SOL-USDC | meteora | 95.66 | no live tick/bin (`range_ready=false`) |
| `HDhWhQCBrSh9xNWmNtsTi86eWj3yCoEiaRodjgNydo1b` | BTC-SOL | meteora | 94.01 | no live tick/bin (`range_ready=false`) |
| `HTvjzsfX3yU6BUodCjZ5vZkUrAxMDTrBs3CJaq43ashR` | SOL-USDC | meteora | 93.14 | no live tick/bin (`range_ready=false`) |
| `7ubS3GccjhQY99AYNKXjNJqnXjaokEdfdV915xnCb96r` | BTC-USDC | meteora | 92.88 | no live tick/bin (`range_ready=false`) |
| `3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv` | SOL-USDC | raydium | 90.47 | no live tick/bin (`range_ready=false`) |
| `CgHuN3ajFijPidxiWStiDBUBkchWGNb8tVTGaNgYx9UJ` | XMR-SOL | raydium | 88.84 | no live tick/bin (`range_ready=false`) |
| `4nFbdT7DeXATvaRZfR3WqALGJnogMjqe9vf2H6C1WXBr` | BTC-USDC | raydium | 87.54 | no live tick/bin (`range_ready=false`) |
| `3nMFwZXwY1s1M5s8vYAHqd4wGs4iSxXE4LRoUMMYqEgF` | SOL-USDT | raydium | 87.43 | no live tick/bin (`range_ready=false`) |
| `BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh` | SOL-USDC | meteora | 83.93 | no live tick/bin (`range_ready=false`) |
| `HCfytQ49w6Dn9UhHCqjNYTZYQ6z5SwqmsyYYqW4EKDdA` | SOL-BTC | raydium | 82.29 | no live tick/bin (`range_ready=false`) |
| `6eCdgpBCAf5bruspnGMFzCXQ1LcKMwb7BiB3Qy5hLpMi` | TAO-USDC | raydium | 78.89 | no live tick/bin (`range_ready=false`) |
| `4RZ4uFqpPrncn3sBik5y4KNMwCRgKRek4F6yB7WKfZB6` | ZEC-USDC | raydium | 78.55 | no live tick/bin (`range_ready=false`) |
| `GLfEiK21UZus4G5BeyUa9PMZZac1DzbyBx99K1d6enYy` | BMT-USDC | raydium | 69.99 | no live tick/bin (`range_ready=false`) |
| `7FDkk29Kdyz2bqSegov3M6nRHRwUmynQhdMqSX8vZ632` | TAO-USDC | raydium | 68.12 | no live tick/bin (`range_ready=false`) |
| `7XawhbbxtsRcQA8KTkHT9f9nc6d69UwqCDh6U5EEbEmX` | SOL-USDT | raydium | 67.61 | no live tick/bin (`range_ready=false`) |
| `GPxw5YstKrcJDe9GtD3u9myrsKAyLNy8tedsoyVDyN2b` | ZEC-SOL | raydium | 67.24 | no live tick/bin (`range_ready=false`) |
| `CTpkCQufppiECZJesCkvy1nETmVQQMFK41EoFjw1x4WG` | BMT-SOL | raydium | 61.66 | no live tick/bin (`range_ready=false`) |
| `58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2` | SOL-USDC | raydium | 57.38 | no live tick/bin (`range_ready=false`) |
| `H7FjuWx44xCCw1FBWm4s42qjUtu7Q78yRxEoGY4Ssdjg` | ZEC-SOL | raydium | 50.26 | no live tick/bin (`range_ready=false`) |

### Missy safety rejects (41), with reject codes

| pool_address | name | dex | score | reject_codes |
|---|---|---|---|---|
| `BZtgQEyS6eXUXicYPHecYQ7PybqodXQMvkjUbP4R8mUU` | USDC-USDT | raydium | 83.23 | low_fee_tvl |
| `ExwyJ1D3F4RwaRvpGhmendPFK7Jfiqjh7a1TcQzt4Sq` | USDG-USDT | raydium | 75.00 | low_tvl, low_fee_tvl |
| `7ktmvkDBNwp7vnM6pjtHCemGykDjgH1G4XG6uf83HDqT` | USDG-USDC | raydium | 73.27 | low_tvl |
| `5KqohoeGjTjyHAFJJywK4J7fkFuK82PfMyuseGgLKZu2` | SOL-USDG | orca | 68.89 | low_fee_tvl |
| `FkggXKXmxaQ8tT46NnpcTW3tWjtSerJ7YdGuqGF9FbjX` | ZBCN-SOL | raydium | 59.87 | low_tvl, low_volume |
| `9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps` | USDG-USDC | orca | 59.26 | low_fee_tvl |
| `8GuXrPq6deS1ZzNXYmBjVZ5P48cAmKGrhhpdAn2FURPC` | BTC-BTC | raydium | 58.34 | low_fee_tvl |
| `CqXeBvzvB4BKxLxwoHNdnLV58PfL8W7iVN8kaezkCrJX` | SOL-BTC | raydium | 52.81 | low_volume |
| `4yrHms7ekgTBgJg77zJ33TsWrraqHsCXDtuSZqUsuGHb` | ETH-SOL | raydium | 50.24 | low_fee_tvl |
| `2EXiumdi14E9b8Fy62QcA5Uh6WdHS2b38wtSxp72Mibj` | USDT-USDC | raydium | 49.63 | low_fee_tvl |
| `F7dYi7W2oDnt5Dtg3E66fREubb65QEwgB5A1cBoek8pY` | ZBCN-USDC | raydium | 49.46 | low_tvl, low_volume |
| `6Xg1WVhrNbujNyBF2KFWCj797vVgxM7x7nFNHx6Fygoz` | ZBCN-SOL | raydium | 49.28 | low_tvl, low_volume |
| `FrMLgdeZrFp6mG4DZsvcVDpbyFMaBcsWRQ9WNk7pbM5e` | ZEC-USDC | raydium | 47.66 | low_volume, low_fee_tvl |
| `3GsB3iPBGkbPedYJmAkV6E8ywZeuMBzBEqwKsYbYZhtG` | GRASS-SOL | raydium | 46.36 | low_tvl, low_volume |
| `Ay22yvM12WkPfgotGRZu7uiUx4KKYwxouTfbCCsgS74c` | VIRTUAL-SOL | raydium | 46.24 | low_tvl, low_volume, low_fee_tvl |
| `EoNrn8iUhwgJySD1pHu8Qxm5gSQqLK3za4m8xzD2RuEb` | ETH-USDC | raydium | 46.00 | low_tvl, low_volume, low_fee_tvl |
| `F9vVRg1uMyh2a3gHcpfuCGcTePRsNi7H7cvKgB8D48gN` | XRP-USDC | raydium | 44.06 | missing_data, low_volume, low_fee_tvl |
| `GTJ2S27UL7yZ3TdTwpKjfNcxeEZRkRPHjpj5Fubwb8Mk` | GRASS-SOL | raydium | 43.14 | low_fee_tvl |
| `F589q6Ai6H9fjjq3sbjg57crbZbWu4aeP3H1jLkvvWCE` | GRASS-SOL | raydium | 42.26 | low_tvl, low_volume |
| `CMmDofGZUWZsVaG6VjUjzPFYoixoWXUMebD8Eb7fLzts` | GRASS-SOL | raydium | 39.58 | low_tvl, low_volume |
| `EK5ixvTiX23cQTq4YxDuEwZc8eJBDV4NdjTsPM4JGJRV` | BMT-USDC | raydium | 38.65 | low_tvl, low_volume |
| `2jYN1tbhkfPG12to4q6a64hFgtyf1e1FddZjVgZPXuNg` | BTC-USDC | raydium | 33.98 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `AZdpLL7apvK7XGvrmZLJUZ4btnQsGMfZ8KVvzqNJgzFE` | BTC-USDT | raydium | 29.99 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `EjQrJS9CGpQh5orXa2JMohge4LtYEj7gTuhPDTom9KQa` | GRASS-USDC | raydium | 27.98 | low_tvl, low_volume, low_fee_tvl |
| `CgLEotKN2ZNXQN6Yb37Dz5NWkiJ52MXmtT3DYmiQuZE5` | GRASS-USDC | raydium | 25.58 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `66eymN8tq28pxKtwwh4auARMbn6bFXuswzMoWv4bTLDE` | GRASS-USDT | raydium | 24.01 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `6Au1u8wdfZAezq8CDoWTBkvL3GwYiFLb5A9YnNuQDPfc` | VIRTUAL-SOL | raydium | 21.95 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `C15nfM2rfoa2G2ELyehVEoE9uhjLHbc3nGVKPLNq6g7V` | GRASS-USDT | raydium | 21.74 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `DTeuynxYgdKLf8HboZEhdz2Aw81fNvyv3TooeHu5heMQ` | VIRTUAL-USDC | raydium | 20.75 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `EokF8giSeu7Zc1o4CZvzK2kiwZxvWT3Zdbe47tPXwcpi` | VIRTUAL-SOL | raydium | 20.45 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `6syTFYwxpbcxLf5iWcwQWrEzFoCvpFtjYEPrgt26w5sG` | BMT-USDC | raydium | 19.38 | missing_data, low_volume, low_fee_tvl |
| `DsoXvAkP2RByYhHCtKyDeR3ybgFtxS2w4vgLqHdSc2fi` | VIRTUAL-SOL | raydium | 19.03 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `92KA37drEUhU5uEkziCSs6saTqXkFudDX5V16YDaGVph` | BMT-SOL | raydium | 18.89 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `EWCxrKbBBVyEJmNw6JSvdKZFfvr9e29sEwsP9WFfU4HS` | ZBCN-SOL | raydium | 18.75 | missing_data, low_volume, low_fee_tvl |
| `BnV9TtojHXkvdzsMRHzA1t7WangSChrRXr4JYf22PLyX` | VIRTUAL-SOL | raydium | 18.49 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `Bsi6sDnairDHexd29CdyyTNiYZLRP39gbJcQEQH2xgGa` | VIRTUAL-SOL | raydium | 18.39 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `6n2gF8FegMme939GzLE3Xuk5hMJaMiuA7PgviQgMYsED` | VIRTUAL-SOL | raydium | 17.38 | missing_data, low_volume, low_fee_tvl |
| `ETSmVNi8qREATZQqMQTGbMDM6QiysEtV5jttf1NRPc4R` | BMT-USDC | raydium | 17.14 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `5655KAdYnTt7yvsGLgDBxeHUYggcZ4daxBo2483MASDm` | BMT-USDC | raydium | 17.01 | missing_data, low_tvl, low_volume, low_fee_tvl |
| `8vd3eqqWD75Piiq2VFD13tnuLXfo63wNHwy7ANbattMB` | ZBCN-SOL | raydium | 16.88 | missing_data, low_volume, low_fee_tvl |
| `9zdmCujMXjERXBsZ4z7B7a73FoBsXqpC4ZD3Y6Fc75Zm` | VIRTUAL-SOL | raydium | 16.88 | missing_data, low_volume, low_fee_tvl |

### Top-10 by old `score` (gross ordinal)

| # | pool_address | name | dex | score | net_daily_pct | fee APR | in_range | vol | ref width |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `2TAgfogn8JRwwTnsGKUa6WUT5Xdv1iMEzfEDMor68X9C` | ZEC-SOL | orca | 97.51 | 0.2344 | 103.98 | 0.830866 | 1.3389 | 1.0050 |
| 2 | `Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE` | SOL-USDC | orca | 96.91 | 0.0729 | 59.70 | 0.504675 | 2.7797 | 1.3996 |
| 3 | `GTHKH8s82ZR8GTSFZ1dUu6wfdxhy59wpMShxzG5zjiPm` | ZEC-USDC | orca | 96.06 | 0.1798 | 144.11 | 0.498302 | 3.6647 | 1.8366 |
| 4 | `HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM` | BTC-USDC | orca | 96.00 | 0.0598 | 39.15 | 0.596724 | 1.8091 | 1.0050 |
| 5 | `5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6` | SOL-USDC | meteora | 95.84 | 0.0002 | 91.37 | 0.101954 | 4.4922 | 1.3690 |
| 6 | `BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y` | SOL-USDC | meteora | 95.66 | 0.0706 | 68.60 | 0.514334 | 4.5501 | 2.3255 |
| 7 | `HDhWhQCBrSh9xNWmNtsTi86eWj3yCoEiaRodjgNydo1b` | BTC-SOL | meteora | 94.01 | 0.0565 | 125.80 | 0.18196 | 1.9717 | 0.6822 |
| 8 | `HTvjzsfX3yU6BUodCjZ5vZkUrAxMDTrBs3CJaq43ashR` | SOL-USDC | meteora | 93.14 | -0.0284 | 130.48 | 0.0 | 4.5941 | 0.3406 |
| 9 | `7ubS3GccjhQY99AYNKXjNJqnXjaokEdfdV915xnCb96r` | BTC-USDC | meteora | 92.88 | 0.0282 | 25.51 | 0.525807 | 2.5801 | 1.3285 |
| 10 | `HktfL7iwGKT5QHjywQkcDnZXScoh811k7akrMZJkCcEF` | SOL-WHETH | orca | 91.40 | 0.0641 | 23.65 | 0.997965 | 0.7029 | 1.0050 |

### Top-10 by `net_daily_pct`

| # | pool_address | name | dex | net_daily_pct | score | fee APR | in_range | vol | ref width | conc | slip bps |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `2TAgfogn8JRwwTnsGKUa6WUT5Xdv1iMEzfEDMor68X9C` | ZEC-SOL | orca | 0.2344 | 97.51 | 103.98 | 0.830866 | 1.3389 | 1.0050 | 1.00 | 0.01 |
| 2 | `GTHKH8s82ZR8GTSFZ1dUu6wfdxhy59wpMShxzG5zjiPm` | ZEC-USDC | orca | 0.1798 | 96.06 | 144.11 | 0.498302 | 3.6647 | 1.8366 | 1.00 | 0.02 |
| 3 | `CgHuN3ajFijPidxiWStiDBUBkchWGNb8tVTGaNgYx9UJ` | XMR-SOL | raydium | 0.1545 | 88.84 | 141.11 | 0.499272 | 3.9826 | 1.9996 | 1.00 | 2.81 |
| 4 | `CeaZcxBNLpJWtxzt58qQmfMBtJY8pQLvursXTJYGQpbN` | SOL-BTC | orca | 0.0973 | 84.73 | 36.76 | 0.976726 | 0.9153 | 1.0050 | 1.00 | 0.0 |
| 5 | `Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE` | SOL-USDC | orca | 0.0729 | 96.91 | 59.70 | 0.504675 | 2.7797 | 1.3996 | 1.00 | 0.0 |
| 6 | `BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y` | SOL-USDC | meteora | 0.0706 | 95.66 | 68.60 | 0.514334 | 4.5501 | 2.3255 | 1.00 | 0.03 |
| 7 | `HktfL7iwGKT5QHjywQkcDnZXScoh811k7akrMZJkCcEF` | SOL-WHETH | orca | 0.0641 | 91.40 | 23.65 | 0.997965 | 0.7029 | 1.0050 | 1.00 | 0.0 |
| 8 | `HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM` | BTC-USDC | orca | 0.0598 | 96.00 | 39.15 | 0.596724 | 1.8091 | 1.0050 | 1.00 | 0.01 |
| 9 | `HDhWhQCBrSh9xNWmNtsTi86eWj3yCoEiaRodjgNydo1b` | BTC-SOL | meteora | 0.0565 | 94.01 | 125.80 | 0.18196 | 1.9717 | 0.6822 | 1.00 | 0.2 |
| 10 | `BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh` | SOL-USDC | meteora | 0.0474 | 83.93 | 42.93 | 0.579516 | 4.0526 | 2.2221 | 1.00 | 0.04 |

### 3 largest ranking inversions (score rank − net rank)

| Δrank | pool_address | name | dex | score rank | net rank | score | net_daily_pct | fee APR | in_range | vol | ref width | conc | slip bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| -18 | `3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv` | SOL-USDC | raydium | 11 | 29 | 90.47 | -0.0815 | 37.48 | 0.481607 | 10.2286 | 5.1163 | 1.00 | 0.03 |
| -17 | `HTvjzsfX3yU6BUodCjZ5vZkUrAxMDTrBs3CJaq43ashR` | SOL-USDC | meteora | 8 | 25 | 93.14 | -0.0284 | 130.48 | 0.0 | 4.5941 | 0.3406 | 1.00 | 0.31 |
| -17 | `GLfEiK21UZus4G5BeyUa9PMZZac1DzbyBx99K1d6enYy` | BMT-USDC | raydium | 23 | 40 | 69.99 | -0.4264 | 70.50 | 0.462141 | 19.1921 | 9.6031 | 1.00 | 8.28 |

---

## Attribution of the gate change

- **Contract change alone** (new evaluator, historical floors held): 0 flips —
  Missy's `safety.eligible` agrees with the old admission decision when the
  old floors are applied. No pool lost eligibility because of the contract.
- **Floor change** (Missy policy v2: 10k TVL / 1k volume / 0.02% fee-TVL):
  the 15 safety-eligible pools below the old `score >= 90` cut are the intended
  widening. Each is listed above with its Missy metrics; none is a Sheldon bug.
- **Range readiness** removes 20 safety-eligible records from the open path
  (`range_ready=false`). This is the expected no-RPC batch; production cron sets
  `SOLANA_RPC_URL`.
- **6 legacy opens lose to `range_ready=false`** while still safety-eligible:
  their reject codes are empty, and the reason is the live-tick gate, not
  economics. This is exactly the Phase-B signal the owner needs before flipping.

## Phase-B diff format on live scans

The shadow cycle attaches `pool_features_shadow` to the report (format in
[`docs/sheldon_contract_v1.md`](sheldon_contract_v1.md) §5):
gate counts, kept list, skipped list with `reject_codes`, `flips_in` /
`flips_out` attributed to `low_tvl_floor_lowered`, `score_bypassed` or
`range_ready=false`, and the top-10/inversions ranker diff. Emitted signals
stay the legacy ones until Phase C flips both flags.

## Reproduce

```bash
python3 - <<'PY'
import json, pool_features
pools = json.load(open("/Users/manthalib/Missy-agent/pool_screener/tests/fixtures/pool_scan_2026-10-29.json"))
cfg = dict(pool_features.DEFAULT_RANKER, enabled=True)
for p in pools:
    print(p["pool_address"], pool_features.gate_v1(p),
          pool_features.compute_net_yield(p, cfg)["net_daily_pct"])
PY
```
