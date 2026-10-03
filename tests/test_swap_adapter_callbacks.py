import unittest
from decimal import Decimal

from app import create_app
from models import Order, Swap, User, db


class SwapAdapterCallbackTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.token = '22' * 32

        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                User(id='alice', username='Alice'),
                User(id='bob', username='Bob'),
            ])
            order = Order(
                user_id='alice',
                side='sell',
                amount_hns=Decimal('10'),
                price_btc_per_hns=Decimal('0.000001'),
                status='matched',
            )
            db.session.add(order)
            db.session.flush()
            swap = Swap(
                order_id=order.id,
                matcher_id='bob',
                role_alice_user_id='alice',
                secret_hash='11' * 32,
                status='initiated',
                adapter_token=self.token,
            )
            db.session.add(swap)
            db.session.commit()
            self.swap_id = swap.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_alice_lock_callback_records_txid_and_zero_output_index(self):
        txid = 'aa' * 32
        response = self.app.test_client().post(
            f'/api/swaps/{self.swap_id}/txids/alice-lock?token={self.token}',
            json={
                'txid': txid,
                'hns_lock_output_index': 0,
                'hns_lock_value': 1_000_000,
                'hns_lock_address': 'hs1qtest',
                'hns_lock_script': '51',
            },
        )

        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            swap = db.session.get(Swap, self.swap_id)
            self.assertEqual(swap.alice_lock_txid, txid)
            self.assertEqual(swap.hns_lock_value, 1_000_000)
            self.assertIsInstance(swap.hns_lock_value, int)
            self.assertEqual(swap.hns_lock_output_index, 0)
            self.assertEqual(swap.status, 'alice_locked')


if __name__ == '__main__':
    unittest.main()
