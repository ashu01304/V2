"""Best sides can use distinct exact recipes; no feed or database required."""
import unittest
from market_data.live_service import PriceStore


def make_store(records):
    meta = {str(i): dict(product='CL',contract=e,structure='spreads',name=e) for i,(e,*_) in enumerate(records)}
    store = PriceStore({i: ('CL',m['contract']) for i,m in meta.items()}, metadata=meta)
    store.status = 'CONNECTED:MDS'
    for i,(_,bid,ask) in enumerate(records):
        store.update(str(i),dict(bidPrice=bid,askPrice=ask,bidQuantity=10,askQuantity=20))
    return store


class BestQuoteTests(unittest.TestCase):
    def test_bid_and_ask_choose_different_two_structure_routes(self):
        store = make_store([('F27-J27',.9,1.5), ('F27-G27',.6,.8), ('G27-J27',.4,.6),
                            ('F27-H27',.5,.55), ('H27-J27',.4,.55)])
        quote = store.expression('CL','F27-J27')
        self.assertAlmostEqual(quote['bid'],1)
        self.assertAlmostEqual(quote['ask'],1.1)
        self.assertEqual((quote['bid_components'],quote['ask_components']),(2,2))
        self.assertTrue(quote['bid_optimal'] and quote['ask_optimal'])
        reverse = store.expression('CL','J27-F27')
        self.assertAlmostEqual(reverse['bid'],-1.1)
        self.assertAlmostEqual(reverse['ask'],-1)

    def test_three_structure_replication(self):
        store = make_store([('F27-G27',.2,.3), ('G27-H27',.3,.4), ('H27-J27',.4,.5)])
        quote = store.expression('CL','F27-J27')
        self.assertAlmostEqual(quote['bid'],.9)
        self.assertAlmostEqual(quote['ask'],1.2)
        self.assertEqual(quote['ask_components'],3)

    def test_missing_side_is_not_replaced_with_last_or_midpoint(self):
        store = make_store([('F27-G27',.2,None)])
        quote = store.expression('CL','F27-G27')
        self.assertAlmostEqual(quote['bid'],.2)
        self.assertIsNone(quote['ask'])
        self.assertIsNone(quote['value'])

    def test_stale_route_is_excluded(self):
        store = make_store([('F27-J27',.9,1.5), ('F27-G27',.6,.8), ('G27-J27',.4,.6)])
        store.rows[('CL','G27-J27')]['_received'] -= 121
        quote = store.expression('CL','F27-J27')
        self.assertAlmostEqual(quote['bid'],.9)
        self.assertAlmostEqual(quote['ask'],1.5)


if __name__ == '__main__':
    unittest.main()
