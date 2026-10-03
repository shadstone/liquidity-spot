import unittest
from decimal import Decimal
from sqlalchemy import create_engine, text
from app import create_app
from models import db, P2POffer, P2PTrade
from services.payment_assets import PAYMENT_ASSETS, get_payment_asset, parse_offer_amounts, format_decimal
from services.p2p_schema import ensure_payment_schema


class PaymentAssetTests(unittest.TestCase):
    def test_registry_is_network_specific_and_conservative(self):
        self.assertEqual(len(PAYMENT_ASSETS), 8)
        self.assertEqual(get_payment_asset('usdc-base')['chain_id'], 8453)
        self.assertEqual(get_payment_asset('usdc-optimism')['chain_id'], 10)
        self.assertEqual(get_payment_asset('usdt-ethereum')['chain_id'], 1)
        for unknown in ['usdt-base', 'usdt-optimism', 'weth-base', 'USDT', 'custom-token']:
            with self.assertRaises(ValueError):
                get_payment_asset(unknown)
        self.assertIsNone(get_payment_asset('eth-base')['contract'])

    def test_precise_math_and_rounding(self):
        self.assertEqual(parse_offer_amounts('10000', '0.005', 'usdt-ethereum')[2], Decimal('50'))
        self.assertEqual(parse_offer_amounts('1', '0.0000005', 'usdc-base')[2], Decimal('0.000001'))
        self.assertEqual(parse_offer_amounts('1', '0.000000000000000001', 'eth-base')[2], Decimal('1e-18'))
        self.assertEqual(format_decimal(Decimal('100.001000')), '100.001')
        self.assertEqual(format_decimal(0.0000005), '0.0000005')
        self.assertEqual(parse_offer_amounts('4444', '0.000000020319', 'btc-bitcoin')[2], Decimal('0.00009030'))

    def test_bad_values_cannot_enter_terms(self):
        for amount, price in [('NaN', '1'), ('1', 'Infinity'), ('-1', '1'), ('0', '1'),
                              ('1e6', '1'), ('1.0000001', '1'), ('1', '0.0000001'),
                              ('1', '1e999'), ('9' * 81, '1')]:
            with self.subTest(amount=amount, price=price), self.assertRaises(ValueError):
                parse_offer_amounts(amount, price, 'usdc-base')

    def test_additive_schema_upgrade_preserves_old_values_and_snapshots_once(self):
        engine = create_engine('sqlite://')
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE p2p_offers (id INTEGER PRIMARY KEY, side TEXT, amount_hns NUMERIC, price_btc_per_hns NUMERIC, payment_method TEXT, status TEXT)'))
            conn.execute(text('CREATE TABLE p2p_trades (id INTEGER PRIMARY KEY, offer_id INTEGER, status TEXT)'))
            conn.execute(text("INSERT INTO p2p_offers VALUES (1,'sell',1000,0.000001,'Manual Wallet Transfer','matched')"))
            conn.execute(text("INSERT INTO p2p_trades VALUES (1,1,'matched')"))
        ensure_payment_schema(engine)
        with engine.begin() as conn:
            original = conn.execute(text('SELECT terms_snapshot FROM p2p_trades')).scalar_one()
            self.assertIn('btc-bitcoin', original)
            conn.execute(text('UPDATE p2p_offers SET amount_hns=2000'))
        ensure_payment_schema(engine)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT terms_snapshot FROM p2p_trades')).scalar_one(), original)
            self.assertEqual(conn.execute(text('SELECT status FROM p2p_trades')).scalar_one(), 'matched')
            self.assertIsNone(conn.execute(text('SELECT payment_asset_id FROM p2p_offers')).scalar_one())
        engine.dispose()


