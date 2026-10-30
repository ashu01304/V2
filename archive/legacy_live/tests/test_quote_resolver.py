"""Exact structure replication and executable side arithmetic; no network."""
import unittest
from unittest.mock import Mock
from datetime import datetime, timezone, timedelta

import pandas as pd
from market_data.live_service import PriceStore
from market_data.structure_curve import ForwardCurve
from market_data.structure_seasonality import Seasonality
from market_data.live_client import LiveMarketDataClient


class QuotedResolverTests(unittest.TestCase):
    def test_old_service_curve_endpoint_reports_restart_instruction(self):
        client = LiveMarketDataClient()
        client.session = Mock()
        client.session.get.return_value.status_code = 404
        with self.assertRaisesRegex(RuntimeError, 'restart market_data/live_service.py'):
            client.curve('CL', 'F27-G27')

    def store(self, definitions):
        meta = {str(i): dict(product='CL', contract=expression, structure=kind, name=expression)
                for i, (expression, kind, _) in enumerate(definitions)}
        store = PriceStore({i: ('CL', m['contract']) for i,m in meta.items()}, metadata=meta)
        store.status = 'CONNECTED:MDS'
        for i, (_, _, price) in enumerate(definitions):
            store.update(str(i), dict(tradePrice=str(price), bidPrice=str(price-.01), askPrice=str(price+.01),
                                     bidQuantity='10', askQuantity='20'))
        return store

    def test_defly_prefers_two_quoted_flies_before_spreads(self):
        store = self.store([('F27-2*G27+H27','flies',.3), ('G27-2*H27+J27','flies',.1),
                            ('F27-G27','spreads',5), ('G27-H27','spreads',2), ('H27-J27','spreads',1)])
        quote = store.expression('CL','F27-3*G27+3*H27-J27', best_sides=False)
        self.assertEqual(quote['source'], 'quoted_flies')
        self.assertEqual(quote['component_count'], 2)
        self.assertAlmostEqual(quote['value'], .2)
        self.assertAlmostEqual(quote['bid'], .18)
        self.assertAlmostEqual(quote['ask'], .22)
        self.assertEqual((quote['bid_size'],quote['ask_size']), (10,10))

    def test_defly_falls_back_to_quoted_spreads_if_fly_is_stale(self):
        store = self.store([('F27-2*G27+H27','flies',.3), ('G27-2*H27+J27','flies',.1),
                            ('F27-G27','spreads',5), ('G27-H27','spreads',2), ('H27-J27','spreads',1)])
        store.rows[('CL','G27-2*H27+J27')]['_received'] -= 121
        quote = store.expression('CL','F27-3*G27+3*H27-J27', best_sides=False)
        self.assertEqual(quote['source'], 'quoted_spreads')
        self.assertAlmostEqual(quote['value'], 2)
        self.assertAlmostEqual(quote['bid'], 1.96)
        self.assertAlmostEqual(quote['ask'], 2.04)

    def test_long_spread_uses_intermediate_quoted_spreads(self):
        store = self.store([('F27-N27','spreads',1), ('N27-F28','spreads',2)])
        quote = store.expression('CL','F27-F28')
        self.assertEqual(quote['source'], 'quoted_spreads')
        self.assertEqual(quote['value'],3)
        with self.assertRaises(LookupError):
            store.expression('CL','F27')

    def test_no_outright_fallback_and_no_invented_missing_bid(self):
        store = self.store([('F27','outrights',70), ('G27','outrights',69), ('F27-G27','spreads',.5)])
        store.rows[('CL','F27-G27')]['bid'] = None
        self.assertIsNone(store.expression('CL','F27-G27')['bid'])
        store.rows[('CL','F27-G27')]['_received'] -= 121
        with self.assertRaises(LookupError):
            store.expression('CL','F27-G27')

    def test_exact_mixed_replication_and_disconnected_component_rejection(self):
        store = self.store([('F27-2*G27+H27','flies',.3), ('H27-J27','spreads',.1)])
        expression = 'F27-2*G27+2*H27-J27'
        quote = store.expression('CL',expression)
        self.assertEqual(quote['source'],'quoted_mixed')
        self.assertAlmostEqual(quote['value'],.4)
        store.item_users['1'] = 'disconnected-user'
        store.connections['disconnected-user'] = 'DISCONNECTED'
        with self.assertRaises(LookupError):
            store.expression('CL',expression)

    def test_batch_unavailable_quote_does_not_block_valid_expression(self):
        store = self.store([('F27-G27','spreads',1)])
        quotes = store.quotes('CL',['F27','F27-G27'])['quotes']
        self.assertIsNone(quotes[0]['value'])
        self.assertEqual(quotes[1]['value'],1)

    def test_forward_curve_uses_resolver_without_snapshot(self):
        store = self.store([('F27-G27','spreads',1), ('G27-H27','spreads',.8)])
        points = store.resolver.curve('CL','F27-G27')['live']
        self.assertEqual([p['price'] for p in points], [1,.8])
        live, db = Mock(), Mock()
        live.curve.return_value = {'live': points}
        db.settlement_dates.return_value = []
        result = ForwardCurve(live=live,db=db).calculate('CL','F27-G27')
        self.assertEqual(result['live'],points)
        live.snapshot.assert_not_called()

    def test_cross_product_initial_overlay_requests_complete_box(self):
        live = Mock()
        live.expression.return_value = dict(value=.4,timestamp='2026-10-29T12:00:00Z')
        engine = Seasonality(Mock(),live=live)
        series, error = engine._live_daily_overlay('CL-CO','F27-G27',0,pd.Timestamp('2026-10-28'),pd.Timestamp('2027-01-01'))
        self.assertIsNone(error)
        self.assertEqual(series.iloc[0],.4)
        live.expression.assert_called_once_with('CL-CO','F27-G27')


if __name__ == '__main__':
    unittest.main()
