"""Isolated maker bounds; no real wallets, credentials, offers or messages."""
from datetime import datetime, timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from sqlalchemy import update

from app import create_app
from models import db, User, P2POffer, P2PTrade, P2PTradeMessage
from services.agent_sso import AgentSSOGrant, create_sso_grant
from services.agent_workspace import AgentConnection, WorkspaceError, issue_connection, authenticate_bearer, digest_token
from services.agent_maker import (
    AgentMakerAction, AgentMakerPolicy, validate_maker_policy, publish_offer,
    cancel_offer, send_reply, policy_status, serialize_offer,
)
from services.payment_assets import make_terms_snapshot
from services.trade_events import AgentTradeEvent


AGENT_ID = '11111111-1111-4111-8111-111111111111'
POLICY = {'payment_asset': 'usdc-base', 'side': 'sell', 'min_price': '0.003',
    'max_price': '0.01', 'max_offer_hns': '100', 'total_hns_budget': '1000',
    'max_open_offers': 5, 'max_offers_per_hour': 10, 'allow_replies': True,
    'max_replies_per_hour': 5, 'max_replies_total': 20}
OFFER = {'side': 'sell', 'payment_asset': 'usdc-base', 'amount_hns': '100', 'price': '0.005', 'notes': 'Test offer'}


class AgentMakerTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app.config['AGENT_MAKER_ENABLED'] = True
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all([User(id=name, username=name) for name in ('alice', 'bob', 'other')])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def grant(self, owner='alice', **limits):
        connection = create_sso_grant(owner, 'Test maker', AGENT_ID, 'maker-assistant',
            maker_policy={**POLICY, **limits}, reviewed_username=True)
        db.session.commit()
        return connection

    def publish(self, connection, key='pub1', **payload):
        result, created = publish_offer(connection, key, {**OFFER, **payload})
        db.session.commit()
        self.assertTrue(created)
        return db.session.get(P2POffer, result['offer']['id'])

    def room(self, connection, key='pub1'):
        offer = self.publish(connection, key)
        offer.status = 'matched'
        trade = P2PTrade(offer_id=offer.id, creator_id=connection.owner_id,
            counterparty_id='bob', status='matched', milestone='matched',
            terms_snapshot=make_terms_snapshot(offer), latest_note='Human-controlled note')
        db.session.add(trade)
        db.session.commit()
        return trade

    def denied(self, status, function, *args, **kwargs):
        with self.assertRaises(WorkspaceError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)
        db.session.rollback()

    def test_policy_requires_every_explicit_field_and_strict_types(self):
        for key in POLICY:
            value = dict(POLICY)
            del value[key]
            self.denied(400, validate_maker_policy, value)
        for value in [None, [], {}, {**POLICY, 'extra': 'ignore'}]:
            self.denied(400, validate_maker_policy, value)
        mutations = [('payment_asset', ['usdc-base']), ('payment_asset', 'usdt-base'),
            ('side', 'both'), ('side', True), ('min_price', 0.003), ('max_price', 'NaN'),
            ('min_price', '1e-3'), ('min_price', '0'), ('max_price', '-1'),
            ('max_price', '0.0000000000000000001'), ('max_offer_hns', '0.0000001'),
            ('total_hns_budget', '10000000000000000'), ('max_open_offers', True),
            ('max_open_offers', 0), ('max_open_offers', 11), ('max_offers_per_hour', 21),
            ('max_offers_per_hour', '10'), ('allow_replies', 'true'),
            ('max_replies_total', True), ('max_replies_total', 201),
            ('max_replies_per_hour', 0), ('max_replies_per_hour', 21)]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                self.denied(400, validate_maker_policy, {**POLICY, key: value})
        self.denied(400, validate_maker_policy, {**POLICY, 'min_price': '0.02'})
        self.denied(400, validate_maker_policy, {**POLICY, 'total_hns_budget': '50'})
        self.denied(400, validate_maker_policy, {**POLICY, 'allow_replies': False})
        self.denied(400, validate_maker_policy, {**POLICY, 'min_price': '0.0000000001', 'max_price': '0.0000000002'})
        self.assertEqual(validate_maker_policy({**POLICY, 'max_offer_hns': '100.000000'})['max_offer_hns'], '100')
        self.assertFalse(validate_maker_policy({**POLICY, 'allow_replies': False,
            'max_replies_per_hour': 0, 'max_replies_total': 0})['allow_replies'])

    def test_maker_grants_require_flag_reviewed_sso_and_policy(self):
        self.denied(403, issue_connection, 'alice', 'legacy maker', 'maker-assistant', maker_policy=POLICY)
        self.denied(403, create_sso_grant, 'alice', 'manual maker', AGENT_ID, 'maker-assistant', maker_policy=POLICY)
        self.denied(400, create_sso_grant, 'alice', 'missing limits', AGENT_ID, 'maker-assistant', reviewed_username=True)
        self.app.config['AGENT_MAKER_ENABLED'] = False
        self.denied(503, create_sso_grant, 'alice', 'paused maker', AGENT_ID, 'maker-assistant',
            maker_policy=POLICY, reviewed_username=True)
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(AgentMakerPolicy.query.count(), 0)

    def test_policy_record_is_immutable_and_legacy_profile_does_not_take_policy(self):
        connection = self.grant()
        self.assertEqual(connection.profile_id, 'maker-assistant')
        self.assertLessEqual(len(connection.scope), 80)
        self.assertEqual(connection.authentication_method, 'gfavip-sso')
        self.denied(400, issue_connection, 'alice', 'wrong profile', 'offer-drafts', maker_policy=POLICY)
        record = AgentMakerPolicy.query.one()
        record.policy = {**POLICY, 'total_hns_budget': '999999'}
        self.denied(409, db.session.flush)
        self.assertEqual(AgentMakerPolicy.query.one().policy, POLICY)

    def test_policy_tampering_missing_binding_or_bad_scope_fail_closed(self):
        connection = self.grant()
        record = AgentMakerPolicy.query.one()
        db.session.execute(update(AgentMakerPolicy).where(AgentMakerPolicy.id == record.id).values(
            policy={**POLICY, 'total_hns_budget': '999999'}))
        db.session.commit()
        self.denied(403, publish_offer, connection, 'tampered', OFFER)
        db.session.execute(update(AgentMakerPolicy).where(AgentMakerPolicy.id == record.id).values(policy=POLICY))
        db.session.commit()
        connection.scope += ' drafts:write'
        db.session.commit()
        self.denied(403, publish_offer, connection, 'badscope', OFFER)
        connection.scope = 'events:read trades:read offers:read maker:write'
        db.session.delete(AgentSSOGrant.query.one())
        db.session.commit()
        self.denied(401, publish_offer, connection, 'nobinding', OFFER)
        self.assertEqual(P2POffer.query.count(), 0)

    def test_legacy_credentials_cannot_authenticate_maker_even_if_hash_known(self):
        connection = self.grant()
        token = 'ls_agent_' + 'a' * 43
        connection.token_hash = digest_token(token)
        db.session.commit()
        self.denied(401, authenticate_bearer, 'Bearer ' + token, 'maker:write')
        db.session.delete(AgentSSOGrant.query.one())
        db.session.commit()
        self.denied(401, authenticate_bearer, 'Bearer ' + token, 'maker:write')

    def test_publish_is_attributed_unbonded_exact_and_does_not_move_funds(self):
        connection = self.grant()
        offer = self.publish(connection, amount_hns='12.123456')
        result = serialize_offer(offer, 'alice')
        self.assertEqual(result['amount_hns'], '12.123456')
        self.assertEqual(result['price'], '0.005')
        self.assertEqual(result['actor'], 'agent')
        self.assertEqual(result['connection_id'], connection.id)
        self.assertTrue(result['is_mine'])
        self.assertFalse(result['funds_verified'])
        self.assertTrue(offer.notes.startswith(f'[AI agent connection #{connection.id}]\n'))
        self.assertEqual((offer.gems_stake, offer.maker_bond_status, offer.status), (0, 'none', 'open'))
        self.assertEqual(AgentMakerAction.query.one().amount_hns, '12.123456')
        self.assertEqual(P2PTrade.query.count(), 0)
        self.assertEqual(P2PTradeMessage.query.count(), 0)
        self.assertEqual([user.gems_balance for user in User.query.all()], [0, 0, 0])

    def test_publish_checks_side_network_price_amount_and_extra_fields(self):
        connection = self.grant()
        for key, value in [('side', 'buy'), ('payment_asset', 'usdc-ethereum'),
                           ('price', '0.002'), ('price', '0.011'), ('amount_hns', '100.000001')]:
            self.denied(403, publish_offer, connection, 'failed', {**OFFER, key: value})
        for key in ('gems_stake', 'creator_id', 'status', 'payment_address', 'terms_snapshot', 'price_btc_per_hns'):
            self.denied(400, publish_offer, connection, 'failed', {**OFFER, key: 'forbidden'})
        self.denied(400, publish_offer, connection, 'failed', {**OFFER, 'amount_hns': 100})
        self.assertEqual(P2POffer.query.count(), 0)
        self.assertEqual(AgentMakerAction.query.count(), 0)

    def test_publish_idempotency_shared_across_actions_and_targets(self):
        connection = self.grant()
        offer = self.publish(connection)
        first_id = offer.id
        replay, created = publish_offer(connection, 'pub1', dict(OFFER))
        self.assertFalse(created)
        self.assertEqual(replay['offer']['id'], first_id)
        db.session.commit()
        self.denied(409, publish_offer, connection, 'pub1', {**OFFER, 'notes': 'changed'})
        self.denied(409, cancel_offer, connection, first_id, 'pub1', {})
        for key in ('', 'x' * 129, 'space key', None):
            self.denied(400, publish_offer, connection, key, OFFER)
        self.assertEqual(P2POffer.query.count(), 1)
        self.assertEqual(AgentMakerAction.query.count(), 1)

    def test_lifetime_budget_not_restored_by_cancellation_or_completion(self):
        connection = self.grant(total_hns_budget='200')
        offer = self.publish(connection)
        cancel_offer(connection, offer.id, 'cancel1', {})
        db.session.commit()
        second = self.publish(connection, 'pub2')
        second.status = 'matched'
        db.session.add(P2PTrade(offer_id=second.id, creator_id='alice', counterparty_id='bob',
            status='completed', milestone='completed'))
        db.session.commit()
        self.denied(403, publish_offer, connection, 'pub3', OFFER)
        status = policy_status(connection)
        self.assertEqual(status['usage']['published_hns'], '200')
        self.assertEqual(status['usage']['remaining_hns'], '0')
        self.assertEqual(status['usage']['open_offers'], 0)
        replay, created = publish_offer(connection, 'pub1', OFFER)
        self.assertFalse(created)
        self.assertEqual(replay['offer']['status'], 'canceled')

    def test_fractional_budget_is_exact(self):
        connection = self.grant(max_offer_hns='0.100001', total_hns_budget='0.3')
        for index in range(3):
            self.publish(connection, f'fraction{index}', amount_hns='0.1')
        self.denied(403, publish_offer, connection, 'overflow', {**OFFER, 'amount_hns': '0.0001'})
        self.assertEqual(policy_status(connection)['usage']['published_hns'], '0.3')

    def test_open_and_hourly_limits_are_separate(self):
        connection = self.grant(max_open_offers=1, max_offers_per_hour=2)
        first = self.publish(connection)
        self.denied(429, publish_offer, connection, 'pub2', OFFER)
        cancel_offer(connection, first.id, 'cancel1', {})
        db.session.commit()
        second = self.publish(connection, 'pub2')
        cancel_offer(connection, second.id, 'cancel2', {})
        db.session.commit()
        self.denied(429, publish_offer, connection, 'pub3', OFFER)
        AgentMakerAction.query.filter_by(action='publish').update({'created_at': datetime.utcnow() - timedelta(hours=2)})
        db.session.commit()
        self.publish(connection, 'pub3')
        self.assertEqual(policy_status(connection)['usage']['published_hns'], '300')

    def test_revocation_expiry_and_kill_switch_rechecked_in_service(self):
        connection = self.grant()
        trade = self.room(connection)
        offer = self.publish(connection, 'pub2')
        for field, value, status in [('revoked_at', datetime.utcnow(), 401),
                                     ('expires_at', datetime.utcnow() - timedelta(seconds=1), 401),
                                     ('kill_flag', False, 503)]:
            connection.revoked_at = None
            connection.expires_at = datetime.utcnow() + timedelta(days=1)
            self.app.config['AGENT_MAKER_ENABLED'] = True
            if field == 'kill_flag':
                self.app.config['AGENT_MAKER_ENABLED'] = value
            else:
                setattr(connection, field, value)
            db.session.commit()
            self.denied(status, publish_offer, connection, 'new', OFFER)
            self.denied(status, publish_offer, connection, 'pub1', OFFER)
            self.denied(status, cancel_offer, connection, offer.id, 'cancel', {})
            self.denied(status, send_reply, connection, trade.id, 'reply', {'message': 'hello'})
        self.assertEqual(AgentMakerAction.query.count(), 2)

    def test_old_profiles_cannot_call_maker_service_directly(self):
        for profile in ('offer-drafts', 'trade-assistant'):
            connection = create_sso_grant('alice', profile, AGENT_ID, profile)
            db.session.commit()
            self.denied(403, publish_offer, connection, 'forbidden', OFFER)
            self.denied(403, cancel_offer, connection, 1, 'forbidden', {})
            self.denied(403, send_reply, connection, 1, 'forbidden', {'message': 'no'})
            self.assertIsNone(policy_status(connection))

    def test_cancellation_only_own_connection_unbonded_open_offers(self):
        owner = self.grant()
        sibling = self.grant()
        outsider = self.grant(owner='other')
        offer = self.publish(owner)
        other_offer = self.publish(outsider)
        self.denied(404, cancel_offer, sibling, offer.id, 'sibling', {})
        self.denied(404, cancel_offer, owner, other_offer.id, 'outsider', {})
        human = P2POffer(creator_id='alice', side='sell', amount_hns=1, price_btc_per_hns=1, status='open')
        db.session.add(human)
        db.session.commit()
        self.denied(404, cancel_offer, owner, human.id, 'human', {})
        self.denied(400, cancel_offer, owner, offer.id, 'extra', {'status': 'canceled'})
        self.denied(400, cancel_offer, owner, 9223372036854775808, 'too-large', {})
        for values in ({'gems_stake': 1, 'maker_bond_status': 'locked'},
                       {'gems_stake': 0, 'maker_bond_status': 'pending'},
                       {'gems_stake': 0, 'maker_bond_status': 'none', 'status': 'matched'}):
            for field, value in values.items():
                setattr(offer, field, value)
            db.session.commit()
            self.denied(409, cancel_offer, owner, offer.id, 'blocked', {})
        offer.status = 'open'
        db.session.commit()
        first, made = cancel_offer(owner, offer.id, 'cancel-ok', {})
        db.session.commit()
        self.assertTrue(made)
        second, made = cancel_offer(owner, offer.id, 'cancel-ok', {})
        self.assertFalse(made)
        self.assertEqual(first, second)
        self.assertEqual(policy_status(owner)['usage']['published_hns'], '100')

    def test_reply_attribution_events_and_no_trade_or_payment_mutation(self):
        connection = self.grant()
        self.assertNotIn('trade_messages:read', connection.scope_list)
        trade = self.room(connection)
        before = (trade.status, trade.milestone, trade.updated_at, trade.latest_note,
                  trade.alice_lock_txid, trade.bob_lock_txid, json.dumps(trade.terms_snapshot, sort_keys=True))
        reply, created = send_reply(connection, trade.id, 'reply1', {'message': 'Owner will verify the transfer.'})
        db.session.commit()
        self.assertTrue(created)
        self.assertTrue(reply['message']['content'].startswith(f'[AI agent connection #{connection.id}]\n'))
        self.assertEqual(P2PTradeMessage.query.one().user_id, 'alice')
        self.assertEqual((trade.status, trade.milestone, trade.updated_at, trade.latest_note,
            trade.alice_lock_txid, trade.bob_lock_txid, json.dumps(trade.terms_snapshot, sort_keys=True)), before)
        self.assertEqual({event.owner_id for event in AgentTradeEvent.query.all()}, {'alice', 'bob'})
        self.assertTrue(all(event.kind == 'p2p.message_added' for event in AgentTradeEvent.query.all()))
        replay, created = send_reply(connection, trade.id, 'reply1', {'message': 'Owner will verify the transfer.'})
        self.assertFalse(created)
        self.assertEqual(reply, replay)
        self.assertEqual(P2PTradeMessage.query.count(), 1)
        self.assertEqual(AgentTradeEvent.query.count(), 2)

    def test_replies_require_explicit_permission_not_optional_read_scope(self):
        connection = create_sso_grant('alice', 'read, not reply', AGENT_ID, 'maker-assistant',
            include_messages=True, maker_policy={**POLICY, 'allow_replies': False,
                'max_replies_per_hour': 0, 'max_replies_total': 0}, reviewed_username=True)
        db.session.commit()
        trade = self.room(connection)
        self.assertIn('trade_messages:read', connection.scope_list)
        self.denied(403, send_reply, connection, trade.id, 'denied', {'message': 'No consent'})

    def test_reply_room_ownership_connection_and_status_restrictions(self):
        connection = self.grant()
        sibling = self.grant()
        outsider = self.grant(owner='other')
        trade = self.room(connection)
        other_trade = self.room(outsider)
        self.denied(404, send_reply, sibling, trade.id, 'sibling', {'message': 'no'})
        self.denied(404, send_reply, connection, other_trade.id, 'outsider', {'message': 'no'})
        for status in ('completed', 'canceled', 'disputed', 'no_show', 'unexpected'):
            trade.status = status
            db.session.commit()
            self.denied(409, send_reply, connection, trade.id, 'closed', {'message': 'no'})
        trade.status = 'matched'
        trade.admin_review_status = 'in_review'
        db.session.commit()
        self.denied(409, send_reply, connection, trade.id, 'review', {'message': 'no'})
        self.assertEqual(P2PTradeMessage.query.count(), 0)

    def test_reply_strict_payload_length_and_cross_target_idempotency(self):
        connection = self.grant()
        trade = self.room(connection)
        second = self.room(connection, 'pub2')
        self.denied(400, send_reply, connection, 9223372036854775808, 'too-large', {'message': 'hello'})
        for payload in ({}, {'message': ''}, {'message': ' '}, {'message': 'x' * 1001},
                        {'message': 'null\x00'}, {'message': 'bad\x1b'}, {'message': 123},
                        {'content': 'not accepted'}, {'message': 'hello', 'status': 'completed'},
                        {'message': 'hello', 'payment_address': 'not accepted'}):
            self.denied(400, send_reply, connection, trade.id, 'invalid', payload)
        send_reply(connection, trade.id, 'reply', {'message': 'hello'})
        db.session.commit()
        self.denied(409, send_reply, connection, second.id, 'reply', {'message': 'hello'})
        self.denied(409, publish_offer, connection, 'reply', OFFER)
        self.denied(409, send_reply, connection, trade.id, 'reply', {'message': 'changed'})

    def test_reply_hour_and_lifetime_limits_replays_and_atomic_rollback(self):
        connection = self.grant(max_replies_per_hour=1, max_replies_total=2)
        trade = self.room(connection)
        send_reply(connection, trade.id, 'one', {'message': 'one'})
        db.session.commit()
        self.denied(429, send_reply, connection, trade.id, 'two', {'message': 'two'})
        AgentMakerAction.query.filter_by(action='reply').update({'created_at': datetime.utcnow() - timedelta(hours=2)})
        db.session.commit()
        send_reply(connection, trade.id, 'two', {'message': 'two'})
        db.session.commit()
        AgentMakerAction.query.filter_by(action='reply').update({'created_at': datetime.utcnow() - timedelta(hours=2)})
        db.session.commit()
        self.denied(429, send_reply, connection, trade.id, 'three', {'message': 'three'})
        trade.status = 'completed'
        db.session.commit()
        _, created = send_reply(connection, trade.id, 'one', {'message': 'one'})
        self.assertFalse(created)
        self.assertEqual(P2PTradeMessage.query.count(), 2)
        self.assertEqual(AgentTradeEvent.query.count(), 4)
        self.assertEqual(policy_status(connection)['usage']['replies_total'], 2)

    def test_failed_event_transaction_rolls_back_reply_and_audit(self):
        connection = self.grant()
        trade = self.room(connection)
        with patch('services.agent_maker.emit_trade_event', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                send_reply(connection, trade.id, 'rollback', {'message': 'never committed'})
            db.session.rollback()
        self.assertEqual(P2PTradeMessage.query.count(), 0)
        self.assertEqual(AgentMakerAction.query.filter_by(action='reply').count(), 0)
        send_reply(connection, trade.id, 'rollback', {'message': 'never committed'})
        db.session.rollback()
        self.assertEqual(P2PTradeMessage.query.count(), 0)
        self.assertEqual(AgentTradeEvent.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
