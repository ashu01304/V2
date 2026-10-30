"""Offline production feed checks; no network connections or listening ports."""
import unittest
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

from market_data.mds_feed import CONFIG_PATH, load_selection, contract_expression, connect
from market_data.live_service import PriceStore
from market_data.live_client import LiveMarketDataClient


class MDSFeedTests(unittest.TestCase):
    def test_selection_uses_filtered_file_and_real_fly_names(self):
        metadata, batch = load_selection()
        config = json.loads(CONFIG_PATH.read_text())
        catalogue = json.loads((CONFIG_PATH.parent / config['catalogue_file']).read_text())['contracts']
        expected = 0
        for product in config['products']:
            structures = catalogue['Boxes' if product == '+1xCL' else product]
            if isinstance(structures, list):
                structures = {'boxes': structures}
            expected += sum(len(rows) for kind, rows in structures.items() if kind in config['structures'])
        self.assertEqual(len(metadata), expected)
        self.assertEqual(batch, config['batch_size'])
        self.assertEqual(contract_expression('BRN Jan27 Jun27 Jul27 Butterfly', 'flies'), 'F27-2*M27+N27')
        selected, _ = load_selection(products=['LCO'])
        self.assertTrue(all(m['product'] == 'CO' for m in selected.values()))

    def store(self):
        metadata = {
            '1-2': dict(product='CL', contract='F27', structure='outrights'),
            '2-2': dict(product='CL', contract='G27', structure='outrights'),
            '3-2': dict(product='CL', contract='F27-G27', structure='spreads'),
            '4-2': dict(product='CL-CO', contract='F27-G27', structure='boxes'),
        }
        for m in metadata.values():
            m.update(name=m['contract'], route='+1xCL' if m['structure'] == 'boxes' else m['product'])
        store = PriceStore({i: (m['product'], m['contract']) for i,m in metadata.items()}, recorder=Mock(), metadata=metadata)
        store.status = 'CONNECTED:MDS'
        return store

    def test_direct_quote_beats_outrights_and_reverses_bid_ask(self):
        store = self.store()
        store.update('1-2', {'tradePrice': '70'})
        store.update('2-2', {'tradePrice': '69'})
        store.update('3-2', {'tradePrice': '0.75', 'bidPrice': '0.7', 'askPrice': '0.8'})
        quote = store.expression('CL', 'F27-G27')
        self.assertEqual((quote['value'], quote['source']), (0.75, 'direct'))
        reverse = store.expression('CL', '2*G27-2*F27')
        self.assertEqual((reverse['value'], reverse['bid'], reverse['ask']), (-1.5, -1.6, -1.4))
        self.assertEqual(store.recorder.record.call_count, 2)
        store.rows[('CL', 'F27-G27')]['_received'] = (datetime.now(timezone.utc)-timedelta(seconds=121)).timestamp()
        with self.assertRaises(LookupError):
            store.expression('CL', 'F27-G27')

    def test_box_direct_and_independent_connection_freshness(self):
        store = self.store()
        store.update('4-2', {'bidPrice': '0.1', 'askPrice': '0.2'})
        self.assertAlmostEqual(store.expression('CL-CO', 'F27-G27')['value'], 0.15)
        store.item_users['4-2'] = 'box-user'
        store.connections['box-user'] = 'DISCONNECTED'
        with self.assertRaises(LookupError):
            store.expression('CL-CO', 'F27-G27')

    def test_authenticated_groups_and_batches(self):
        store = self.store()
        metadata = {str(i)+'-2': dict(route='CL') for i in range(501)}
        metadata['box-2'] = dict(route='+1xCL')
        auth = {'CL': ('cl-user','secret-one'), '+1xCL': ('box-user','secret-two')}
        with patch('market_data.mds_feed.credentials', return_value=auth), \
             patch('lightstreamer.client.LightstreamerClient') as client, \
             patch('lightstreamer.client.Subscription') as subscription:
            clients = connect(store, metadata, 250)
        self.assertEqual(len(clients), 2)
        self.assertEqual([len(c.args[1]) for c in subscription.call_args_list], [250,250,1,1])
        self.assertEqual(client.call_count, 2)
        self.assertEqual(store.item_users['box-2'], 'box-user')

    def test_outright_subscription_rejected_before_authentication(self):
        with patch('market_data.mds_feed.credentials') as credentials:
            with self.assertRaisesRegex(ValueError,'Outright subscriptions are disabled'):
                connect(self.store(), {'1-2': {'structure':'outrights'}}, 250)
            credentials.assert_not_called()

    def test_existing_snapshot_client_keeps_forward_curves_outright_only(self):
        client = LiveMarketDataClient()
        client.session = Mock()
        client.session.get.return_value.json.return_value = [
            dict(contract='F27', structure='outrights', value=70),
            dict(contract='F27-G27', structure='spreads', value=0.75),
        ]
        self.assertEqual(len(client.snapshot()), 1)
        self.assertEqual(len(client.snapshot(include_structures=True)), 2)


if __name__ == '__main__':
    unittest.main()
