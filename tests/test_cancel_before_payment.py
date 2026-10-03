import unittest

from app import create_app
from models import db, User, Order, Swap, SwapMessage, P2POffer, P2PTrade, P2PTradeMessage


class CancelBeforePaymentTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            db.session.add_all([User(id=u, username=u) for u in ['alice', 'bob', 'outsider']])
            order = Order(user_id='alice', side='sell', amount_hns=10, price_btc_per_hns='.000001', status='matched')
            offer = P2POffer(creator_id='alice', side='sell', amount_hns=10, price_btc_per_hns='.000001', status='matched')
            new_offer = P2POffer(creator_id='alice', side='sell', amount_hns=20, price_btc_per_hns='.000001', status='open')
            db.session.add_all([order, offer, new_offer])
            db.session.flush()
            swap = Swap(order_id=order.id, matcher_id='bob', role_alice_user_id='alice', status='initiated', adapter_token='old-token')
            trade = P2PTrade(offer_id=offer.id, creator_id='alice', counterparty_id='bob')
            db.session.add_all([swap, trade])
            db.session.commit()
            self.swap_id, self.trade_id, self.new_offer_id = swap.id, trade.id, new_offer.id
        self.sign_in('alice')

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def sign_in(self, user):
        with self.client.session_transaction() as sess:
            sess['user_id'] = user
            sess['cancel_before_payment_token'] = 'test-token'

    def url(self, kind):
        return f'/swaps/{self.swap_id}' if kind == 'swap' else f'/p2p/trades/{self.trade_id}'

    def cancel(self, kind, data=None):
        return self.client.post(self.url(kind) + '/cancel-before-payment', data=data if data is not None else {
            'cancel_token': 'test-token', 'confirm_no_payment': 'yes',
        })

    def test_visible_in_both_rooms_and_hidden_after_payment(self):
        for kind, model, rid in [('swap', Swap, self.swap_id), ('p2p', P2PTrade, self.trade_id)]:
            with self.subTest(kind=kind):
                response = self.client.get(self.url(kind))
                self.assertEqual(response.status_code, 200)
                self.assertIn('Cancel before payment</button>', response.get_data(as_text=True))
                with self.app.app_context():
                    db.session.get(model, rid).alice_lock_txid = 'aa' * 32
                    db.session.commit()
                self.assertNotIn('Cancel before payment</button>', self.client.get(self.url(kind)).get_data(as_text=True))

    def test_either_party_can_cancel_without_timeout(self):
        for kind, model, rid in [('swap', Swap, self.swap_id), ('p2p', P2PTrade, self.trade_id)]:
            for user in ['alice', 'bob']:
                with self.subTest(kind=kind, user=user):
                    with self.app.app_context():
                        row = db.session.get(model, rid)
                        row.status = 'initiated' if kind == 'swap' else 'matched'
                        db.session.commit()
                    self.sign_in(user)
                    self.assertEqual(self.cancel(kind).status_code, 302)
                    with self.app.app_context():
                        row = db.session.get(model, rid)
                        self.assertEqual(row.status, 'canceled')
                        self.assertEqual(row.admin_review_status, 'unreviewed')
                        self.assertEqual(db.session.get(P2POffer, self.new_offer_id).status, 'open')
                        if kind == 'swap':
                            self.assertIsNone(row.adapter_token)
                    self.assertEqual(self.cancel(kind).status_code, 409)
        with self.app.app_context():
            self.assertEqual(SwapMessage.query.count(), 2)
            self.assertEqual(P2PTradeMessage.query.count(), 2)

    def test_requires_participant_confirmation_and_session_token(self):
        for kind in ['swap', 'p2p']:
            self.assertEqual(self.cancel(kind, {}).status_code, 400)
            self.assertEqual(self.cancel(kind, {'cancel_token': 'test-token'}).status_code, 400)
            self.sign_in('outsider')
            self.assertEqual(self.cancel(kind).status_code, 403)
            self.sign_in('alice')

    def test_rechecks_funded_terminal_and_review_states(self):
        cases = [
            (Swap, self.swap_id, 'swap', 'status', 'alice_locked'),
            (Swap, self.swap_id, 'swap', 'status', 'completed'),
            (Swap, self.swap_id, 'swap', 'bob_refund_txid', 'aa' * 32),
            (Swap, self.swap_id, 'swap', 'hns_lock_output_index', 0),
            (Swap, self.swap_id, 'swap', 'alice_lock_txid', 'aa' * 32),
            (P2PTrade, self.trade_id, 'p2p', 'milestone', 'payment_sent'),
            (P2PTrade, self.trade_id, 'p2p', 'status', 'disputed'),
            (P2PTrade, self.trade_id, 'p2p', 'bob_lock_txid', 'aa' * 32),
            (P2PTrade, self.trade_id, 'p2p', 'admin_review_status', 'in_review'),
        ]
        for model, rid, kind, field, value in cases:
            with self.subTest(kind=kind, field=field, value=value):
                with self.app.app_context():
                    row = db.session.get(model, rid)
                    old = getattr(row, field)
                    setattr(row, field, value)
                    db.session.commit()
                self.assertEqual(self.cancel(kind).status_code, 409)
                with self.app.app_context():
                    row = db.session.get(model, rid)
                    self.assertEqual(getattr(row, field), value)
                    setattr(row, field, old)
                    db.session.commit()

    def test_bond_is_retained_for_review(self):
        with self.app.app_context():
            trade = db.session.get(P2PTrade, self.trade_id)
            trade.maker_bond_amount = 5
            trade.maker_bond_status = 'locked'
            db.session.commit()
        self.assertEqual(self.cancel('p2p').status_code, 302)
        with self.app.app_context():
            trade = db.session.get(P2PTrade, self.trade_id)
            self.assertEqual(trade.maker_bond_status, 'locked')
            self.assertEqual(trade.admin_review_status, 'in_review')

    def test_canceled_room_cannot_be_reopened_by_old_p2p_form(self):
        self.assertEqual(self.cancel('p2p').status_code, 302)
        for path, data in [('/action', {'action': 'mark_payment_sent'}),
                           ('/update', {'status': 'matched', 'milestone': 'payment_sent'})]:
            self.assertEqual(self.client.post(self.url('p2p') + path, data=data).status_code, 409)

    def test_old_wallet_token_is_rejected_after_cancel(self):
        self.assertEqual(self.cancel('swap').status_code, 302)
        response = self.client.post(f'/api/swaps/{self.swap_id}/txids/alice-lock?token=old-token', json={'txid': 'aa' * 32})
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
