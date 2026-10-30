"""Offline checks of the complete request → registry → feed → publication flow."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from structure_lab.live_feed import LiveFeed
from structure_lab.request_store import RequestStore
from structure_lab.subscription_service import SubscriptionService
from structure_lab.price_manager import PriceManager
from structure_lab.manager_server import create_app
from structure_lab.tests.test_live_feed import lookup


class FakeFeed(LiveFeed):
    def __init__(self):
        super().__init__()
        self.starts=[]
        self.stops=0

    def start_metadata(self,metadata):
        self.starts.append(set(metadata))
        self.metadata=dict(metadata)
        self.accepted=set(metadata)
        self.groups={'0':'CONNECTED:WS-STREAMING'}
        self.status='Connected'
        self.books={tt:dict(bid=.1*(int(tt[3:])%100),ask=.1*(int(tt[3:])%100)+.02,
            bid_size=20,ask_size=20,received=time.time(),received_at='now',
            group='0',snapshot=False,live_updates=1) for tt in metadata}
        return len(metadata)

    def stop(self):
        self.stops+=1
        super().stop()

    def update_metadata(self,metadata):
        if metadata:
            self.start_metadata(metadata)
        else:
            self.metadata={}
            self.accepted.clear()
            self.books={}


class DemandManagerTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='lab_demand_test_')
        self.now=1000.
        self.store=RequestStore(Path(self.folder.name)/'requests.sqlite3',clock=lambda:self.now)
        self.feed=FakeFeed()
        self.manager=PriceManager(self.store,SubscriptionService(self.store,self.feed))
        self.browser=create_app(self.manager).test_client()
        self.patches=[patch('structure_lab.live_feed.index',return_value=lookup()),
                      patch('structure_lab.interpretation.index',return_value=lookup())]
        for item in self.patches:
            item.start()

    def tearDown(self):
        self.manager.stop()
        for item in self.patches:
            item.stop()
        self.folder.cleanup()

    def register(self,owner='dashboard-a',expressions=None):
        response=self.browser.post('/requests',json=dict(owner=owner,structures=expressions or ['CL F27 1MDF']))
        self.assertEqual(response.status_code,201,response.get_data(as_text=True))
        return response.get_json()['requests']

    def test_complete_structure_published_under_original_request(self):
        expression='CL F27 1MF - CL G27 1MF'
        row=self.register(expressions=[expression])[0]
        self.assertEqual(self.feed.starts,[])  # Registering does not open a connection.
        self.manager.cycle()
        result=self.browser.get('/quotes/'+row['request_id']).get_json()
        self.assertEqual(result['expression'],expression)
        quote=result['quote']
        self.assertEqual(quote['expression'],expression)
        self.assertEqual(quote['accepted'],5)
        self.assertEqual(set(quote['books']),set(row['required_ids']))
        overall=quote['overall']
        self.assertIsNotNone(overall['bid'])
        self.assertIsNotNone(overall['ask'])
        self.assertAlmostEqual(overall['mid'],(overall['bid']+overall['ask'])/2)
        self.assertTrue(overall['bid_recipe'])

    def test_shared_demand_deduplicates_and_release_preserves_other_owner(self):
        first=self.register()[0]
        second=self.register('dashboard-b')[0]
        self.assertNotEqual(first['request_id'],second['request_id'])
        self.manager.cycle()
        self.assertEqual(len(self.feed.starts),1)
        self.assertEqual(len(self.feed.starts[0]),5)
        self.manager.cycle()
        self.assertEqual(len(self.feed.starts),1)
        self.assertFalse(self.store.release('dashboard-b',first['request_id']))
        self.store.release('dashboard-a',first['request_id'])
        self.manager.cycle()
        self.assertEqual(len(self.feed.starts),1)
        self.assertEqual(len(self.feed.accepted),5)
        self.store.release('dashboard-b',second['request_id'])
        self.manager.cycle()
        self.assertEqual(self.feed.accepted,set())
        self.assertEqual(self.store.desired(),{})

    def test_expiry_removes_requirements_and_cached_results(self):
        row=self.register()[0]
        self.manager.cycle()
        self.now+=1799
        self.manager.cycle()
        self.assertIsNotNone(self.store.read(row['request_id']))
        self.now+=2
        self.manager.cycle()
        self.assertIsNone(self.store.read(row['request_id']))
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM required_contracts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM published_quotes').fetchone()[0],0)
        self.assertFalse(self.feed.accepted)

    def test_get_does_not_renew_but_owned_heartbeat_does(self):
        row=self.register()[0]
        self.now+=60
        self.assertEqual(self.browser.get('/quotes/'+row['request_id']).status_code,200)
        self.assertEqual(self.store.read(row['request_id'])['expires_at'],2800)
        self.assertEqual(self.store.renew('wrong-owner',[row['request_id']]),0)
        self.assertEqual(self.store.renew('dashboard-a',[row['request_id']]),1)
        self.now+=60
        self.assertIsNotNone(self.store.read(row['request_id']))

    def test_batch_invalid_is_atomic_and_no_outright_subscription(self):
        response=self.browser.post('/requests',json=dict(owner='a',structures=['CL F27 1MF','CL F27 OUT']))
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.store.active(),[])
        self.assertEqual(self.feed.starts,[])
        for ttl in (True,None,'1800',float('nan'),0,1801):
            response=self.browser.post('/requests',json=dict(owner='a',structures=['CL F27 1MF'],ttl=ttl))
            self.assertEqual(response.status_code,400)

    def test_multiple_targets_do_not_leak_component_quotes(self):
        rows=self.register(expressions=['CL F27 1MF','CL G27 1MF'])
        self.manager.cycle()
        result=self.browser.post('/quotes',json=dict(request_ids=[r['request_id'] for r in rows])).get_json()['quotes']
        self.assertEqual(len(result),2)
        for row,quote in zip(rows,result):
            self.assertEqual(set(quote['quote']['books']),set(row['required_ids']))
            self.assertEqual(quote['expression'],row['expression'])
        status=self.browser.get('/status').get_json()
        self.assertEqual(status['active_requests'],2)
        self.assertEqual(status['required'],len(set().union(*(set(r['required_ids']) for r in rows))))

    def test_cancelled_intermediate_month_still_allows_requested_components(self):
        row=self.register(expressions=['CL F27 1MS + CL G27 1MS'])[0]
        self.assertTrue({'TT-3','TT-4'}<=set(row['required_ids']))
        self.assertNotIn('TT-99',row['required_ids'])
        self.manager.cycle()
        quote=self.store.read(row['request_id'])['quote']['overall']
        self.assertIsNotNone(quote['mid'])

    def test_old_publication_is_not_served_as_live(self):
        row=self.register()[0]
        self.manager.cycle()
        self.now+=11
        quote=self.browser.get('/quotes/'+row['request_id']).get_json()
        self.assertIsNone(quote['quote'])
        self.assertIn('stale',quote['status'])


if __name__=='__main__':
    unittest.main()
