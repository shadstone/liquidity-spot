import hashlib
from datetime import datetime, timedelta
import re
import unittest

from app import create_app
from models import db, User, P2POffer, P2PTrade, Order, Swap
from services.agent_workspace import AgentConnection, AgentDraft, validate_draft


class AgentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.owner = self.app.test_client()
        self.other = self.app.test_client()
        self.machine = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            db.session.add_all([User(id='owner', username='Owner', tier='free'),
                                User(id='other', username='Other', tier='free')])
            db.session.commit()
        for client, user_id in [(self.owner, 'owner'), (self.other, 'other')]:
            with client.session_transaction() as state:
                state['user_id'] = user_id
        self.assertFalse(self.app.config['ALLOW_EXTERNAL_HTTP'])

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def csrf(self, client=None):
        client = client or self.owner
        response = client.get('/agents')
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as state:
            return state['agent_workspace_csrf']

    def issue(self, client=None, label='My agent'):
        client = client or self.owner
        response = client.post('/agents/connections', data={'csrf_token': self.csrf(client), 'label': label,
                                                          'profile': 'offer-drafts'})
        self.assertEqual(response.status_code, 200)
        token = re.search(r'ls_agent_[A-Za-z0-9_-]{43}', response.get_data(as_text=True)).group()
        with self.app.app_context():
            connection_id = AgentConnection.query.filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).one().id
        return token, connection_id, response

    def payload(self, **overrides):
        return {'side': 'sell', 'payment_asset': 'usdc-base', 'amount_hns': '1000',
                'price': '0.0035', 'notes': 'Draft for human review', **overrides}

    def submit(self, token, key='draft-1', payload=None, client=None):
        return (client or self.machine).post('/api/agent/v1/drafts', json=payload or self.payload(),
                    headers={'Authorization': f'Bearer {token}', 'Idempotency-Key': key})

    def api_list(self, token, client=None):
        return (client or self.machine).get('/api/agent/v1/drafts', headers={'Authorization': f'Bearer {token}'})

    def test_capabilities_public_and_cors_is_not_wildcard(self):
        response = self.machine.get('/api/agent/v1/capabilities', headers={'Origin': 'https://example.invalid'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['mode'], 'human-controlled')
        self.assertEqual(body['draft_mode'], 'draft-only')
        self.assertEqual(len(body['payment_assets']), 8)
        self.assertFalse(body['draft_schema']['additionalProperties'])
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.machine.get('/api/channel').headers['Access-Control-Allow-Origin'], '*')

    def test_api_never_uses_browser_session_or_query_token(self):
        token, _, _ = self.issue()
        self.assertEqual(self.owner.get('/api/agent/v1/drafts').status_code, 401)
        self.assertEqual(self.owner.post('/api/agent/v1/drafts', json=self.payload()).status_code, 401)
        self.assertEqual(self.machine.get('/api/agent/v1/drafts', query_string={'token': token}).status_code, 401)
        self.assertEqual(self.machine.get('/agents').status_code, 302)
        self.assertEqual(self.machine.post('/agents/connections', data={'label': 'no session'}).status_code, 302)

    def test_token_only_shown_once_and_only_hash_stored(self):
        token, connection_id, response = self.issue()
        self.assertEqual(response.headers['Cache-Control'], 'no-store, private')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        self.assertNotIn('<script', response.get_data(as_text=True).lower())
        self.assertNotIn('cdn.tailwindcss.com', response.get_data(as_text=True))
        self.assertNotIn(token, self.owner.get('/agents').get_data(as_text=True))
        self.assertNotIn(token, response.headers.get('Set-Cookie', ''))
        with self.owner.session_transaction() as state:
            self.assertNotIn(token, str(dict(state)))
        with self.app.app_context():
            connection = db.session.get(AgentConnection, connection_id)
            self.assertEqual(connection.token_hash, hashlib.sha256(token.encode()).hexdigest())
            self.assertEqual(connection.scope, 'drafts:read drafts:write')
            self.assertEqual(connection.expires_at - connection.created_at, timedelta(days=7))
            self.assertNotIn(token, str(connection.__dict__))

    def test_csrf_required_for_all_human_writes(self):
        self.csrf()
        self.assertEqual(self.owner.post('/agents/connections', data={'label': 'missing csrf'}).status_code, 400)
        self.assertEqual(self.owner.post('/agents/connections', data={'label': 'bad csrf', 'csrf_token': 'wrong'}).status_code, 400)
        token, connection_id, _ = self.issue()
        draft_id = self.submit(token).get_json()['draft']['id']
        self.assertEqual(self.owner.post(f'/agents/connections/{connection_id}/revoke').status_code, 400)
        self.assertEqual(self.owner.post(f'/agents/drafts/{draft_id}/dismiss').status_code, 400)
        with self.app.app_context():
            self.assertEqual(AgentConnection.query.count(), 1)
            self.assertIsNone(db.session.get(AgentConnection, connection_id).revoked_at)
            self.assertEqual(db.session.get(AgentDraft, draft_id).status, 'pending')

    def test_owner_isolation_in_api_and_human_management(self):
        token_a, connection_a, _ = self.issue()
        token_b, _, _ = self.issue(self.other)
        draft_a = self.submit(token_a, payload=self.payload(notes='owner-only')).get_json()['draft']['id']
        draft_b = self.submit(token_b, payload=self.payload(notes='other-only')).get_json()['draft']['id']
        self.assertEqual([d['id'] for d in self.api_list(token_a, self.other).get_json()['drafts']], [draft_a])
        self.assertEqual([d['id'] for d in self.api_list(token_b, self.owner).get_json()['drafts']], [draft_b])
        self.assertNotIn('owner-only', self.other.get('/agents').get_data(as_text=True))
        csrf = self.csrf(self.other)
        self.assertEqual(self.other.post(f'/agents/connections/{connection_a}/revoke', data={'csrf_token': csrf}).status_code, 404)
        self.assertEqual(self.other.post(f'/agents/drafts/{draft_a}/dismiss', data={'csrf_token': csrf}).status_code, 404)

    def test_revoked_expired_and_invalid_credentials_fail(self):
        token, connection_id, _ = self.issue()
        self.owner.post(f'/agents/connections/{connection_id}/revoke', data={'csrf_token': self.csrf()})
        self.assertEqual(self.api_list(token).status_code, 401)
        self.assertEqual(self.submit(token).status_code, 401)
        expired, expired_id, _ = self.issue(label='expires')
        with self.app.app_context():
            db.session.get(AgentConnection, expired_id).expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()
        self.assertEqual(self.api_list(expired).status_code, 401)
        self.assertEqual(self.submit(expired).status_code, 401)
        self.assertEqual(self.api_list('ls_agent_' + 'a' * 43).status_code, 401)

    def test_strict_validation_rejects_unknown_fields_and_bad_precision(self):
        token, _, _ = self.issue()
        invalid = [self.payload(amount_hns=1000), self.payload(price=0.0035), self.payload(price=True),
                   self.payload(amount_hns='1.0000001'), self.payload(price='0.0000000000000000001'),
                   self.payload(price='NaN'), self.payload(price='1e-5'), self.payload(amount_hns='-1'),
                   self.payload(payment_asset='usdt-base'), self.payload(side='withdraw'),
                   self.payload(notes='x' * 1001), self.payload(notes={'publish': True}),
                   self.payload(publish=True), self.payload(owner_id='other')]
        for index, payload in enumerate(invalid):
            with self.subTest(index=index):
                self.assertEqual(self.submit(token, f'invalid-{index}', payload).status_code, 400)
        with self.app.app_context():
            self.assertEqual(AgentDraft.query.count(), 0)

    def test_body_limit_invalid_json_and_missing_idempotency_key(self):
        token, _, _ = self.issue()
        headers = {'Authorization': f'Bearer {token}', 'Idempotency-Key': 'body-test'}
        self.assertEqual(self.machine.post('/api/agent/v1/drafts', data='x' * 8193, content_type='application/json', headers=headers).status_code, 413)
        self.assertEqual(self.machine.post('/api/agent/v1/drafts', data='{', content_type='application/json', headers=headers).status_code, 400)
        self.assertEqual(self.machine.post('/api/agent/v1/drafts', data='{"side":"buy","side":"sell"}', content_type='application/json', headers=headers).status_code, 400)
        self.assertEqual(self.machine.post('/api/agent/v1/drafts', data='side=sell', headers=headers).status_code, 415)
        self.assertEqual(self.machine.post('/api/agent/v1/drafts', json=self.payload(), headers={'Authorization': f'Bearer {token}'}).status_code, 400)
        self.assertEqual(self.submit(token, 'x' * 129).status_code, 400)

    def test_idempotency_is_bound_to_payload_and_keeps_audit(self):
        token, connection_id, _ = self.issue()
        first = self.submit(token)
        replay = self.submit(token)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.get_json()['draft']['id'], replay.get_json()['draft']['id'])
        self.assertFalse(replay.get_json()['created'])
        self.assertEqual(self.submit(token, payload=self.payload(price='0.004')).status_code, 409)
        with self.app.app_context():
            self.assertEqual(AgentDraft.query.count(), 1)
            draft = AgentDraft.query.one()
            self.assertEqual(draft.owner_id, 'owner')
            self.assertEqual(draft.connection_id, connection_id)
            self.assertEqual(draft.actor, 'agent')
            self.assertEqual(draft.terms['total'], '3.5')
            self.assertEqual(draft.terms['payment_asset']['chain_id'], 8453)
            self.assertIsNotNone(draft.created_at)
            self.assertIsNotNone(db.session.get(AgentConnection, connection_id).last_used_at)

    def test_pending_quota_is_owner_wide_and_replays_still_work(self):
        token, connection_id, _ = self.issue()
        other_token, _, _ = self.issue(label='second credential')
        self.submit(token)
        terms, notes, payload_hash = validate_draft(self.payload())
        with self.app.app_context():
            for index in range(49):
                db.session.add(AgentDraft(owner_id='owner', connection_id=connection_id, idempotency_key=f'filled-{index}',
                    payload_hash=payload_hash, terms=terms, notes=notes))
            db.session.commit()
        self.assertEqual(self.submit(other_token, 'over-quota').status_code, 429)
        self.assertEqual(self.submit(token).status_code, 200)
        with self.app.app_context():
            self.assertEqual(AgentDraft.query.count(), 50)

    def test_hourly_creation_quota_counts_dismissed_drafts(self):
        token, connection_id, _ = self.issue()
        terms, notes, payload_hash = validate_draft(self.payload())
        with self.app.app_context():
            for index in range(100):
                db.session.add(AgentDraft(owner_id='owner', connection_id=connection_id, idempotency_key=f'hour-{index}',
                    payload_hash=payload_hash, terms=terms, notes=notes, status='dismissed'))
            db.session.commit()
        response = self.submit(token, 'over-hourly')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '3600')

    def test_active_connection_cap_is_enforced(self):
        for index in range(5):
            self.issue(label=f'agent {index}')
        response = self.owner.post('/agents/connections', data={'csrf_token': self.csrf(), 'label': 'sixth'})
        self.assertEqual(response.status_code, 429)
        with self.app.app_context():
            self.assertEqual(AgentConnection.query.count(), 5)

    def test_dismissal_is_idempotent_and_does_not_recreate_on_retry(self):
        token, _, _ = self.issue()
        draft_id = self.submit(token).get_json()['draft']['id']
        csrf = self.csrf()
        for _ in range(2):
            self.assertEqual(self.owner.post(f'/agents/drafts/{draft_id}/dismiss', data={'csrf_token': csrf}).status_code, 302)
        self.assertEqual(self.submit(token).get_json()['draft']['status'], 'dismissed')
        with self.app.app_context():
            self.assertEqual(AgentDraft.query.count(), 1)
            draft = db.session.get(AgentDraft, draft_id)
            self.assertIsNotNone(draft.dismissed_at)
            self.assertEqual(draft.dismissed_by_user_id, 'owner')

    def test_untrusted_notes_and_connection_labels_are_escaped(self):
        token, _, _ = self.issue(label='<b>untrusted label</b>')
        self.submit(token, payload=self.payload(notes='<script>alert(1)</script>'))
        body = self.owner.get('/agents').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', body)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', body)
        self.assertNotIn('<b>untrusted label</b>', body)
        self.assertIn('&lt;b&gt;untrusted label&lt;/b&gt;', body)

    def test_draft_token_cannot_fall_through_to_public_guest_trade_routes(self):
        token, _, _ = self.issue()
        headers = {'Authorization': f'Bearer {token}'}
        paths = ['/p2p/offers', '/p2p/offers/999/accept', '/p2p/trades/999/action',
                 '/orders', '/agents/connections', '/agents/connections/1/revoke', '/agents/drafts/1/dismiss']
        for client in (self.machine, self.owner):
            for path in paths:
                with self.subTest(path=path, client=client):
                    response = client.post(path, data={**self.payload(), 'gems_stake': '0', 'csrf_token': self.csrf()}, headers=headers)
                    self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 2)
            self.assertEqual(AgentConnection.query.count(), 1)
            self.assertEqual(P2POffer.query.count(), 0)
            self.assertEqual(P2PTrade.query.count(), 0)
            self.assertEqual(Order.query.count(), 0)
            self.assertEqual(Swap.query.count(), 0)

    def test_machine_drafts_never_create_offers_trades_or_wallet_operations(self):
        from unittest.mock import patch
        token, _, _ = self.issue()
        with patch('routes.main.wallet_credit_gems') as credit, patch('routes.main.wallet_deduct_gems') as debit:
            self.assertEqual(self.submit(token).status_code, 201)
            self.assertEqual(self.machine.post('/api/agent/v1/drafts/1/publish', headers={'Authorization': f'Bearer {token}'}).status_code, 404)
        credit.assert_not_called()
        debit.assert_not_called()
        with self.app.app_context():
            self.assertEqual(P2POffer.query.count(), 0)
            self.assertEqual(P2PTrade.query.count(), 0)
            self.assertEqual(Order.query.count(), 0)
            self.assertEqual(Swap.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
