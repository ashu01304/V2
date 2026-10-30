from datetime import datetime,timezone
import unittest
from unittest.mock import patch,MagicMock
from structure_lab.live_feed import LiveFeed,selection
from structure_lab.catalogue import signature


def lookup():
    return {
        signature('CL',[('F27',1),('G27',-2),('H27',1)]):['TT-1'],
        signature('CL',[('G27',1),('H27',-2),('J27',1)]):['TT-2'],
        signature('CL',[('F27',1),('G27',-1)]):['TT-3'],
        signature('CL',[('G27',1),('H27',-1)]):['TT-4'],
        signature('CL',[('H27',1),('J27',-1)]):['TT-5'],
        signature('CL',[('Z28',1),('F29',-1)]):['TT-99'],
    }


class LabFeedTests(unittest.TestCase):
    def feed(self):
        request,target,metadata=selection('CL F27 1MDF',lookup())
        feed=LiveFeed()
        feed.expression=request.text
        feed.target=target
        feed.metadata=metadata
        feed.accepted=set(metadata)
        feed.groups={'0':'CONNECTED:WS-STREAMING'}
        feed.status='Connected'
        now=datetime.now(timezone.utc)
        for tt,price in [('TT-1',.3),('TT-2',.1),('TT-3',1.),('TT-4',.7),('TT-5',.6)]:
            feed.books[tt]=dict(bid=price-.01,ask=price+.01,bid_size=10,ask_size=20,
                received=now.timestamp(),received_at=now.isoformat(),snapshot=False,live_updates=1,group='0')
        return feed

    def test_candidates_are_limited_to_requested_months(self):
        _,_,metadata=selection('CL F27 1MDF',lookup())
        self.assertEqual(set(metadata),{'TT-1','TT-2','TT-3','TT-4','TT-5'})

    def test_component_and_overall_prices_and_expression_change(self):
        feed=self.feed()
        state=feed.snapshot('CL F27 1MDF')
        self.assertAlmostEqual(state['books']['TT-1']['mid'],.3)
        self.assertIsNotNone(state['overall']['bid'])
        self.assertAlmostEqual(state['overall']['mid'],(state['overall']['bid']+state['overall']['ask'])/2)
        self.assertLessEqual(state['overall']['bid_components'],3)
        self.assertFalse(feed.snapshot('CL G27 1MF')['matched'])
        self.assertEqual(feed.snapshot('CL G27 1MF')['books'],{})

    def test_stale_or_disconnected_books_have_no_displayed_prices(self):
        feed=self.feed()
        feed.books['TT-1']['received']-=121
        self.assertIsNone(feed.snapshot('CL F27 1MDF')['books']['TT-1']['bid'])
        feed.groups['0']='DISCONNECTED'
        state=feed.snapshot('CL F27 1MDF')
        self.assertIsNone(state['overall'])
        self.assertTrue(all(row['mid'] is None for row in state['books'].values()))

    def test_sdk_uses_correct_items_and_discards_old_generation(self):
        feed=self.feed()
        feed.accepted.clear()
        feed.books.clear()
        with patch('structure_lab.live_feed.credentials',return_value={'CL':('user','secret')}), \
             patch('lightstreamer.client.LightstreamerClient') as client, \
             patch('lightstreamer.client.Subscription') as subscription:
            feed._connect(feed.generation,feed.metadata)
            self.assertEqual(subscription.call_args.args[1],['1-2','2-2','3-2','4-2','5-2'])
            subscription.return_value.setDataAdapter.assert_called_once_with('MDS')
            listener=subscription.return_value.addListener.call_args.args[0]
            listener.onSubscription()
            self.assertEqual(len(feed.accepted),5)
            update=MagicMock()
            update.getItemName.return_value='1-2'
            update.isSnapshot.return_value=False
            update.getValue.side_effect=lambda field:{'bidPrice':'.2','askPrice':'.3','bidQuantity':'10','askQuantity':'20'}.get(field)
            listener.onItemUpdate(update)
            self.assertAlmostEqual(feed.books['TT-1']['bid'],.2)
            feed.stop()
            update.getValue.side_effect=lambda field:'99'
            listener.onItemUpdate(update)
            self.assertAlmostEqual(feed.books['TT-1']['bid'],.2)
            client.return_value.disconnect.assert_called_once()

    def test_credentials_are_redacted_in_sdk_errors(self):
        feed=self.feed()
        with patch('structure_lab.live_feed.credentials',return_value={'CL':('user','secret')}), \
             patch('lightstreamer.client.LightstreamerClient'),patch('lightstreamer.client.Subscription') as subscription:
            feed._connect(feed.generation,feed.metadata)
            listener=subscription.return_value.addListener.call_args.args[0]
            listener.onSubscriptionError(1,'bad secret')
            self.assertNotIn('secret',' '.join(feed.errors))
            feed.stop()

    def test_dashboard_subscribe_action_and_live_prices_render(self):
        from structure_lab import dashboard
        from structure_lab.interpretation import interpret
        result=interpret('CL F27 1MDF')
        for i,row in enumerate(result['live']['rows'],1):
            row['tt_ids']=['TT-'+str(i)]
        state=self.feed().snapshot('CL F27 1MDF')
        mocked=MagicMock()
        mocked.start.return_value=5
        mocked.snapshot.return_value=state
        with patch.object(dashboard,'feed',mocked),patch.object(dashboard,'interpret',return_value=result):
            browser=dashboard.app.server.test_client()
            response=browser.post('/_dash-update-component',json={
                'output':'action.children','outputs':{'id':'action','property':'children'},
                'inputs':[{'id':'subscribe','property':'n_clicks','value':1},{'id':'stop','property':'n_clicks','value':0}],
                'state':[{'id':'expression','property':'value','value':'CL F27 1MDF'}],
                'changedPropIds':['subscribe.n_clicks']})
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            mocked.start.assert_called_once_with('CL F27 1MDF')
            key=next(k for k in dashboard.app.callback_map if 'breakdowns.children' in k)
            response=browser.post('/_dash-update-component',json={
                'output':key,'outputs':[{'id':'breakdowns','property':'children'},{'id':'message','property':'children'}],
                'inputs':[{'id':'expression','property':'value','value':'CL F27 1MDF'},
                          {'id':'live-tick','property':'n_intervals','value':1}],
                'state':[{'id':'breakdowns','property':'children','value':None},{'id':'expanded-sections','property':'data','value':{}}],'changedPropIds':['live-tick.n_intervals']})
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            text=response.get_data(as_text=True)
            for label in ('TT-1','Complete structure','Best bid','Best ask','Average','0.3'):
                self.assertIn(label,text)
            previous=response.get_json()['response']['breakdowns']['children']
            state['books']['TT-1']['bid']=.31
            response=browser.post('/_dash-update-component',json={
                'output':key,'outputs':[{'id':'breakdowns','property':'children'},{'id':'message','property':'children'}],
                'inputs':[{'id':'expression','property':'value','value':'CL F27 1MDF'},
                          {'id':'live-tick','property':'n_intervals','value':2}],
                'state':[{'id':'breakdowns','property':'children','value':previous},{'id':'expanded-sections','property':'data','value':{}}],
                'changedPropIds':['live-tick.n_intervals']})
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            updates=response.get_json()['response']['breakdowns']['children']['operations']
            self.assertTrue(updates)
            # Only quote text changes; neither details nor its open state is replaced.
            for update in updates:
                self.assertNotIn('open',update['location'])
                self.assertNotIsInstance(update['params']['value'],(dict,list))
            self.assertIn('0.31',str(updates))


if __name__=='__main__':
    unittest.main()
