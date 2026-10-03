from datetime import datetime
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from models import db, User, P2POffer, P2PTrade, P2PTradeMessage, Order, Swap
from services.trade_events import (
    AgentTradeEvent, EVENT_LOCK_NAMESPACE, _lock_participant_events,
    emit_trade_event, lock_trade_bond_writes, lock_trade_message_writes, serialize_event,
)
from services.gems_service import GemsServiceError


class TradeEventTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            db.session.add_all([User(id=user, username=user) for user in ('alice', 'bob', 'outsider')])
            offer = P2POffer(creator_id='alice', side='sell', amount_hns=1000,
                             price_btc_per_hns='0.000001', status='open')
            db.session.add(offer)
            db.session.commit()
            self.offer_id = offer.id
        self.sign_in('bob')

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def sign_in(self, user):
        with self.client.session_transaction() as state:
            state['user_id'] = user
            state['cancel_before_payment_token'] = 'test-token'

    def accept(self):
        response = self.client.post(f'/p2p/offers/{self.offer_id}/accept')
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            return P2PTrade.query.one().id

    def events(self, owner='alice'):
        with self.app.app_context():
            return [serialize_event(event) for event in AgentTradeEvent.query.filter_by(owner_id=owner).order_by(AgentTradeEvent.id).all()]

    def lock_bond(self, trade_id):
        with self.app.app_context():
            trade = db.session.get(P2PTrade, trade_id)
            trade.maker_bond_status = 'locked'
            trade.maker_bond_amount = 5
            trade.offer.maker_bond_status = 'locked'
            trade.offer.gems_stake = 5
            db.session.commit()

    def test_acceptance_journals_both_participants_not_public_bystanders(self):
        trade_id = self.accept()
        alice, bob = self.events('alice'), self.events('bob')
        self.assertEqual(len(alice), 1)
        self.assertEqual(len(bob), 1)
        self.assertEqual(self.events('outsider'), [])
        self.assertEqual(alice[0]['actor'], 'counterparty')
        self.assertEqual(bob[0]['actor'], 'self')
        for event in alice + bob:
            self.assertEqual(event['type'], 'p2p.offer_accepted')
            self.assertEqual(event['trade_id'], trade_id)
            self.assertEqual(event['actor_user_id'], 'bob')
            self.assertEqual(event['room_url'], f'/p2p/trades/{trade_id}')
            self.assertEqual(event['metadata'], {'status': 'matched', 'milestone': 'matched'})
            self.assertTrue(event['occurred_at'].endswith('Z'))

    def test_emit_never_commits_and_rollback_removes_business_change_and_events(self):
        with self.app.app_context():
            trade = P2PTrade(offer_id=self.offer_id, creator_id='alice', counterparty_id='bob')
            db.session.add(trade)
            db.session.get(P2POffer, self.offer_id).status = 'matched'
            with patch.object(db.session, 'commit') as commit:
                events = emit_trade_event(trade, 'p2p.offer_accepted', 'bob')
                self.assertEqual(len(events), 2)
                self.assertTrue(all(event.id for event in events))
                commit.assert_not_called()
            db.session.rollback()
            self.assertEqual(P2PTrade.query.count(), 0)
            self.assertEqual(AgentTradeEvent.query.count(), 0)
            self.assertEqual(db.session.get(P2POffer, self.offer_id).status, 'open')

    def test_route_event_failure_cannot_leave_accepted_trade_without_journal(self):
        def emit_then_fail(*args, **kwargs):
            emit_trade_event(*args, **kwargs)
            raise RuntimeError('test journal interruption')
        with patch('routes.main.emit_trade_event', side_effect=emit_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'journal interruption'):
                self.client.post(f'/p2p/offers/{self.offer_id}/accept')
        with self.app.app_context():
            self.assertEqual(P2PTrade.query.count(), 0)
            self.assertEqual(AgentTradeEvent.query.count(), 0)
            self.assertEqual(db.session.get(P2POffer, self.offer_id).status, 'open')

    def test_consecutive_same_actor_messages_each_have_distinct_cursor_and_message_id(self):
        trade_id = self.accept()
        for message in ('PRIVATE message one: address secret', 'PRIVATE message two: bank details'):
            self.client.post(f'/p2p/trades/{trade_id}/message', data={'message': message})
        for owner in ('alice', 'bob'):
            events = self.events(owner)
            chat_events = [event for event in events if event['type'] == 'p2p.message_added']
            self.assertEqual(len(chat_events), 2)
            self.assertGreater(chat_events[1]['id'], chat_events[0]['id'])
            self.assertNotEqual(chat_events[1]['metadata']['message_id'], chat_events[0]['metadata']['message_id'])
            self.assertTrue(all(event['actor_user_id'] == 'bob' for event in chat_events))
            self.assertNotIn('PRIVATE', json.dumps(events))
            self.assertNotIn('address secret', json.dumps(events))
        with self.app.app_context():
            self.assertEqual(P2PTradeMessage.query.count(), 2)

    def test_update_action_dispute_and_cancel_emit_metadata_only(self):
        trade_id = self.accept()
        txid = 'ab' * 32
        self.client.post(f'/p2p/trades/{trade_id}/update', data={
            'milestone': 'payment_sent', 'bob_lock_txid': txid, 'latest_note': 'PRIVATE note',
        })
        self.client.post(f'/p2p/trades/{trade_id}/action', data={'action': 'mark_disputed', 'note': 'PRIVATE dispute details'})
        self.client.post(f'/p2p/trades/{trade_id}/action', data={'action': 'mark_canceled', 'note': 'PRIVATE reason'})
        events = self.events()
        self.assertEqual([event['type'] for event in events], [
            'p2p.offer_accepted', 'p2p.trade_updated', 'p2p.dispute_opened', 'p2p.trade_canceled',
        ])
        self.assertEqual(events[1]['metadata']['milestone'], 'payment_sent')
        self.assertEqual(events[-1]['metadata']['status'], 'canceled')
        serialized = json.dumps(events)
        self.assertNotIn('PRIVATE', serialized)
        self.assertNotIn(txid, serialized)

    def test_cancel_before_payment_refreshes_state_and_rejects_repeated_cancel(self):
        trade_id = self.accept()
        data = {'cancel_token': 'test-token', 'confirm_no_payment': 'yes'}
        self.assertEqual(self.client.post(f'/p2p/trades/{trade_id}/cancel-before-payment', data=data).status_code, 302)
        self.assertEqual(self.events()[-1]['type'], 'p2p.trade_canceled')
        self.assertEqual(self.events()[-1]['metadata']['status'], 'canceled')
        before = self.events()
        self.assertEqual(self.client.post(f'/p2p/trades/{trade_id}/cancel-before-payment', data=data).status_code, 409)
        self.assertEqual(self.events(), before)

    def test_rejected_accepts_messages_and_actions_do_not_emit_events(self):
        self.sign_in('alice')
        self.client.post(f'/p2p/offers/{self.offer_id}/accept')
        self.assertEqual(self.events(), [])
        self.sign_in('bob')
        trade_id = self.accept()
        before = self.events()
        self.client.post(f'/p2p/offers/{self.offer_id}/accept')
        self.client.post(f'/p2p/trades/{trade_id}/message', data={'message': '   '})
        self.client.post(f'/p2p/trades/{trade_id}/action', data={'action': 'not-a-real-action'})
        self.client.post(f'/p2p/trades/{trade_id}/update', data={'milestone': 'payment_sent', 'alice_lock_txid': 'bad txid'})
        self.client.post(f'/p2p/trades/{trade_id}/cancel-before-payment', data={})
        self.sign_in('outsider')
        self.client.post(f'/p2p/trades/{trade_id}/message', data={'message': 'not permitted'})
        self.client.post(f'/p2p/trades/{trade_id}/action', data={'action': 'mark_completed'})
        self.client.post(f'/p2p/trades/{trade_id}/update', data={'status': 'completed'})
        self.assertEqual(self.events(), before)

    def test_no_retroactive_or_atomic_swap_events(self):
        with self.app.app_context():
            existing = P2PTrade(offer_id=self.offer_id, creator_id='alice', counterparty_id='bob')
            order = Order(user_id='alice', side='sell', amount_hns=10, price_btc_per_hns='0.000001')
            db.session.add_all([existing, order])
            db.session.flush()
            swap = Swap(order_id=order.id, matcher_id='bob', role_alice_user_id='alice', status='initiated')
            db.session.add(swap)
            db.session.commit()
            trade_id, swap_id = existing.id, swap.id
        self.client.get(f'/p2p/trades/{trade_id}')
        self.client.post(f'/swaps/{swap_id}/cancel-before-payment', data={
            'cancel_token': 'test-token', 'confirm_no_payment': 'yes',
        })
        self.assertEqual(self.events(), [])

    def test_serializer_and_emitter_allowlist_prevents_accidental_text_expansion(self):
        trade_id = self.accept()
        with self.app.app_context():
            trade = db.session.get(P2PTrade, trade_id)
            trade.status = 'PRIVATE secret status'
            emit_trade_event(trade, 'p2p.trade_updated', 'bob')
            db.session.commit()
        event = self.events()[-1]
        self.assertEqual(event['metadata']['status'], 'unknown')
        self.assertNotIn('PRIVATE', json.dumps(event))
        stored = SimpleNamespace(id=123, trade_id=trade_id, owner_id='alice', actor_user_id='bob',
                    kind='p2p.message_added', created_at=datetime.utcnow(), payload={
                        'status': 'matched', 'milestone': 'matched', 'message_id': 4,
                        'message': 'PRIVATE', 'address': 'PRIVATE', 'txid': 'PRIVATE',
                    })
        self.assertNotIn('PRIVATE', json.dumps(serialize_event(stored)))
        stored.actor_user_id = None
        self.assertEqual(serialize_event(stored)['actor'], 'system')

    def test_postgresql_locks_use_sorted_stable_separate_namespace(self):
        fake_db = SimpleNamespace(engine=SimpleNamespace(dialect=SimpleNamespace(name='postgresql')), session=MagicMock())
        owners = ['bob', 'alice', 'alice']
        expected = sorted({int.from_bytes(hashlib.sha256(owner.encode()).digest()[:4], 'big', signed=True) for owner in owners})
        with patch('services.trade_events.db', fake_db):
            _lock_participant_events(owners)
        calls = fake_db.session.execute.call_args_list
        self.assertEqual(str(calls[0].args[0]), "SET LOCAL lock_timeout = '10s'")
        calls = calls[1:]
        self.assertEqual([call.args[1]['owner_key'] for call in calls], expected)
        self.assertTrue(all(call.args[1]['namespace'] == EVENT_LOCK_NAMESPACE for call in calls))
        self.assertTrue(all(str(call.args[0]) == 'SELECT pg_advisory_xact_lock(:namespace, :owner_key)' for call in calls))

    def test_postgresql_message_lock_refreshes_before_message_allocation(self):
        fake_db = SimpleNamespace(engine=SimpleNamespace(dialect=SimpleNamespace(name='postgresql')), session=MagicMock())
        trade = object()
        with patch('services.trade_events.db', fake_db):
            lock_trade_message_writes(trade)
        self.assertEqual(str(fake_db.session.execute.call_args.args[0]), "SET LOCAL lock_timeout = '10s'")
        fake_db.session.refresh.assert_called_once_with(trade, with_for_update={'key_share': True})

    def test_bond_row_locks_use_trade_then_offer_before_event_preparation(self):
        fake_db = SimpleNamespace(engine=SimpleNamespace(dialect=SimpleNamespace(name='postgresql')), session=MagicMock())
        trade = SimpleNamespace(offer=object())
        with patch('services.trade_events.db', fake_db):
            lock_trade_bond_writes(trade)
        calls = fake_db.session.refresh.call_args_list
        self.assertEqual([call.args[0] for call in calls], [trade, trade.offer])
        self.assertTrue(all(call.kwargs == {'with_for_update': {'key_share': True}} for call in calls))

    def test_chat_and_action_lock_before_constructing_messages(self):
        trade_id = self.accept()
        lock_calls = []
        def check_before_message(trade):
            self.assertEqual(trade.id, trade_id)
            self.assertFalse(any(isinstance(row, P2PTradeMessage) for row in db.session.new))
            lock_calls.append(trade.id)
        with patch('routes.main.lock_trade_message_writes', side_effect=check_before_message):
            self.client.post(f'/p2p/trades/{trade_id}/message', data={'message': 'first'})
            self.client.post(f'/p2p/trades/{trade_id}/action', data={'action': 'mark_payment_sent', 'note': 'second'})
        self.assertEqual(lock_calls, [trade_id, trade_id])
        with self.app.app_context():
            self.assertEqual(P2PTradeMessage.query.count(), 2)

    def test_admin_resolution_emits_system_metadata_without_notes(self):
        trade_id = self.accept()
        self.sign_in('outsider')
        with self.client.session_transaction() as state:
            state['tier'] = 'team'
        for status in ('disputed', 'completed', 'canceled'):
            response = self.client.post(f'/admin/p2p-trades/{trade_id}/resolve', data={
                'status': status, 'admin_review_status': 'resolved',
                'admin_notes': 'PRIVATE admin assessment', 'admin_resolution': 'PRIVATE resolution',
            })
            self.assertEqual(response.status_code, 302)
        for owner in ('alice', 'bob'):
            events = self.events(owner)[1:]
            self.assertEqual([event['type'] for event in events], [
                'p2p.dispute_opened', 'p2p.trade_updated', 'p2p.trade_canceled',
            ])
            self.assertTrue(all(event['actor'] == 'system' and event['actor_user_id'] is None for event in events))
            self.assertNotIn('PRIVATE', json.dumps(events))
            self.assertNotIn('outsider', json.dumps(events))
        self.assertEqual(self.events('outsider'), [])

    def test_rejected_admin_resolution_does_not_emit_or_change_trade(self):
        trade_id = self.accept()
        before = self.events()
        self.client.post(f'/admin/p2p-trades/{trade_id}/resolve', data={'status': 'completed'})
        self.assertEqual(self.events(), before)
        with self.app.app_context():
            self.assertEqual(db.session.get(P2PTrade, trade_id).status, 'matched')

    def test_admin_event_failure_rolls_back_resolution(self):
        trade_id = self.accept()
        before = self.events()
        self.sign_in('outsider')
        with self.client.session_transaction() as state:
            state['tier'] = 'team'
        def emit_then_fail(*args, **kwargs):
            emit_trade_event(*args, **kwargs)
            raise RuntimeError('test admin journal interruption')
        with patch('routes.admin.emit_trade_event', side_effect=emit_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'admin journal interruption'):
                self.client.post(f'/admin/p2p-trades/{trade_id}/resolve', data={
                    'status': 'completed', 'admin_notes': 'PRIVATE',
                })
        self.assertEqual(self.events(), before)
        with self.app.app_context():
            trade = db.session.get(P2PTrade, trade_id)
            self.assertEqual(trade.status, 'matched')
            self.assertIsNone(trade.admin_notes)

    def test_completion_journal_failure_never_credits_wallet_even_on_retry(self):
        trade_id = self.accept()
        self.lock_bond(trade_id)
        before = self.events()
        def emit_then_fail(*args, **kwargs):
            emit_trade_event(*args, **kwargs)
            raise RuntimeError('journal preparation failed')
        with patch('routes.main.emit_trade_event', side_effect=emit_then_fail), \
             patch('routes.main.wallet_credit_gems') as credit:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'journal preparation failed'):
                    self.client.post(f'/p2p/trades/{trade_id}/action', data={
                        'action': 'mark_completed', 'note': 'rolled back completion',
                    })
            credit.assert_not_called()
        self.assertEqual(self.events(), before)
        with self.app.app_context():
            trade = db.session.get(P2PTrade, trade_id)
            self.assertEqual(trade.status, 'matched')
            self.assertEqual(trade.maker_bond_status, 'locked')
            self.assertEqual(trade.offer.maker_bond_status, 'locked')
            self.assertEqual(P2PTradeMessage.query.count(), 0)

    def test_admin_refund_journal_failure_never_credits_wallet_even_on_retry(self):
        trade_id = self.accept()
        self.lock_bond(trade_id)
        before = self.events()
        self.sign_in('outsider')
        with self.client.session_transaction() as state:
            state['tier'] = 'team'
        def emit_then_fail(*args, **kwargs):
            emit_trade_event(*args, **kwargs)
            raise RuntimeError('journal preparation failed')
        with patch('routes.admin.emit_trade_event', side_effect=emit_then_fail), \
             patch('routes.admin.is_wallet_service_configured', return_value=True), \
             patch('routes.admin.wallet_credit_gems') as credit:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'journal preparation failed'):
                    self.client.post(f'/admin/p2p-trades/{trade_id}/resolve', data={
                        'status': 'completed', 'bond_action': 'refund_full',
                    })
            credit.assert_not_called()
        self.assertEqual(self.events(), before)
        with self.app.app_context():
            trade = db.session.get(P2PTrade, trade_id)
            self.assertEqual(trade.status, 'matched')
            self.assertEqual(trade.maker_bond_status, 'locked')
            self.assertEqual(trade.offer.maker_bond_status, 'locked')

    def test_completion_prepares_event_before_credit_and_commits_refund(self):
        trade_id = self.accept()
        self.lock_bond(trade_id)
        def credit_after_journal(*args, **kwargs):
            self.assertEqual(AgentTradeEvent.query.filter_by(trade_id=trade_id).count(), 4)
            event = AgentTradeEvent.query.filter_by(owner_id='alice').order_by(AgentTradeEvent.id.desc()).first()
            self.assertEqual(event.payload, {'status': 'completed', 'milestone': 'completed'})
            self.assertEqual(db.session.get(P2PTrade, trade_id).maker_bond_status, 'locked')
            return {'amountCredited': 5}
        with patch('routes.main.wallet_credit_gems', side_effect=credit_after_journal) as credit:
            response = self.client.post(f'/p2p/trades/{trade_id}/action', data={'action': 'mark_completed'})
            self.assertEqual(response.status_code, 302)
            credit.assert_called_once()
        self.assertEqual(self.events()[-1]['metadata']['status'], 'completed')
        with self.app.app_context():
            trade = db.session.get(P2PTrade, trade_id)
            self.assertEqual(trade.maker_bond_status, 'refunded')
            self.assertEqual(trade.offer.maker_bond_status, 'refunded')

    def test_admin_refund_prepares_system_event_before_credit_and_commits(self):
        trade_id = self.accept()
        self.lock_bond(trade_id)
        self.sign_in('outsider')
        with self.client.session_transaction() as state:
            state['tier'] = 'team'
        def credit_after_journal(*args, **kwargs):
            self.assertEqual(AgentTradeEvent.query.filter_by(trade_id=trade_id).count(), 4)
            event = AgentTradeEvent.query.filter_by(owner_id='alice').order_by(AgentTradeEvent.id.desc()).first()
            self.assertIsNone(event.actor_user_id)
            self.assertEqual(event.payload['status'], 'completed')
            return {'amountCredited': 5}
        with patch('routes.admin.is_wallet_service_configured', return_value=True), \
             patch('routes.admin.wallet_credit_gems', side_effect=credit_after_journal) as credit:
            response = self.client.post(f'/admin/p2p-trades/{trade_id}/resolve', data={
                'status': 'completed', 'bond_action': 'refund_full',
            })
            self.assertEqual(response.status_code, 302)
            credit.assert_called_once()
        self.assertEqual(self.events()[-1]['actor'], 'system')
        with self.app.app_context():
            trade = db.session.get(P2PTrade, trade_id)
            self.assertEqual(trade.maker_bond_status, 'refunded')
            self.assertEqual(trade.offer.maker_bond_status, 'refunded')

    def test_failed_admin_refund_rolls_back_prepared_event(self):
        trade_id = self.accept()
        self.lock_bond(trade_id)
        before = self.events()
        self.sign_in('outsider')
        with self.client.session_transaction() as state:
            state['tier'] = 'team'
        with patch('routes.admin.is_wallet_service_configured', return_value=True), \
             patch('routes.admin.wallet_credit_gems', side_effect=GemsServiceError('test unavailable')):
            response = self.client.post(f'/admin/p2p-trades/{trade_id}/resolve', data={
                'status': 'completed', 'bond_action': 'refund_full',
            })
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.events(), before)
        with self.app.app_context():
            self.assertEqual(db.session.get(P2PTrade, trade_id).status, 'matched')


if __name__ == '__main__':
    unittest.main()
