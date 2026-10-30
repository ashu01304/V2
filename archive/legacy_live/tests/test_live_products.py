"""Offline checks: python -m unittest tests.test_live_products"""

import io
import json
import unittest
from unittest.mock import patch

from market_data.config import LIVE_PRODUCTS
from market_data.contracts import price_recipe
from market_data.live_history import spread_closes
from market_data.live_service import get_instruments


class LiveProductTests(unittest.TestCase):
    def test_default_product_selection(self):
        self.assertEqual(LIVE_PRODUCTS, ("CL", "CO", "G", "HO", "NG", "RB"))

    def test_unrelated_products_rejected_before_request(self):
        with patch("market_data.live_service.urlopen") as request:
            with self.assertRaises(ValueError):
                get_instruments(["CL", "GC"])
            request.assert_not_called()

    def test_aliases_use_saved_structures_without_catalogue_request(self):
        with patch("market_data.live_service.urlopen") as request:
            instruments = get_instruments(["LCO", "CO", "LGO", "G"])
            request.assert_not_called()
        self.assertTrue(instruments)
        self.assertEqual({product for product, _ in instruments.values()}, {'CO', 'G'})
        self.assertTrue(all('-' in expression for _, expression in instruments.values()))

    def test_gasoil_minutes_match_stored_gasoil_spreads(self):
        frame = spread_closes([(0, "G", "Z26", 700.0, 0), (0, "G", "F27", 698.0, 0)], ["LGOZ6-F7"])
        self.assertEqual(frame.instrument.tolist(), ["LGOZ6-F7"])
        self.assertEqual(frame.price.tolist(), [2.0])
        self.assertEqual(price_recipe("G", "Z26-F27"), {"LGOZ6-F7": 1.0})


if __name__ == "__main__":
    unittest.main()