class MultiAssetP2PTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.maker = self.app.test_client()
        self.taker = self.app.test_client()
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def create_offer(self, asset='usdt-ethereum', **kwargs):
        data = {'side': 'sell', 'amount_hns': '10000', 'price': '0.005', 'payment_asset': asset, 'gems_stake': '0'}
        data.update(kwargs)
        return self.maker.post('/p2p/offers', data=data)

    def test_each_pair_can_be_posted_and_terms_frozen(self):
        for index, asset_id in enumerate(PAYMENT_ASSETS, 1):
            with self.subTest(asset=asset_id):
                self.assertEqual(self.create_offer(asset_id).status_code, 302)
                self.taker.post(f'/p2p/offers/{index}/accept', data={'confirm_network': 'yes'})
                with self.app.app_context():
                    offer = db.session.get(P2POffer, index)
                    trade = P2PTrade.query.filter_by(offer_id=index).one()
                    self.assertEqual(trade.terms_snapshot['payment_asset']['id'], asset_id)
                    offer.price_quote_per_hns = '999'
                    offer.amount_hns = 5
                    offer.payment_asset_id = 'eth-ethereum'
                    db.session.commit()
                    self.assertEqual(trade.quote_price, Decimal('0.005'))
                    self.assertEqual(trade.amount_hns, Decimal('10000'))
                    self.assertEqual(trade.quote_total, Decimal('50'))
                    self.assertEqual(trade.payment_asset['id'], asset_id)

    def test_confirmation_is_required_and_cannot_double_accept(self):
        self.create_offer('usdc-base')
        self.taker.post('/p2p/offers/1/accept')
        with self.app.app_context():
            self.assertEqual(P2PTrade.query.count(), 0)
            self.assertEqual(db.session.get(P2POffer, 1).status, 'open')
        self.taker.post('/p2p/offers/1/accept', data={'confirm_network': 'yes'})
        self.app.test_client().post('/p2p/offers/1/accept', data={'confirm_network': 'yes'})
        with self.app.app_context():
            self.assertEqual(P2PTrade.query.count(), 1)

    def test_offer_room_and_receipt_show_chain_and_contract(self):
        self.create_offer('usdc-base')
        detail = self.taker.get('/p2p/offers/1').get_data(as_text=True)
        self.assertIn('Base', detail)
        self.assertIn('8453', detail)
        self.assertIn(get_payment_asset('usdc-base')['contract'], detail)
        self.taker.post('/p2p/offers/1/accept', data={'confirm_network': 'yes'})
        room = self.taker.get('/p2p/trades/1').get_data(as_text=True)
        self.assertIn('USDC', room)
        self.assertIn('8453', room)
        self.assertNotIn('You send BTC', room)
        from routes.main import _build_p2p_trade_receipt_text
        with self.app.app_context():
            receipt = _build_p2p_trade_receipt_text(db.session.get(P2PTrade, 1))
            self.assertIn('Total USDC: 50', receipt)
            self.assertIn('Chain ID: 8453', receipt)
            self.assertIn(get_payment_asset('usdc-base')['contract'], receipt)
            self.assertNotIn('BTC/HNS', receipt)

    def test_unsupported_pair_and_invalid_numbers_never_create_offer(self):
        for asset, values in [('usdt-base', {}), ('usdc-base', {'price': 'NaN'}),
                               ('eth-ethereum', {'side': 'oops'}), ('usdc-base', {'amount_hns': '-1'})]:
            self.create_offer(asset, **values)
        with self.app.app_context():
            self.assertEqual(P2POffer.query.count(), 0)

    def test_filter_does_not_mix_networks(self):
        self.create_offer('usdc-base', notes='BASE-ONLY-LISTING')
        self.create_offer('usdc-optimism', notes='OP-ONLY-LISTING')
        self.assertEqual(self.taker.get('/p2p?payment_asset=usdt-base').status_code, 400)
        body = self.taker.get('/p2p?payment_asset=usdc-base').get_data(as_text=True)
        self.assertIn('BASE-ONLY-LISTING', body)
        self.assertNotIn('OP-ONLY-LISTING', body)

    def test_old_bob_feed_is_btc_only_and_v2_is_explicit(self):
        self.create_offer('btc-bitcoin')
        self.create_offer('usdc-base')
        old = self.taker.get('/api/channel').get_json()
        self.assertEqual(old['version'], 1)
        self.assertEqual(len(old['p2p']['offers']), 1)
        self.assertEqual(old['p2p']['offers'][0]['quote_asset'], 'BTC')
        new = self.taker.get('/api/channel?version=2').get_json()
        self.assertEqual(new['version'], 2)
        self.assertEqual(len(new['p2p']['offers']), 2)
        token = next(o for o in new['p2p']['offers'] if o['quote_asset'] == 'USDC')
        self.assertIsNone(token['price_btc_per_hns'])
        self.assertEqual(token['chain_id'], 8453)
        self.assertEqual(self.taker.get('/api/channel?version=99').status_code, 400)

    def test_large_hns_amount_does_not_round_trip_through_float(self):
        self.create_offer('eth-base', amount_hns='9007199254740991.123456', price='0.000000000000000001')
        self.taker.post('/p2p/offers/1/accept', data={'confirm_network': 'yes'})
        with self.app.app_context():
            trade = db.session.get(P2PTrade, 1)
            self.assertEqual(trade.amount_hns, Decimal('9007199254740991.123456'))
            self.assertEqual(trade.quote_total, Decimal('0.009007199254740991'))

    def test_invalid_evm_hash_does_not_partially_update_room(self):
        self.create_offer('usdc-base')
        self.taker.post('/p2p/offers/1/accept', data={'confirm_network': 'yes'})
        self.taker.post('/p2p/trades/1/update', data={'milestone': 'payment_sent', 'bob_lock_txid': 'aa' * 32})
        with self.app.app_context():
            trade = db.session.get(P2PTrade, 1)
            self.assertEqual(trade.milestone, 'matched')
            self.assertIsNone(trade.bob_lock_txid)
        self.taker.post('/p2p/trades/1/update', data={'bob_lock_txid': '0x' + 'aa' * 32})
        with self.app.app_context():
            self.assertEqual(db.session.get(P2PTrade, 1).bob_lock_txid, '0x' + 'aa' * 32)

    def test_cancel_cannot_refund_or_overwrite_a_matched_offer(self):
        from unittest.mock import patch
        self.create_offer('usdc-base')
        self.taker.post('/p2p/offers/1/accept', data={'confirm_network': 'yes'})
        with patch('routes.main.wallet_credit_gems') as refund:
            self.maker.post('/p2p/offers/1/cancel')
        refund.assert_not_called()
        with self.app.app_context():
            self.assertEqual(db.session.get(P2POffer, 1).status, 'matched')
            self.assertEqual(P2PTrade.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
