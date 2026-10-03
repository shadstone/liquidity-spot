"""Read-only agent access never grants settlement authority or marks a room read."""
from datetime import datetime, timedelta
import json
import unittest

from app import create_app
from models import db, User, P2POffer, P2PTrade, P2PTradeMessage, P2PTradeParticipantState
from services.agent_workspace import AgentConnection, AgentDraft, WorkspaceError, create_draft, issue_connection
from services.payment_assets import make_terms_snapshot
from services.trade_events import AgentTradeEvent


class AgentTradeAccessTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.machine = self.app.test_client()
        self.human = self.app.test_client()
        self.seen = datetime(2026, 1, 1)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([User(id=name, username='PRIVATE-' + name, email=name + '@example.invalid')
                                for name in ('alice', 'bob', 'other', 'fourth')])
            db.session.flush()
            for offer_id, creator, taker, side in [(1, 'alice', 'bob', 'sell'), (2, 'other', 'fourth', 'sell'), (3, 'alice', 'bob', 'buy')]:
                offer = P2POffer(id=offer_id, creator_id=creator, side=side, amount_hns=1000,
                    amount_hns_exact='1000', price_btc_per_hns=0, payment_asset_id='usdc-base',
                    price_quote_per_hns='0.0035', payment_method='PRIVATE-PAYMENT-METHOD', status='matched')
                db.session.add(offer)
                db.session.flush()
                db.session.add(P2PTrade(id=offer_id, offer_id=offer_id, creator_id=creator,
                    counterparty_id=taker, status='matched', milestone='payment_sent',
                    terms_snapshot=make_terms_snapshot(offer), latest_note='PRIVATE-LATEST-NOTE',
                    admin_notes='PRIVATE-ADMIN-NOTES', alice_lock_txid='a' * 64,
                    bob_lock_txid='0x' + 'b' * 64))
            db.session.flush()
            db.session.add(P2PTradeParticipantState(trade_id=1, user_id='alice', last_viewed_at=self.seen))
            db.session.add_all([P2PTradeMessage(id=1, trade_id=1, user_id='bob', message='PRIVATE-CHAT-ONE'),
                                P2PTradeMessage(id=2, trade_id=2, user_id='other', message='OUTSIDER-CHAT'),
                                P2PTradeMessage(id=3, trade_id=1, user_id='alice', message='PRIVATE-CHAT-TWO')])
            db.session.commit()
        with self.human.session_transaction() as state:
            state['user_id'] = 'alice'

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def token(self, owner='alice', profile='trade-assistant', messages=False):
        with self.app.app_context():
            connection, token = issue_connection(owner, 'test connection', profile, messages)
            db.session.commit()
            return token, connection.id

    def get(self, path, token, client=None):
        return (client or self.machine).get(path, headers={'Authorization': 'Bearer ' + token})

    def seed_events(self):
        with self.app.app_context():
            for event_id, owner, trade_id in [(2, 'alice', 1), (4, 'other', 2), (7, 'alice', 3), (100, 'other', 2)]:
                db.session.add(AgentTradeEvent(id=event_id, owner_id=owner, trade_id=trade_id,
                    kind='p2p.message_added', actor_user_id='bob',
                    payload={'status': 'matched', 'milestone': 'payment_sent', 'message_id': 1,
                             'notes': 'SECRET-UNSAFE-EXTRA', 'txid': 'SECRET-TXID'}))
            db.session.commit()

    def test_old_draft_tokens_are_not_widened_and_trade_tokens_cannot_create_drafts(self):
        old, old_id = self.token(profile='offer-drafts')
        reader, _ = self.token()
        for path in ('/events', '/trades', '/trades/1', '/trades/1/messages'):
            self.assertEqual(self.get('/api/agent/v1' + path, old).status_code, 403)
        self.assertEqual(self.get('/api/agent/v1/drafts', reader).status_code, 403)
        response = self.machine.post('/api/agent/v1/drafts', json={'side': 'sell'},
            headers={'Authorization': 'Bearer ' + reader, 'Idempotency-Key': 'no-drafts'})
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertEqual(db.session.get(AgentConnection, old_id).scope, 'drafts:read drafts:write')
            self.assertEqual(AgentDraft.query.count(), 0)

    def test_new_human_connections_default_read_only_and_require_message_consent(self):
        self.human.get('/agents')
        with self.human.session_transaction() as state:
            csrf = state['agent_workspace_csrf']
        response = self.human.post('/agents/connections', data={'label': 'default', 'csrf_token': csrf})
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertEqual(AgentConnection.query.one().scope, 'events:read trades:read')
        with self.human.session_transaction() as state:
            csrf = state['agent_workspace_csrf']
        for choices in ({'profile': 'offer-drafts', 'include_messages': 'yes'}, {'profile': 'unknown'},
                        {'profile': 'trade-assistant', 'include_messages': 'true'}):
            self.assertEqual(self.human.post('/agents/connections', data={
                'label': 'bad', 'csrf_token': csrf, **choices}).status_code, 400)
        response = self.human.post('/agents/connections', data={'label': 'reader', 'csrf_token': csrf,
                                    'profile': 'trade-assistant', 'include_messages': 'yes'})
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        self.assertNotIn('<script', response.get_data(as_text=True).lower())
        with self.app.app_context():
            self.assertEqual(AgentConnection.query.filter_by(label='reader').one().scope,
                             'events:read trades:read trade_messages:read')

    def test_trade_summary_is_owner_scoped_frozen_and_has_no_private_notes(self):
        reader, _ = self.token()
        with self.app.app_context():
            offer = db.session.get(P2POffer, 1)
            offer.amount_hns_exact = '99'
            offer.price_quote_per_hns = '9'
            offer.payment_asset_id = 'eth-ethereum'
            db.session.commit()
        response = self.get('/api/agent/v1/trades/1', reader)
        self.assertEqual(response.status_code, 200)
        trade = response.get_json()['trade']
        self.assertEqual(trade['terms']['amount_hns'], '1000')
        self.assertEqual(trade['terms']['total'], '3.5')
        self.assertEqual(trade['terms']['payment_asset']['id'], 'usdc-base')
        self.assertEqual(trade['send']['asset'], 'HNS')
        self.assertEqual(trade['receive']['asset'], 'USDC')
        self.assertEqual(trade['role'], 'HNS seller')
        self.assertFalse(trade['chain_verified'])
        self.assertIn('Participant-reported', trade['reporting'])
        self.assertEqual(trade['reported_transaction_ids']['payment'], '0x' + 'b' * 64)
        self.assertEqual(self.get('/api/agent/v1/trades/3', reader).get_json()['trade']['role'], 'HNS buyer')
        for text in ('PRIVATE-CHAT', 'PRIVATE-LATEST', 'PRIVATE-ADMIN', 'PRIVATE-PAYMENT', '@example.invalid', 'PRIVATE-alice'):
            self.assertNotIn(text, response.get_data(as_text=True))
        self.assertEqual(self.get('/api/agent/v1/trades/2', reader).status_code, 404)
        self.assertEqual(self.get('/api/agent/v1/trades/999', reader).status_code, 404)

    def test_trade_fields_do_not_leak_arbitrary_text_in_legacy_status_or_txid(self):
        reader, _ = self.token()
        with self.app.app_context():
            trade = db.session.get(P2PTrade, 1)
            trade.status = 'UNTRUSTED-STATUS'
            trade.milestone = 'UNTRUSTED-MILESTONE'
            trade.alice_lock_txid = 'SECRET-MISTYPED-IN-TXID'
            db.session.commit()
        response = self.get('/api/agent/v1/trades/1', reader)
        trade = response.get_json()['trade']
        self.assertEqual(trade['status'], 'unknown')
        self.assertEqual(trade['milestone'], 'unknown')
        self.assertTrue(trade['unrecognized_transaction_ids_present'])
        self.assertIsNone(trade['reported_transaction_ids']['hns'])
        self.assertNotIn('SECRET-', response.get_data(as_text=True))
        self.assertNotIn('UNTRUSTED-', response.get_data(as_text=True))

    def test_message_access_needs_scope_and_explicit_detail_request(self):
        reader, _ = self.token()
        chatter, _ = self.token(messages=True)
        self.assertEqual(self.get('/api/agent/v1/trades/1?include_messages=yes', reader).status_code, 403)
        self.assertEqual(self.get('/api/agent/v1/trades/1/messages', reader).status_code, 403)
        self.assertNotIn('PRIVATE-CHAT', self.get('/api/agent/v1/trades/1', chatter).get_data(as_text=True))
        detail = self.get('/api/agent/v1/trades/1?include_messages=yes&limit=1', chatter).get_json()
        self.assertEqual(detail['message_page']['messages'][0]['id'], 1)
        self.assertTrue(detail['message_page']['has_more'])
        page = self.get('/api/agent/v1/trades/1/messages?after=1&limit=1', chatter).get_json()
        self.assertEqual([message['id'] for message in page['messages']], [3])
        self.assertEqual(page['next_cursor'], 3)
        self.assertIn('untrusted', page['messages'][0]['trust'])
        self.assertNotIn('OUTSIDER', json.dumps(page))
        self.assertEqual(self.get('/api/agent/v1/trades/2/messages', chatter).status_code, 404)

    def test_chat_context_is_bounded_and_empty_cursor_is_preserved(self):
        chatter, _ = self.token(messages=True)
        with self.app.app_context():
            db.session.get(P2PTradeMessage, 1).message = 'x' * 5000
            db.session.commit()
        page = self.get('/api/agent/v1/trades/1/messages?limit=1', chatter).get_json()
        self.assertEqual(len(page['messages'][0]['content']), 4000)
        self.assertTrue(page['messages'][0]['truncated'])
        empty = self.get('/api/agent/v1/trades/1/messages?after=3', chatter).get_json()
        self.assertEqual(empty['messages'], [])
        self.assertEqual(empty['next_cursor'], 3)

    def test_events_cursor_only_advances_through_returned_owner_events(self):
        self.seed_events()
        reader, _ = self.token()
        second, _ = self.token()
        outsider, _ = self.token(owner='other')
        first = self.get('/api/agent/v1/events?after=0&limit=1', reader).get_json()
        self.assertEqual([event['id'] for event in first['events']], [2])
        self.assertEqual(first['next_cursor'], 2)
        self.assertTrue(first['has_more'])
        next_page = self.get('/api/agent/v1/events?after=2&limit=1', reader).get_json()
        self.assertEqual([event['id'] for event in next_page['events']], [7])
        self.assertEqual(next_page['next_cursor'], 7)
        self.assertFalse(next_page['has_more'])
        empty = self.get('/api/agent/v1/events?after=7', reader).get_json()
        self.assertEqual(empty['next_cursor'], 7)
        self.assertEqual(empty['events'], [])
        self.assertEqual(first['stream_id'], self.get('/api/agent/v1/events', second).get_json()['stream_id'])
        self.assertNotEqual(first['stream_id'], self.get('/api/agent/v1/events', outsider).get_json()['stream_id'])
        self.assertNotIn('SECRET-', json.dumps(first))

    def test_event_owner_tag_alone_never_grants_access_to_outsider_room(self):
        reader, _ = self.token()
        with self.app.app_context():
            db.session.add(AgentTradeEvent(owner_id='alice', trade_id=2, kind='p2p.offer_accepted',
                                          actor_user_id='other', payload={}))
            db.session.commit()
        page = self.get('/api/agent/v1/events', reader).get_json()
        self.assertEqual(page['events'], [])
        self.assertEqual(page['next_cursor'], 0)

    def test_service_defense_checks_draft_scope_after_owner_lock(self):
        _, connection_id = self.token()
        with self.app.app_context():
            reader = db.session.get(AgentConnection, connection_id)
            with self.assertRaises(WorkspaceError) as caught:
                create_draft(reader, 'forbidden-service-call', {'side': 'sell', 'payment_asset': 'usdc-base',
                                                             'amount_hns': '1', 'price': '1'})
            self.assertEqual(caught.exception.status, 403)
            db.session.rollback()
            self.assertEqual(AgentDraft.query.count(), 0)

    def test_bootstrap_includes_legacy_rooms_without_fabricated_events(self):
        reader, _ = self.token()
        first = self.get('/api/agent/v1/trades?limit=1', reader).get_json()
        self.assertEqual([trade['id'] for trade in first['trades']], [1])
        self.assertEqual(first['next_cursor'], 1)
        self.assertTrue(first['has_more'])
        last = self.get('/api/agent/v1/trades?after=1', reader).get_json()
        self.assertEqual([trade['id'] for trade in last['trades']], [3])
        self.assertEqual(last['next_cursor'], 3)
        self.assertEqual(self.get('/api/agent/v1/events', reader).get_json()['events'], [])

    def test_read_requests_do_not_mark_room_read_or_change_trade_state(self):
        reader, connection_id = self.token(messages=True)
        self.seed_events()
        with self.app.app_context():
            initial = [(t.id, t.status, t.milestone, t.updated_at, t.latest_note) for t in P2PTrade.query.all()]
            counts = (P2POffer.query.count(), P2PTrade.query.count(), P2PTradeMessage.query.count(), AgentTradeEvent.query.count())
        for path in ('/events', '/trades', '/trades/1', '/trades/1?include_messages=yes', '/trades/1/messages'):
            self.assertEqual(self.get('/api/agent/v1' + path, reader).status_code, 200)
        with self.app.app_context():
            self.assertEqual([(t.id, t.status, t.milestone, t.updated_at, t.latest_note) for t in P2PTrade.query.all()], initial)
            self.assertEqual((P2POffer.query.count(), P2PTrade.query.count(), P2PTradeMessage.query.count(), AgentTradeEvent.query.count()), counts)
            self.assertEqual(P2PTradeParticipantState.query.one().last_viewed_at, self.seen)
            self.assertIsNotNone(db.session.get(AgentConnection, connection_id).last_used_at)

    def test_expiry_revocation_and_cookie_identity_cannot_bypass_scopes(self):
        reader, connection_id = self.token()
        for attr, value in [('revoked_at', datetime.utcnow()), ('expires_at', datetime.utcnow() - timedelta(seconds=1))]:
            with self.app.app_context():
                connection = db.session.get(AgentConnection, connection_id)
                connection.revoked_at = None
                setattr(connection, attr, value)
                db.session.commit()
            for path in ('/events', '/trades', '/trades/1', '/trades/1/messages'):
                self.assertEqual(self.get('/api/agent/v1' + path, reader).status_code, 401)
        self.assertEqual(self.human.get('/api/agent/v1/trades/1').status_code, 401)
        outsider, _ = self.token(owner='other')
        self.assertEqual(self.get('/api/agent/v1/trades/1', outsider, self.human).status_code, 404)

    def test_pagination_body_and_method_limits(self):
        reader, _ = self.token(messages=True)
        for query in ('limit=0', 'limit=101', 'limit=-1', 'after=-1', 'after=1e3', 'after=9223372036854775808',
                      'after=0&after=1', 'limit=2&limit=3', 'owner_id=other'):
            with self.subTest(query=query):
                self.assertEqual(self.get('/api/agent/v1/events?' + query, reader).status_code, 400)
        self.assertEqual(self.get('/api/agent/v1/trades/1?include_messages=true', reader).status_code, 400)
        response = self.machine.get('/api/agent/v1/events', data='x' * 8193, headers={'Authorization': 'Bearer ' + reader})
        self.assertEqual(response.status_code, 413)
        for path in ('/events', '/trades', '/trades/1', '/trades/1/messages'):
            # Reply POST exists only for a separately approved bounded maker.
            # A reader is denied even when the route exists.
            expected = 403 if path.endswith('/messages') else 405
            self.assertEqual(self.machine.post('/api/agent/v1' + path, headers={'Authorization': 'Bearer ' + reader}).status_code, expected)
        response = self.get('/api/agent/v1/events', reader)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)


if __name__ == '__main__':
    unittest.main()
