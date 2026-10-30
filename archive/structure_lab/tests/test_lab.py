from datetime import datetime,timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch,Mock
import duckdb

from structure_lab.expression import parse
from structure_lab.data_client import DataClient
from structure_lab.resolvers import seac,minute,live
from structure_lab.sources import read_database


class NamedStructureTests(unittest.TestCase):
    def test_compact_and_spaced_names_preserve_two_flies(self):
        compact=parse('CL X271MF - CL Z271MF')
        spaced=parse('CL X27 1MF - CL Z27 1MF')
        self.assertEqual(compact.terms,spaced.terms)
        self.assertEqual(len(compact.terms),2)
        self.assertEqual(compact.exposures(),{('CL','X27'):1,('CL','Z27'):-3,('CL','F28'):3,('CL','G28'):-1})
        self.assertEqual(compact.expression('CL'),'X27-3*Z27+3*F28-G28')

    def test_defly_count_aliases_and_cross_product(self):
        self.assertEqual(parse('CL X27 1DF').exposures(),parse('CL X27 1MF - CL Z27 1MF').exposures())
        self.assertEqual(parse('2 * BRN X27 3MS').exposures(),{('CO','X27'):2,('CO','G28'):-2})
        self.assertEqual(parse('CL-CO X27 1MS').settlement_legs(),{
            ('CL','X27'):1,('CO','X27'):-1,('CL','Z27'):-1,('CO','Z27'):1})

    def test_mdf_notation_across_resolvers(self):
        request=parse('CL X27 1MDF')
        for text in ('CL X271MDF','cl x27 1mdf','CL X27 1DF'):
            self.assertEqual(parse(text).terms,request.terms)
        self.assertEqual(request.named_rows()[0]['structure'],'CL X27 1MDF')
        self.assertEqual(request.expression('CL'),'X27-3*Z27+3*F28-G28')
        self.assertFalse(seac.resolve(request,'2026-10-28','2026-10-29')[0].empty)
        self.assertFalse(minute.resolve(request,'2026-10-28','2026-10-29')[0].empty)
        quote,recipe=live.resolve(request)
        self.assertEqual(len(recipe),2)
        self.assertIsNotNone(quote['bid'])

    def test_invalid_and_cancelled_expressions(self):
        for text in ('CL X27','CL X27 0MF','CL X27 1MF garbage','CL X27 1MF CL Z27 1MF',
                     'CL X27 1MF - CL X27 1MF','GC X27 1MF','CL Z2099 1MF'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                parse(text)

    def test_mddf_notation_and_all_three_resolvers(self):
        request=parse('CL X27 1MDDF')
        for text in ('CL X271MDDF','cl x27 1mddf','CL X27 1DDF'):
            self.assertEqual(parse(text).terms,request.terms)
        self.assertEqual(request.named_rows()[0]['structure'],'CL X27 1MDDF')
        self.assertEqual(request.exposures(),{
            ('CL','X27'):1,('CL','Z27'):-4,('CL','F28'):6,('CL','G28'):-4,('CL','H28'):1})
        self.assertEqual(request.exposures(),parse('CL X27 1MDF - CL Z27 1MDF').exposures())
        self.assertEqual(parse('2 * CL X27 2MDDF').exposures(),
                         parse('2 * CL X27 2MDF - 2 * CL F28 2MDF').exposures())
        self.assertFalse(seac.resolve(request,'2026-10-28','2026-10-29')[0].empty)
        self.assertFalse(minute.resolve(request,'2026-10-28','2026-10-29')[0].empty)
        quote,recipe=live.resolve(request)
        self.assertEqual([r['weight'] for r in recipe],[1,-2,1])
        self.assertAlmostEqual(quote['bid'],recipe[0]['bid']-2*recipe[1]['ask']+recipe[2]['bid'])
        self.assertAlmostEqual(quote['ask'],recipe[0]['ask']-2*recipe[1]['bid']+recipe[2]['ask'])

    def test_live_demo_uses_named_flies_and_correct_side_for_negative_count(self):
        request=parse('CL X27 1MF - CL Z27 1MF')
        quote,recipe=live.resolve(request)
        self.assertEqual(len(recipe),2)
        self.assertEqual([r['weight'] for r in recipe],[1,-1])
        expected_bid=recipe[0]['bid']-recipe[1]['ask']
        expected_ask=recipe[0]['ask']-recipe[1]['bid']
        self.assertAlmostEqual(quote['bid'],expected_bid)
        self.assertAlmostEqual(quote['ask'],expected_ask)
        self.assertAlmostEqual(quote['mid'],(expected_bid+expected_ask)/2)

    def test_live_existing_adapter_sends_one_complete_expression(self):
        response=Mock()
        response.json.return_value={'quotes':[dict(value=.2,bid=.19,ask=.21,timestamp='2026-10-30T10:00:00Z',source='quoted_flies')]}
        with patch('structure_lab.resolvers.live.requests.post',return_value=response) as post:
            quote,recipe=live.resolve(parse('CL X271MF - CL Z271MF'),profile='existing')
        self.assertAlmostEqual(quote['mid'],.2)
        self.assertEqual(post.call_args.kwargs['json']['expressions'],['X27-3*Z27+3*F28-G28'])
        self.assertEqual(post.call_args.args[0],'http://127.0.0.1:8060/quotes')

    def test_live_outrights_and_remote_urls_rejected(self):
        with patch('structure_lab.resolvers.live.requests.post') as post:
            with self.assertRaises(ValueError):
                live.resolve(parse('CL X27 OUT'),profile='existing')
            with self.assertRaises(ValueError):
                live.resolve(parse('CL X27 1MF'),profile='existing',service_url='https://example.com')
            post.assert_not_called()


class HistoryAndIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='structure_lab_test_')
        self.folder=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def database(self):
        path=self.folder/'fixture.duckdb'
        connection=duckdb.connect(str(path))
        connection.execute('CREATE TABLE seac_settlements(symbol VARCHAR,contract_code VARCHAR,trading_date DATE,price DOUBLE)')
        connection.execute("INSERT INTO seac_settlements VALUES ('CL','F27','2026-10-28',70),('CL','G27','2026-10-28',69),('CL','H27','2026-10-28',67),('CL','F27','2026-10-29',71)")
        connection.execute('CREATE TABLE spread_prices(timestamp TIMESTAMPTZ,instrument VARCHAR,price DOUBLE)')
        connection.execute("INSERT INTO spread_prices VALUES ('2026-10-28 12:00:00+00','CLF27-G27',5),('2026-10-28 12:00:00+00','CLG27-H27',3),('2026-10-28 12:01:00+00','CLF27-G27',7),('2026-10-28 12:02:00+00','CLF27-G27',6),('2026-10-28 12:02:00+00','CLG27-H27',2)")
        connection.close()
        return path

    def test_seac_aligns_settlement_dates_without_writing_database(self):
        path=self.database()
        before=path.stat().st_mtime_ns
        frame,recipe=seac.resolve(parse('CL F27 1MF'),'2026-10-28','2026-10-29','existing',path)
        self.assertEqual(frame.price.tolist(),[-1])
        self.assertEqual(path.stat().st_mtime_ns,before)
        with read_database(path) as connection,self.assertRaises(duckdb.Error):
            connection.execute('CREATE TABLE forbidden(i INTEGER)')

    def test_minute_alignment_precedes_candle_aggregation(self):
        path=self.database()
        before=path.stat().st_mtime_ns
        candles,recipe=minute.resolve(parse('CL F27 1MF'),'2026-10-28','2026-10-28',5,'existing',path)
        self.assertEqual(recipe,{'CLF27-G27':1,'CLG27-H27':-1})
        self.assertEqual(candles[['open','high','low','close']].values.tolist(),[[2,4,2,4]])
        self.assertEqual(path.stat().st_mtime_ns,before)

    def test_demo_all_three_paths_never_call_network(self):
        client=DataClient(self.folder/'runtime.sqlite3')
        with patch('structure_lab.resolvers.live.requests.post',side_effect=AssertionError('No demo network')) as post:
            for source in ('seac','minute','live'):
                result=client.resolve('CL X271MF - CL Z271MF',source=source,start='2026-10-28',end='2026-10-29')
                self.assertTrue(result['data'])
                self.assertEqual(len(result['named']),2)
            post.assert_not_called()
        self.assertEqual(client.manager.status()['active_requests'],1)
        self.assertTrue((self.folder/'runtime.sqlite3').exists())

    def test_expired_lab_requests_are_not_counted_as_active(self):
        client=DataClient(self.folder/'runtime.sqlite3')
        client.resolve('CL X27 1MF')
        with client.manager.connection() as connection:
            connection.execute('UPDATE requests SET expires_at=0')
        self.assertEqual(client.manager.status()['active_requests'],0)

    def test_dashboard_and_interpret_api_never_fetch_or_write(self):
        from structure_lab import dashboard
        with patch('requests.post',side_effect=AssertionError('No network')) as post, \
             patch('duckdb.connect',side_effect=AssertionError('No database')) as database, \
             patch('sqlite3.connect',side_effect=AssertionError('No runtime writes')) as cache:
            browser=dashboard.app.server.test_client()
            self.assertEqual(browser.get('/_dash-layout').status_code,200)
            response=browser.post('/api/interpret',json={'expression':'CL X27 1MDF'})
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            result=response.get_json()
            self.assertEqual([r['weight'] for r in result['seac']['rows']],[1,-3,3,-1])
            self.assertEqual([r['weight'] for r in result['minute']['rows']],[1,-2,1])
            self.assertEqual([r['weight'] for r in result['live']['rows']],[1,-1])
            self.assertEqual(browser.post('/api/interpret',json={'expression':'bad'}).status_code,400)
            post.assert_not_called()
            database.assert_not_called()
            cache.assert_not_called()

    def test_dashboard_shows_all_three_interpretations_together(self):
        from structure_lab import dashboard
        browser=dashboard.app.server.test_client()
        key=next(k for k in dashboard.app.callback_map if 'breakdowns.children' in k)
        for expression in ('CL X271MF - CL Z271MF','CL X27 1MDF','CL X27 1MDDF'):
            response=browser.post('/_dash-update-component',json={'output':key,
                'outputs':[{'id':'breakdowns','property':'children'},{'id':'message','property':'children'}],
                'inputs':[{'id':'expression','property':'value','value':expression},{'id':'live-tick','property':'n_intervals','value':0}],
                'state':[{'id':'breakdowns','property':'children','value':None},{'id':'expanded-sections','property':'data','value':{}}], 'changedPropIds':['expression.value']})
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            body=response.get_json()['response']
            self.assertEqual(body['message']['children'],'')
            self.assertEqual(len(body['breakdowns']['children']),3)

    def test_tt_lookup_uses_names_preserves_large_ids_and_reports_missing(self):
        from structure_lab.catalogue import index,named_ids,spread_ids
        catalogue=self.folder/'catalogue.json'
        catalogue.write_text(json.dumps({'contracts':{'CL':{'flies':[
            {'name':'CL Nov27 1mo Butterfly','code':'CLX27-X27-X27','id':'18446744073709551615'},
            {'name':'CL Nov27 1mo Butterfly','id':'123'}],
            'spreads':[{'name':'CL Nov27-Dec27 Calendar','id':'456'}]}}}),encoding='utf-8')
        lookup=index(catalogue)
        term=parse('CL X27 1MF').terms[0]
        self.assertEqual(named_ids(term,lookup),['TT-18446744073709551615','TT-123'])
        self.assertEqual(spread_ids('CLX27-Z27',lookup),['TT-456'])
        self.assertEqual(named_ids(parse('CL H28 1MF').terms[0],lookup),[])

    def test_live_cards_display_tt_ids_or_explicit_missing_label(self):
        from structure_lab import dashboard
        from structure_lab.interpretation import interpret
        from structure_lab.catalogue import signature
        key=signature('CL',[('X27',1),('Z27',-2),('F28',1)])
        with patch('structure_lab.interpretation.index',return_value={key:['TT-123']}):
            result=interpret('CL X27 1MF - CL Z27 1MF')
        self.assertEqual(result['live']['rows'][0]['tt_ids'],['TT-123'])
        self.assertEqual(result['live']['rows'][1]['tt_ids'],[])
        # Render through Dash's callback serialization, keeping the UI's labels visible.
        with patch('structure_lab.dashboard.interpret',return_value=result):
            browser=dashboard.app.server.test_client()
            key=next(k for k in dashboard.app.callback_map if 'breakdowns.children' in k)
            response=browser.post('/_dash-update-component',json={'output':key,
                'outputs':[{'id':'breakdowns','property':'children'},{'id':'message','property':'children'}],
                'inputs':[{'id':'expression','property':'value','value':result['expression']},{'id':'live-tick','property':'n_intervals','value':0}],
                'state':[{'id':'breakdowns','property':'children','value':None},{'id':'expanded-sections','property':'data','value':{}}], 'changedPropIds':['expression.value']})
            self.assertEqual(response.status_code,200)
            self.assertIn('TT-123',response.get_data(as_text=True))
            self.assertIn('Not in saved catalogue',response.get_data(as_text=True))

    def test_aliases_differ_for_minute_and_live_and_outrights_are_source_specific(self):
        from structure_lab.interpretation import interpret
        result=interpret('CO X27 1MS')
        self.assertEqual(result['minute']['rows'][0]['instrument'],'LCOX7-Z7')
        self.assertEqual(result['live']['rows'][0]['instrument'],'CO X27 1MS')
        result=interpret('CL X27 OUT')
        self.assertEqual(result['seac']['rows'][0]['instrument'],'CL X27')
        self.assertIn('error',result['minute'])
        self.assertIn('error',result['live'])


if __name__=='__main__':
    unittest.main()
