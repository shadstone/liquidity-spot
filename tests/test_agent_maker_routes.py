"""Bounded maker grants require a human review; APIs never inherit browser authority."""
from datetime import datetime, timedelta
from html import unescape
import json
import re
import time
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from app import create_app
from models import db, User, P2POffer, P2PTrade, P2PTradeMessage
from routes.agents import lookup_signer
from services.agent_maker import AgentMakerPolicy, AgentMakerAction
from services.agent_sso import create_sso_grant
from services.agent_workspace import AgentConnection, issue_connection
from services.payment_assets import make_terms_snapshot


AGENT = '00000000-0000-4000-8000-000000000003'
ACCOUNT = {'id': AGENT, 'username': 'pl-maker-example', 'accountKind': 'ai_agent'}
TOKEN = 'gfavip-session-' + 'a' * 64
POLICY = {
    'payment_asset': 'usdc-base', 'side': 'sell', 'min_price': '0.003', 'max_price': '0.01',
    'max_offer_hns': '100', 'total_hns_budget': '1000', 'max_open_offers': 5,
    'max_offers_per_hour': 10, 'allow_replies': True, 'max_replies_per_hour': 5,
    'max_replies_total': 20,
}
OFFER = {'side': 'sell', 'payment_asset': 'usdc-base', 'amount_hns': '10', 'price': '0.005',
         'notes': 'Untrusted public note'}


class AgentMakerRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app.config['AGENT_MAKER_ENABLED'] = True
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all([User(id=name, username=name, tier='free') for name in ('owner', 'other', 'taker')])
        db.session.commit()
        self.human = self.login('owner')
        self.other = self.login('other')
        self.machine = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self, owner):
        client = self.app.test_client()
        with client.session_transaction() as state:
            state['user_id'] = owner
        client.get('/agents')
        return client

    def csrf(self, client=None):
        with (client or self.human).session_transaction() as state:
            return state['agent_workspace_csrf']

    def review(self, client=None, **overrides):
        client = client or self.human
        fields = {'csrf_token': self.csrf(client), 'profile': 'maker-assistant',
                  'agent_username': ACCOUNT['username'], 'label': 'Bounded helper'}
        fields.update({'maker_' + key: ('yes' if value is True else '' if value is False else str(value))
                       for key, value in POLICY.items()})
        fields.update(overrides)
        with patch('routes.agents.lookup_agent_username', return_value=dict(ACCOUNT)) as lookup:
            response = client.post('/agents/lookup', data=fields)
        return response, lookup

    def proof(self, response):
        match = re.search(r'name="lookup_proof"[^>]*value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return unescape(match.group(1))

    def approve(self, proof, client=None, **overrides):
        client = client or self.human
        return client.post('/agents/connections', data={'csrf_token': self.csrf(client),
            'auth_mode': 'gfavip-sso', 'identity_mode': 'username', 'lookup_proof': proof,
            'confirm_agent': 'yes', 'confirm_maker_risk': 'yes', **overrides})

    def grant(self, owner='owner', policy=None, messages=False):
        connection = create_sso_grant(owner, 'Maker helper', AGENT, profile='maker-assistant',
            include_messages=messages, maker_policy=policy or dict(POLICY), reviewed_username=True)
        db.session.commit()
        return connection

    def api(self, path, connection=None, method='get', key='action-key', payload=None, client=None, **kwargs):
        headers = {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': key}
        if connection is not None:
            headers['X-Liquidity-Connection'] = str(connection.id)
        if payload is not None:
            kwargs['json'] = payload
        with patch('services.agent_sso.validate_agent_identity', return_value={'gfavip_user_id': AGENT}):
            return getattr(client or self.machine, method)('/api/agent/v1' + path, headers=headers, **kwargs)

    def publish(self, connection, key='publish-1', **overrides):
        return self.api('/offers', connection, 'post', key, {**OFFER, **overrides})

    def room(self, creator='owner', status='matched', offer_id=None):
        offer = db.session.get(P2POffer, offer_id) if offer_id else P2POffer(
            creator_id=creator, side='sell', amount_hns=10, amount_hns_exact='10',
            price_btc_per_hns=0, payment_asset_id='usdc-base', price_quote_per_hns='0.005', status='matched')
        offer.status = 'matched'
        db.session.add(offer)
        db.session.flush()
        trade = P2PTrade(offer_id=offer.id, creator_id=creator, counterparty_id='taker',
                         status=status, terms_snapshot=make_terms_snapshot(offer))
        db.session.add(trade)
        db.session.commit()
        return trade

    def test_review_is_script_free_and_creates_neither_grant_nor_offer(self):
        response, lookup = self.review(include_messages='yes')
        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with(ACCOUNT['username'])
        proof = lookup_signer().loads(self.proof(response))
        self.assertEqual(proof['maker_policy'], POLICY)
        self.assertTrue(proof['messages'])
        html = response.get_data(as_text=True)
        self.assertIn('confirm_maker_risk', html)
        self.assertNotIn('<script', html.lower())
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(AgentMakerPolicy.query.count(), 0)
        self.assertEqual(P2POffer.query.count(), 0)

    def test_approval_uses_only_reviewed_identity_policy_and_explicit_risk_consent(self):
        response, _ = self.review(include_messages='yes')
        proof = self.proof(response)
        for fields in ({'confirm_maker_risk': ''}, {'confirm_maker_risk': 'true'},
                       {'confirm_agent': ''}, {'maker_max_offer_hns': '999999'},
                       {'profile': 'trade-assistant'}, {'include_messages': ''},
                       {'lookup_proof': proof + 'changed'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.approve(proof, **fields).status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)
        with patch('routes.agents.lookup_agent_username') as lookup:
            result = self.approve(proof)
            lookup.assert_not_called()
        self.assertEqual(result.status_code, 200)
        connection = AgentConnection.query.one()
        self.assertEqual(connection.profile_id, 'maker-assistant')
        self.assertEqual(connection.sso_grant.agent_gfavip_user_id, AGENT)
        self.assertEqual(set(connection.scope_list), {'events:read', 'trades:read', 'offers:read', 'maker:write', 'trade_messages:read'})
        self.assertEqual(self.api('/maker-policy', connection).get_json()['policy'], POLICY)
        self.assertEqual(P2POffer.query.count(), 0)

    def test_approval_expiry_owner_session_and_replay_boundaries(self):
        response, _ = self.review()
        proof = self.proof(response)
        with patch('itsdangerous.timed.time.time', return_value=time.time() + 301):
            self.assertEqual(self.approve(proof).status_code, 400)
        self.assertEqual(self.approve(proof, client=self.other).status_code, 400)
        self.assertEqual(self.approve(proof, client=self.login('owner')).status_code, 400)
        cookie = self.human.get_cookie('session').value
        self.assertEqual(self.approve(proof).status_code, 200)
        replay = self.app.test_client()
        replay.set_cookie('session', cookie)
        self.assertEqual(self.approve(proof + '=', client=replay).status_code, 409)
        self.assertEqual(AgentConnection.query.count(), 1)
        self.assertEqual(AgentMakerPolicy.query.count(), 1)

    def test_invalid_policy_fails_before_provider_lookup(self):
        for fields in ({'maker_max_offer_hns': ''}, {'maker_min_price': '1'},
                       {'maker_side': 'both'}, {'maker_payment_asset': 'usdc'},
                       {'maker_max_open_offers': '1.5'}, {'maker_max_open_offers': '-1'},
                       {'maker_max_open_offers': 'true'}, {'maker_allow_replies': 'true'},
                       {'maker_allow_replies': '', 'maker_max_replies_total': '20'},
                       {'maker_owner_id': 'other'}):
            with self.subTest(fields=fields):
                response, lookup = self.review(**fields)
                self.assertEqual(response.status_code, 400)
                lookup.assert_not_called()
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_no_javascript_unchecked_replies_allow_blank_counts_but_not_implied_permission(self):
        response, _ = self.review(maker_allow_replies='', maker_max_replies_per_hour='', maker_max_replies_total='')
        self.assertEqual(response.status_code, 200)
        policy = lookup_signer().loads(self.proof(response))['maker_policy']
        self.assertFalse(policy['allow_replies'])
        self.assertEqual(policy['max_replies_per_hour'], 0)
        self.assertEqual(policy['max_replies_total'], 0)
        response, lookup = self.review(maker_max_replies_per_hour='', maker_max_replies_total='')
        self.assertEqual(response.status_code, 400)
        lookup.assert_not_called()

    def test_username_only_maker_no_direct_uuid_or_legacy_self_grant(self):
        for auth_mode in ('gfavip-sso', 'scoped-token'):
            result = self.human.post('/agents/connections', data={'csrf_token': self.csrf(),
                'auth_mode': auth_mode, 'identity_mode': 'uuid', 'agent_gfavip_user_id': AGENT,
                'profile': 'maker-assistant', 'label': 'Unreviewed'})
            self.assertEqual(result.status_code, 400)
        result = self.human.post('/agents/lookup', headers={'Authorization': 'Bearer ' + TOKEN},
                                data={'csrf_token': self.csrf()})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.machine.post('/agents/lookup').status_code, 302)
        self.assertEqual(self.human.post('/agents/lookup').status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_duplicate_risk_confirmation_and_policy_inputs_are_rejected(self):
        result = self.human.post('/agents/lookup', data=MultiDict([
            ('csrf_token', self.csrf()), ('profile', 'maker-assistant'),
            ('maker_max_open_offers', '1'), ('maker_max_open_offers', '10')]))
        self.assertEqual(result.status_code, 400)
        response, _ = self.review()
        result = self.human.post('/agents/connections', data=MultiDict([
            ('csrf_token', self.csrf()), ('auth_mode', 'gfavip-sso'), ('identity_mode', 'username'),
            ('lookup_proof', self.proof(response)), ('confirm_agent', 'yes'),
            ('confirm_maker_risk', 'yes'), ('confirm_maker_risk', 'no')]))
        self.assertEqual(result.status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_kill_switch_hides_profile_rejects_review_approval_and_api(self):
        response, _ = self.review()
        proof = self.proof(response)
        connection = self.grant()
        self.app.config['AGENT_MAKER_ENABLED'] = False
        self.assertEqual(self.review()[0].status_code, 400)
        self.assertEqual(self.approve(proof).status_code, 403)
        caps = self.machine.get('/api/agent/v1/capabilities').get_json()
        self.assertNotIn('maker-assistant', caps['profiles'])
        self.assertNotIn('maker', caps)
        for action in ('publish', 'accept', 'trade-action', 'message', 'fund', 'sign', 'withdraw'):
            self.assertIn(action, caps['forbidden_actions'])
        self.assertEqual(self.publish(connection).status_code, 403)
        self.assertEqual(self.api('/maker-policy', connection).status_code, 403)
        self.assertEqual(self.api('/trades/1/messages', connection, 'post', payload={'message': 'Hello'}).status_code, 403)
        self.assertEqual(P2POffer.query.count(), 0)

    def test_kill_switch_and_missing_policy_do_not_hide_owner_revoke_controls(self):
        connection = self.grant()
        self.app.config['AGENT_MAKER_ENABLED'] = False
        response = self.human.get('/agents')
        self.assertEqual(response.status_code, 200)
        revoke_path = f'/agents/connections/{connection.id}/revoke'
        self.assertIn(revoke_path, response.get_data(as_text=True))
        db.session.delete(AgentMakerPolicy.query.one())
        db.session.commit()
        self.app.config['AGENT_MAKER_ENABLED'] = True
        response = self.human.get('/agents')
        self.assertEqual(response.status_code, 200)
        self.assertIn(revoke_path, response.get_data(as_text=True))
        self.assertEqual(self.publish(connection).status_code, 403)
        self.assertEqual(self.human.post(revoke_path, data={'csrf_token': self.csrf()}).status_code, 302)
        db.session.refresh(connection)
        self.assertIsNotNone(connection.revoked_at)

    def test_capabilities_never_claim_settlement_or_existing_scope_expansion(self):
        caps = self.machine.get('/api/agent/v1/capabilities').get_json()
        self.assertEqual(caps['default_profile'], 'trade-assistant')
        self.assertIn('maker-assistant', caps['profiles'])
        self.assertIn('gain no write permissions', caps['maker']['existing_profiles'])
        for action in ('accept', 'trade-action', 'fund', 'sign', 'withdraw'):
            self.assertIn(action, caps['forbidden_actions'])

    def test_browser_session_and_old_profiles_cannot_publish_cancel_or_reply(self):
        for path in ('/offers', '/offers/1/cancel', '/trades/1/messages'):
            self.assertEqual(self.human.post('/api/agent/v1' + path, json=OFFER).status_code, 401)
        for profile in ('trade-assistant', 'offer-drafts'):
            connection, token = issue_connection('owner', profile, profile=profile)
            db.session.commit()
            for path in ('/offers', '/offers/1/cancel', '/trades/1/messages'):
                response = self.machine.post('/api/agent/v1' + path, json=OFFER,
                    headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'no-expansion'})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(P2POffer.query.count(), 0)
        self.assertEqual(P2PTradeMessage.query.count(), 0)

    def test_publish_idempotency_json_limits_and_no_browser_csrf_authority(self):
        connection = self.grant()
        response = self.publish(connection)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        replay = self.publish(connection)
        self.assertEqual(replay.status_code, 200)
        self.assertFalse(replay.get_json()['created'])
        self.assertEqual(response.get_json()['offer']['id'], replay.get_json()['offer']['id'])
        self.assertEqual(self.publish(connection, amount_hns='11').status_code, 409)
        self.assertEqual(self.publish(connection, key='').status_code, 400)
        self.assertEqual(self.api('/offers?owner_id=other', connection, 'post', payload=OFFER).status_code, 400)
        self.assertEqual(self.api('/offers', connection, 'post', data='{}').status_code, 415)
        self.assertEqual(self.api('/offers', connection, 'post', data='{"side":"sell","side":"buy"}',
                                  content_type='application/json').status_code, 400)
        self.assertEqual(self.api('/offers', connection, 'post', data='x' * 8193,
                                  content_type='application/json').status_code, 413)
        self.assertEqual(P2POffer.query.count(), 1)
        self.assertEqual(P2PTrade.query.count(), 0)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)

    def test_book_and_mine_reads_are_paginated_filtered_and_owner_safe(self):
        connection = self.grant()
        own = self.publish(connection).get_json()['offer']['id']
        public = P2POffer(creator_id='other', side='buy', amount_hns=2, price_btc_per_hns='0.000001',
                          status='open', notes='Public counteroffer')
        private = P2POffer(creator_id='other', side='sell', amount_hns=2, price_btc_per_hns='0.000001',
                           status='canceled', notes='PRIVATE-CANCELED-OFFER')
        db.session.add_all([public, private])
        db.session.commit()
        page = self.api('/offers?limit=1', connection).get_json()
        self.assertEqual([offer['id'] for offer in page['offers']], [own])
        self.assertTrue(page['offers'][0]['is_mine'])
        self.assertTrue(page['has_more'])
        page2 = self.api('/offers?after=' + str(page['next_cursor']), connection).get_json()
        self.assertEqual([offer['id'] for offer in page2['offers']], [public.id])
        self.assertFalse(page2['offers'][0]['is_mine'])
        self.assertNotIn('PRIVATE-CANCELED-OFFER', json.dumps(page2))
        self.assertNotIn('creator_id', json.dumps(page2))
        mine = self.api('/offers?scope=mine', connection).get_json()
        self.assertEqual([offer['id'] for offer in mine['offers']], [own])
        filtered = self.api('/offers?payment_asset=btc-bitcoin&side=buy', connection).get_json()
        self.assertEqual([offer['id'] for offer in filtered['offers']], [public.id])
        for query in ('scope=all', 'owner_id=other', 'scope=book&scope=mine', 'limit=101', 'side=both', 'payment_asset=usdc'):
            self.assertEqual(self.api('/offers?' + query, connection).status_code, 400)

    def test_cancel_own_agent_offer_only_and_budget_never_refunded(self):
        connection = self.grant()
        offer_id = self.publish(connection).get_json()['offer']['id']
        before = self.api('/maker-policy', connection).get_json()
        canceled = self.api(f'/offers/{offer_id}/cancel', connection, 'post', 'cancel-1', {})
        self.assertEqual(canceled.status_code, 201)
        self.assertEqual(self.api(f'/offers/{offer_id}/cancel', connection, 'post', 'cancel-1', {}).status_code, 200)
        after = self.api('/maker-policy', connection).get_json()
        self.assertEqual(before['policy'], after['policy'])
        self.assertEqual(before['usage']['remaining_hns'], after['usage']['remaining_hns'])
        other = self.grant(owner='other')
        self.assertEqual(self.api(f'/offers/{offer_id}/cancel', other, 'post', 'cross-owner', {}).status_code, 404)
        self.assertEqual(self.api('/maker-policy?owner_id=other', connection).status_code, 400)
        self.assertEqual(self.api('/maker-policy', connection, 'post', payload={}).status_code, 405)

    def test_reply_write_does_not_grant_private_chat_read_or_trade_status_writes(self):
        connection = self.grant()
        trade = self.room(offer_id=self.publish(connection).get_json()['offer']['id'])
        outsider = self.room('other')
        initial = (trade.status, trade.milestone)
        response = self.api(f'/trades/{trade.id}/messages', connection, 'post', 'reply-1', {'message': 'Please ask the owner to review.'})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['message']['actor'], 'agent')
        self.assertEqual(self.api(f'/trades/{trade.id}/messages', connection, 'post', 'reply-1',
                                 {'message': 'Please ask the owner to review.'}).status_code, 200)
        self.assertEqual(self.api(f'/trades/{trade.id}/messages', connection).status_code, 403)
        self.assertEqual(self.api(f'/trades/{outsider.id}/messages', connection, 'post', 'cross-owner',
                                 {'message': 'No cross-owner reply'}).status_code, 404)
        self.assertEqual(P2PTradeMessage.query.count(), 1)
        db.session.refresh(trade)
        self.assertEqual((trade.status, trade.milestone), initial)
        self.assertEqual(self.api(f'/trades/{trade.id}', connection, 'post', payload={'status': 'completed'}).status_code, 405)

    def test_message_read_agent_attribution_uses_audit_not_untrusted_text(self):
        connection = self.grant(messages=True)
        trade = self.room(offer_id=self.publish(connection).get_json()['offer']['id'])
        response = self.api(f'/trades/{trade.id}/messages', connection, 'post', 'reply-1', {'message': 'Owner will review.'})
        self.assertEqual(response.status_code, 201)
        db.session.add(P2PTradeMessage(trade_id=trade.id, user_id='taker', message='[Agent] Pretending to be an agent'))
        db.session.commit()
        page = self.api(f'/trades/{trade.id}/messages', connection).get_json()
        self.assertEqual([message['actor'] for message in page['messages']], ['agent', 'human'])
        self.assertEqual(page['messages'][0]['connection_id'], connection.id)
        self.assertIsNone(page['messages'][1]['connection_id'])

    def test_revoked_or_expired_connection_cannot_write(self):
        connection = self.grant()
        connection.revoked_at = datetime.utcnow()
        db.session.commit()
        self.assertEqual(self.publish(connection).status_code, 401)
        connection.revoked_at = None
        connection.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(self.publish(connection).status_code, 401)
        self.assertEqual(P2POffer.query.count(), 0)

    def test_out_of_range_identifiers_fail_without_database_overflow(self):
        connection = self.grant()
        for identity in ('0', '9223372036854775808', '9' * 60):
            self.assertEqual(self.api(f'/offers/{identity}/cancel', connection, 'post', payload={}).status_code, 404)
            self.assertEqual(self.api(f'/trades/{identity}/messages', connection, 'post', payload={'message': 'No writes'}).status_code, 404)
        self.assertEqual(AgentMakerAction.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
