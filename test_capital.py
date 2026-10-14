"""Regression tests for capital.py mint constants and wallet classification.

2026-10-14: commit 3841104 dropped one character ('u') from USDC_MINT
(EPjFWdd5Afq... instead of EPjFWdd5Aufq...). summarize_wallet() then
classified the wallet's real USDC as dust: idle_usdc=0, so
open_eligible() returned False and every cycle built 0 strategies while
Missy's screen showed 324 USDC of idle capital. These tests pin the real
SPL USDC mint byte-for-byte and assert real USDC classifies as idle.
"""

import unittest

from capital import USDC_MINT, summarize_wallet

REAL_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class TestUsdcMintConstant(unittest.TestCase):
    def test_mint_matches_real_usdc_byte_for_byte(self):
        self.assertEqual(USDC_MINT, REAL_USDC_MINT)
        self.assertEqual(len(USDC_MINT), 44)

    def test_real_usdc_classifies_as_idle_not_dust(self):
        screen = {
            "wallet": "B8qv53pKyW2sgP4VYik1GkkZ6NHNZZn6K5VTyVm55tTa",
            "wallet_id": "main",
            "total_usd": 367.1951,
            "assets": [
                {
                    "mint": REAL_USDC_MINT,
                    "symbol": "USDC",
                    "decimals": 6,
                    "amount_raw": "324675567",
                    "amount_ui": 324.675567,
                    "price_usd": 0.9998,
                    "total_value_usd": 324.6152509755521,
                    "is_native_sol": False,
                },
                {
                    "mint": "So11111111111111111111111111111111111111112",
                    "symbol": "SOL",
                    "decimals": 9,
                    "amount_raw": "393270334",
                    "amount_ui": 0.393270334,
                    "price_usd": 107.20,
                    "total_value_usd": 42.15915436192038,
                    "is_native_sol": True,
                },
            ],
        }
        summary = summarize_wallet(screen)
        self.assertAlmostEqual(summary["idle_usdc"], 324.6152509755521)
        self.assertEqual(summary["dust_assets"], [])
        self.assertAlmostEqual(summary["dust_total_usdc"], 0.0)


if __name__ == "__main__":
    unittest.main()
