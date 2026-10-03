"""Private enquiries never match a listing, create trades, or touch funds."""
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text

from app import create_app
from models import db, User, Order, Swap, P2POffer, P2PTrade, P2PTradeMessage
from services.agent_sso import create_sso_grant
from services.agent_workspace import AgentConnection, WorkspaceError, issue_connection
from services.inquiry_schema import ensure_inquiry_schema
from services.listing_inquiries import (
    ListingInquiry, ListingInquiryMessage, ListingInquiryAction,
    start_inquiry, reply_inquiry, set_listing_chat, close_inquiry, mark_inquiry_seen,
    unread_inquiry_count, listing_inquiry_context,
)

AGENT_ID = '11111111-1111-4111-8111-111111111111'


class ListingInquiryTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app.config['AGENT_LISTING_CONVERSATIONS_ENABLED'] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        db.session.add_all([User(id=name, username=name, tier='admin' if name == 'admin' else 'guest')
                            for name in ('alice', 'bob', 'other', 'admin')])
        db.session.flush()
        for number in range(1, 14):
            db.session.add(P2POffer(id=number, creator_id='alice', side='sell', amount_hns=100,
                amount_hns_exact='100', price_btc_per_hns=0, price_quote_per_hns='0.005',
                payment_asset_id='usdc-base', status='open', notes='PUBLIC OFFER'))
        db.session.add(Order(id=23, user_id='alice', side='sell', amount_hns=100,
                             price_btc_per_hns='0.00000001', status='open'))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def human(self, user='bob'):
        client = self.app.test_client()
        with client.session_transaction() as state:
            state['user_id'] = user
            state['auth_method'] = 'guest'
        response = client.get('/inquiries')
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as state:
            csrf = state['inquiry_csrf']
        return client, csrf

    def grant(self, owner='bob', profile='listing-conversations'):
        result = create_sso_grant(owner, 'Inquiry bot', AGENT_ID,
                                  profile=profile, reviewed_username=True)
        db.session.commit()
        return result

    def headers(self, connection, key='key'):
        return {'Authorization': 'Bearer gfavip-session-' + 'a' * 32,
                'X-Liquidity-Connection': str(connection.id), 'Idempotency-Key': key}

    def start(self, user='bob', kind='p2p', listing_id=1, key='initial', connection=None):
        result, created = start_inquiry(kind, listing_id, user, key, {'message': 'PRIVATE QUESTION'}, connection)
        db.session.commit()
        self.assertTrue(created)
        return result['inquiry']['id']

    def denied(self, status, function, *args, **kwargs):
        with self.assertRaises(WorkspaceError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)
        db.session.rollback()

    def test_defaults_and_additive_migration_preserve_opt_out(self):
        self.assertTrue(db.session.get(Order, 23).allow_pretrade_chat)
        self.assertTrue(db.session.get(P2POffer, 1).allow_pretrade_chat)
        engine = create_engine('sqlite://')
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)'))
            conn.execute(text("INSERT INTO orders VALUES (23, 'open')"))
            conn.execute(text('CREATE TABLE p2p_offers (id INTEGER PRIMARY KEY, allow_pretrade_chat BOOLEAN)'))
            conn.execute(text('INSERT INTO p2p_offers VALUES (1, FALSE), (2, NULL)'))
        ensure_inquiry_schema(engine)
        ensure_inquiry_schema(engine)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT allow_pretrade_chat FROM orders')).scalar(), 1)
            self.assertEqual(conn.execute(text('SELECT allow_pretrade_chat FROM p2p_offers ORDER BY id')).scalars().all(), [0, 1])
        engine.dispose()

    def test_get_human_pages_never_create_conversation_or_mark_read(self):
        client, csrf = self.human()
        for path in ('/listings/atomic/23/inquire', '/listings/p2p/1/inquire', '/inquiries'):
            self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(ListingInquiry.query.count(), 0)
        inquiry_id = self.start(user='other')
        owner, _ = self.human('alice')
        self.assertEqual(unread_inquiry_count('alice'), 1)
        for _ in range(2):
            self.assertEqual(owner.get(f'/inquiries/{inquiry_id}').status_code, 200)
        self.assertEqual(unread_inquiry_count('alice'), 1)
        self.assertEqual(ListingInquiry.query.one().owner_seen_message_id, 0)

    def test_logged_in_listing_pages_with_csrf_are_not_cacheable(self):
        anonymous = self.app.test_client()
        for user in ('alice', 'bob'):
            client, _ = self.human(user)
            for path in ('/p2p/offers/1', '/orders/23'):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store, private')
                self.assertEqual(response.headers['Pragma'], 'no-cache')
        for path in ('/p2p/offers/1', '/orders/23'):
            response = anonymous.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('no-store', response.headers.get('Cache-Control', ''))

    def test_signed_out_is_not_created_and_human_bearers_cannot_fallback(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get('/listings/atomic/23/inquire').status_code, 302)
        self.assertEqual(anon.post('/listings/atomic/23/inquire', data={'message': 'no'}).status_code, 302)
        client, csrf = self.human()
        for path in ('/inquiries', '/listings/atomic/23/inquire'):
            response = client.get(path, headers={'Authorization': 'Bearer arbitrary-credential'})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(User.query.count(), 4)
        self.assertEqual(ListingInquiry.query.count(), 0)
        with self.app.test_request_context('/'):
            self.assertTrue(listing_inquiry_context('atomic', db.session.get(Order, 23), None)['can_start'])

    def test_human_csrf_idempotency_and_xss_boundary(self):
        client, csrf = self.human()
        endpoint = '/listings/atomic/23/inquire'
        payload = {'csrf_token': csrf, 'idempotency_key': 'human-key', 'message': '<script>PRIVATE EVIL</script>'}
        self.assertEqual(client.post(endpoint, data={**payload, 'csrf_token': 'bad'}).status_code, 400)
        self.assertEqual(client.post(endpoint, data={**payload, 'status': 'matched'}).status_code, 400)
        response = client.post(endpoint, data=payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.post(endpoint, data=payload).status_code, 302)
        self.assertEqual(ListingInquiry.query.count(), 1)
        self.assertEqual(ListingInquiryMessage.query.count(), 1)
        rendered = client.get(response.location).get_data(as_text=True)
        self.assertIn('&lt;script&gt;PRIVATE EVIL&lt;/script&gt;', rendered)
        self.assertNotIn('<script>PRIVATE EVIL</script>', rendered)
        self.assertEqual(db.session.get(Order, 23).status, 'open')
        self.assertEqual(Swap.query.count(), 0)

    def test_inquirers_are_isolated_admin_has_no_bypass(self):
        first = self.start()
        second = self.start(user='other')
        for user, forbidden in [('bob', second), ('other', first), ('admin', first)]:
            client, csrf = self.human(user)
            self.assertEqual(client.get(f'/inquiries/{forbidden}').status_code, 404)
            self.assertEqual(client.post(f'/inquiries/{forbidden}', data={'csrf_token': csrf,
                'idempotency_key': 'forbidden', 'message': 'spy'}).status_code, 404)
            self.assertEqual(client.post(f'/inquiries/{forbidden}/close', data={'csrf_token': csrf}).status_code, 404)
        owner, _ = self.human('alice')
        self.assertEqual(owner.get(f'/inquiries/{first}').status_code, 200)
        self.assertEqual(owner.get(f'/inquiries/{second}').status_code, 200)

    def test_closed_opted_out_and_matched_block_writes_not_history_or_replay(self):
        inquiry_id = self.start()
        for mutate in ('off', 'matched', 'closed'):
            if mutate == 'off':
                set_listing_chat('p2p', 1, 'alice', False)
            elif mutate == 'matched':
                set_listing_chat('p2p', 1, 'alice', True)
                db.session.get(P2POffer, 1).status = 'matched'
            else:
                db.session.get(P2POffer, 1).status = 'open'
                close_inquiry(inquiry_id, 'bob')
            db.session.commit()
            self.denied(409, reply_inquiry, inquiry_id, 'alice', 'blocked', {'message': 'no'})
            self.denied(409, start_inquiry, 'p2p', 1, 'bob', 'new-key', {'message': 'no'})
            original, created = start_inquiry('p2p', 1, 'bob', 'initial', {'message': 'PRIVATE QUESTION'})
            self.assertFalse(created)
            self.assertEqual(original['inquiry']['id'], inquiry_id)
            db.session.rollback()
            client, _ = self.human()
            self.assertEqual(client.get(f'/inquiries/{inquiry_id}').status_code, 200)
        self.assertEqual(P2PTrade.query.count(), 0)
        self.assertEqual(P2PTradeMessage.query.count(), 0)
        self.assertEqual(ListingInquiryMessage.query.count(), 1)

    def test_toggle_owner_only_and_explicit_seen_does_not_hide_future_messages(self):
        inquiry_id = self.start()
        self.denied(403, set_listing_chat, 'p2p', 1, 'bob', False)
        self.denied(403, set_listing_chat, 'p2p', 1, 'admin', False)
        self.denied(403, start_inquiry, 'p2p', 1, 'alice', 'self', {'message': 'myself'})
        first = ListingInquiryMessage.query.one().id
        mark_inquiry_seen(inquiry_id, 'alice', first)
        db.session.commit()
        self.assertEqual(unread_inquiry_count('alice'), 0)
        reply_inquiry(inquiry_id, 'bob', 'next', {'message': 'new'})
        db.session.commit()
        self.assertEqual(unread_inquiry_count('alice'), 1)
        self.denied(404, mark_inquiry_seen, inquiry_id, 'alice', 9999)
        mark_inquiry_seen(inquiry_id, 'alice', first)
        db.session.commit()
        self.assertEqual(unread_inquiry_count('alice'), 1)

    def test_message_payload_key_and_numeric_ids_are_strict(self):
        for message in ('', ' ', 'x' * 1001, '\x00bad', '\x1bbad', 123):
            self.denied(400, start_inquiry, 'p2p', 1, 'bob', 'bad', {'message': message})
        self.denied(400, start_inquiry, 'p2p', 1, 'bob', 'bad', {'message': 'hey', 'accept': True})
        self.denied(400, start_inquiry, 'p2p', 1, 'bob', 'bad key', {'message': 'hey'})
        self.denied(404, start_inquiry, 'p2p', 9223372036854775808, 'bob', 'large', {'message': 'hey'})
        self.denied(404, start_inquiry, 'unknown', 1, 'bob', 'unknown', {'message': 'hey'})
        self.start()
        self.denied(409, start_inquiry, 'p2p', 2, 'bob', 'initial', {'message': 'PRIVATE QUESTION'})

    def test_new_conversation_and_owner_message_quota(self):
        for number in range(1, 11):
            self.start(listing_id=number, key=f'open{number}')
        self.denied(429, start_inquiry, 'p2p', 11, 'bob', 'eleven', {'message': 'limit'})
        inquiry = ListingInquiry.query.first()
        for number in range(30):
            reply_inquiry(inquiry.id, 'bob', f'reply{number}', {'message': 'bounded'})
            db.session.commit()
        self.denied(429, reply_inquiry, inquiry.id, 'bob', 'fortyone', {'message': 'limit'})
        self.assertEqual(ListingInquiryAction.query.count(), 40)

    def test_agent_expiry_revocation_scope_binding_flag_and_lifetime_caps(self):
        connection = self.grant()
        inquiry_id = self.start(connection=connection)
        self.assertEqual(ListingInquiryMessage.query.one().actor, 'agent')
        self.assertTrue(ListingInquiryMessage.query.one().content.startswith(f'[AI agent connection #{connection.id}]'))
        for number in range(1, 200):
            ListingInquiryAction.query.update({'created_at': datetime.utcnow() - timedelta(days=2)})
            db.session.commit()
            reply_inquiry(inquiry_id, 'bob', f'life{number}', {'message': 'bounded'}, connection)
            db.session.commit()
        self.denied(429, reply_inquiry, inquiry_id, 'bob', 'overlife', {'message': 'limit'}, connection)
        for field in ('revoked_at', 'expires_at'):
            connection.revoked_at = None
            connection.expires_at = datetime.utcnow() + timedelta(days=1)
            setattr(connection, field, datetime.utcnow() - timedelta(seconds=1))
            db.session.commit()
            self.denied(401, reply_inquiry, inquiry_id, 'bob', 'revoked', {'message': 'limit'}, connection)
        connection.expires_at = datetime.utcnow() + timedelta(days=1)
        db.session.commit()
        self.app.config['AGENT_LISTING_CONVERSATIONS_ENABLED'] = False
        self.denied(403, reply_inquiry, inquiry_id, 'bob', 'flag', {'message': 'limit'}, connection)

    def test_api_identity_only_right_profile_private_pages_and_untrusted_read(self):
        connection = self.grant()
        old = self.grant(owner='other', profile='trade-assistant')
        client = self.app.test_client()
        with patch('services.agent_sso.validate_agent_identity', return_value={'gfavip_user_id': AGENT_ID}):
            for path in ('/listings', '/inquiries'):
                self.assertEqual(client.get('/api/agent/v1' + path, headers=self.headers(old)).status_code, 403)
            book = client.get('/api/agent/v1/listings', headers=self.headers(connection))
            self.assertEqual(book.status_code, 200)
            self.assertEqual(set(book.get_json()['books']), {'p2p', 'atomic'})
            self.assertEqual(book.get_json()['books']['atomic']['listings'][0]['id'], 23)
            self.assertNotIn('PRIVATE', book.get_data(as_text=True))
            created = client.post('/api/agent/v1/listings/atomic/23/inquiries', json={'message': 'PRIVATE AGENT'}, headers=self.headers(connection))
            self.assertEqual(created.status_code, 201)
            inquiry_id = created.get_json()['inquiry']['id']
            self.assertEqual(client.post('/api/agent/v1/listings/atomic/23/inquiries', json={'message': 'PRIVATE AGENT'}, headers=self.headers(connection)).status_code, 200)
            detail = client.get(f'/api/agent/v1/inquiries/{inquiry_id}', headers=self.headers(connection))
            self.assertEqual(detail.get_json()['messages'][0]['actor'], 'agent')
            self.assertIn('untrusted', detail.get_json()['messages'][0]['trust'].lower())
            self.assertIn('no-store', detail.headers['Cache-Control'])
            self.assertNotIn('Access-Control-Allow-Origin', detail.headers)
            for endpoint in ('/p2p/offers', '/orders/23/accept', f'/inquiries/{inquiry_id}'):
                self.assertEqual(client.post(endpoint, headers=self.headers(connection)).status_code, 403)
            for suffix in ('?owner_id=alice', '?after=-1', '?limit=101', '?after=0&after=1'):
                self.assertEqual(client.get('/api/agent/v1/inquiries' + suffix, headers=self.headers(connection)).status_code, 400)
            self.assertEqual(client.post('/api/agent/v1/listings/p2p/1/inquiries', data='{"message":"a","message":"b"}', content_type='application/json', headers=self.headers(connection)).status_code, 400)
            self.assertEqual(client.post('/api/agent/v1/listings/p2p/1/inquiries', data='x' * 8193, headers=self.headers(connection)).status_code, 413)
            self.assertEqual(client.get('/api/agent/v1/inquiries').status_code, 401)
        self.assertEqual((Swap.query.count(), P2PTrade.query.count(), P2PTradeMessage.query.count()), (0, 0, 0))
        self.assertTrue(all(user.gems_balance == 0 for user in User.query.all()))

    def test_old_service_grants_cannot_send_and_changes_rollback_atomically(self):
        old = self.grant(profile='trade-assistant')
        self.denied(403, start_inquiry, 'p2p', 1, 'bob', 'old', {'message': 'no'}, old)
        result, _ = start_inquiry('p2p', 1, 'bob', 'rollback', {'message': 'private'})
        db.session.rollback()
        self.assertEqual(ListingInquiry.query.count(), 0)
        self.assertEqual(ListingInquiryMessage.query.count(), 0)
        self.assertEqual(ListingInquiryAction.query.count(), 0)
        self.start(key='rollback')


if __name__ == '__main__':
    unittest.main()
