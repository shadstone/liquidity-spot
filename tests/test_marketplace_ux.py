"""Marketplace direction, privacy and owner-controlled availability; synthetic data only."""
from datetime import datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from app import create_app
from models import db, User, P2POffer, P2PTrade, P2PTradeMessage
from services.payment_assets import PAYMENT_ASSETS, make_terms_snapshot
from services.p2p_schema import ensure_payment_schema
from services.offer_sharing import build_offer_share_text


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all([User(id=name, username=name) for name in ('maker', 'other', 'PRIVATE-COUNTERPARTY')])
        db.session.commit()
        self.visitor = self.app.test_client()
        self.owner = self.login('maker')
        self.price = patch('routes.main._current_hns_btc_price', return_value=0.0000001)
        self.price.start()

    def tearDown(self):
        self.price.stop()
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self, user):
        client = self.app.test_client()
        with client.session_transaction() as state:
            state['user_id'] = user
        return client

    def offer(self, **overrides):
        values = dict(creator_id='maker', side='sell', amount_hns=1000, amount_hns_exact='1000',
            price_btc_per_hns=Decimal('0.000001'), payment_asset_id='usdt-ethereum',
            price_quote_per_hns='0.01', status='open', gems_stake=0, maker_bond_status='none')
        values.update(overrides)
        offer = P2POffer(**values)
        db.session.add(offer)
        db.session.commit()
        return offer

    def page(self, path='/p2p', client=None):
        response = (client or self.visitor).get(path)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def renew_data(self, offer, **overrides):
        self.owner.get(f'/p2p/offers/{offer.id}')
        with self.owner.session_transaction() as state:
            token = state['offer_availability_token']
        return dict(csrf_token=token, confirm_available='yes', expiry='7', **overrides)

    def test_marketplace_first_and_post_form_separate_with_no_board_acceptance(self):
        self.offer(payment_asset_id='btc-bitcoin')
        body = self.page()
        for label in ('P2P Marketplace', 'Available offers', 'My trades', 'Past listings', 'Buy HNS', 'Sell HNS', 'Review offer', 'Ask a question'):
            self.assertIn(label, body)
        tags = Elements(body).tags
        self.assertFalse(any(t == 'form' and a.get('method', '').lower() == 'post' for t, a in tags))
        self.assertNotIn('id="p2p-offer-form"', body)
        form = self.page('/p2p?view=post')
        self.assertIn('id="p2p-offer-form"', form)
        self.assertIn('name="expiry"', form)
        self.assertIn('7 days', form)

    def test_bob_manifest_is_unchanged_and_public_books_are_not_cached(self):
        manifest = self.visitor.get('/bob-addon.json')
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.get_json()['id'], 'liquidity-spot')
        for version in (1, 2):
            response = self.visitor.get(f'/api/channel?version={version}')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_buy_sell_filters_use_taker_intention_and_exact_network(self):
        seller = self.offer(side='sell', payment_asset_id='usdc-base')
        buyer = self.offer(side='buy', payment_asset_id='usdc-base')
        wrong_network = self.offer(side='sell', payment_asset_id='usdc-ethereum')
        for intent, included, excluded in [('buy', seller, buyer), ('sell', buyer, seller)]:
            body = self.page(f'/p2p?intent={intent}&payment_asset=usdc-base')
            self.assertIn(f'id="offer-{included.id}"', body)
            self.assertNotIn(f'id="offer-{excluded.id}"', body)
            self.assertNotIn(f'id="offer-{wrong_network.id}"', body)
        for query in ('intent=bogus', 'view=private', 'payment_asset=usdt-base'):
            self.assertEqual(self.visitor.get('/p2p?' + query).status_code, 400)

    def test_both_sides_and_all_assets_show_correct_owner_and_visitor_direction(self):
        for asset in PAYMENT_ASSETS.values():
            for side in ('buy', 'sell'):
                offer = self.offer(side=side, payment_asset_id=asset['id'])
                for client, receives_hns in ((self.owner, side == 'buy'), (self.visitor, side == 'sell')):
                    body = self.page(f'/p2p/offers/{offer.id}', client)
                    receive = body.split('You receive</dt>', 1)[1].split('</dd>', 1)[0]
                    pay = body.split('You pay</dt>', 1)[1].split('</dd>', 1)[0]
                    self.assertIn('1000 HNS' if receives_hns else '10 ' + asset['symbol'], receive)
                    self.assertIn('10 ' + asset['symbol'] if receives_hns else '1000 HNS', pay)
                    self.assertIn(asset['network_label'], body)
                    self.assertIn('Manual P2P · no escrow', body)

    def test_legacy_offers_remain_available_without_invented_confirmation(self):
        offer = self.offer(created_at=datetime(2025, 1, 1))
        body = self.page()
        self.assertIn(f'id="offer-{offer.id}"', body)
        self.assertIn('Availability not reconfirmed', body)
        self.assertIn('No automatic expiry set', body)
        self.assertIsNone(offer.availability_confirmed_at)
        self.assertIsNone(offer.expires_at)

    def test_expired_offer_is_not_discoverable_shareable_or_acceptable(self):
        offer = self.offer(expires_at=datetime.utcnow() - timedelta(seconds=1), payment_asset_id='btc-bitcoin')
        self.assertNotIn(f'id="offer-{offer.id}"', self.page())
        self.assertIn(f'id="offer-{offer.id}"', self.page('/p2p?view=past'))
        detail = self.page(f'/p2p/offers/{offer.id}')
        self.assertIn('Expired · unavailable', detail)
        self.assertNotIn(f'action="/p2p/offers/{offer.id}/accept"', detail)
        self.assertNotIn('id="offer-share-text"', detail)
        self.assertIsNone(build_offer_share_text(offer, 'https://example.invalid'))
        for version in (1, 2):
            rows = self.visitor.get(f'/api/channel?version={version}').get_json()['p2p']['offers']
            self.assertEqual(rows, [])
        self.visitor.post(f'/p2p/offers/{offer.id}/accept', data={'confirm_network': 'yes'})
        self.assertEqual(P2PTrade.query.count(), 0)
        db.session.refresh(offer)
        self.assertEqual(offer.status, 'open')

    def test_past_listing_does_not_leak_trade_details_or_imply_completion(self):
        offer = self.offer(status='matched')
        trade = P2PTrade(offer_id=offer.id, creator_id='maker', counterparty_id='PRIVATE-COUNTERPARTY',
            status='matched', milestone='payment_sent', latest_note='PRIVATE-NOTE', admin_notes='PRIVATE-ADMIN',
            alice_lock_txid='PRIVATE-TXID', terms_snapshot=make_terms_snapshot(offer))
        db.session.add(trade)
        db.session.flush()
        db.session.add(P2PTradeMessage(trade_id=trade.id, user_id='maker', message='PRIVATE-CHAT'))
        db.session.commit()
        for path in ('/p2p?view=past', f'/p2p/offers/{offer.id}'):
            body = self.page(path)
            self.assertNotIn('PRIVATE-', body)
            self.assertNotIn(f'/p2p/trades/{trade.id}', body)
            self.assertNotIn(f'action="/p2p/offers/{offer.id}/accept"', body)
        body = self.page('/p2p?view=past')
        self.assertIn('not proof of a completed trade', body)
        self.assertIn('Matched · unavailable', body)
        self.assertNotIn(f'id="offer-{offer.id}"', self.page())
        self.assertNotIn(f'/p2p/trades/{trade.id}', self.page('/p2p?view=mine', self.login('other')))
        self.assertIn(f'/p2p/trades/{trade.id}', self.page('/p2p?view=mine', self.owner))
        self.assertIn('no-store', self.owner.get('/p2p?view=mine').headers['Cache-Control'])

    def test_owner_can_explicitly_reconfirm_expired_offer_without_term_changes(self):
        offer = self.offer(expires_at=datetime.utcnow() - timedelta(days=1))
        original = make_terms_snapshot(offer)
        data = self.renew_data(offer)
        with patch('routes.main.wallet_credit_gems') as credit, patch('routes.main.wallet_deduct_gems') as debit:
            self.assertEqual(self.owner.post(f'/p2p/offers/{offer.id}/availability', data=data).status_code, 302)
            credit.assert_not_called()
            debit.assert_not_called()
        db.session.refresh(offer)
        self.assertEqual(make_terms_snapshot(offer), original)
        self.assertTrue(offer.is_available)
        self.assertLess(abs((offer.expires_at - datetime.utcnow()).total_seconds() - 7 * 86400), 10)
        self.assertIsNotNone(offer.availability_confirmed_at)
        self.assertEqual(P2PTrade.query.count(), 0)

    def test_reconfirm_requires_owner_csrf_explicit_choice_and_unmatched_state(self):
        offer = self.offer()
        path = f'/p2p/offers/{offer.id}/availability'
        data = self.renew_data(offer)
        self.assertEqual(self.visitor.post(path, data=data).status_code, 302)
        self.assertEqual(self.login('other').post(path, data=data).status_code, 403)
        for changed in ({'csrf_token': 'wrong'}, {'csrf_token': 'é'}, {'confirm_available': ''}, {'expiry': '-1'}, {'expiry': 'NaN'}):
            self.assertEqual(self.owner.post(path, data={**data, **changed}).status_code, 400)
        for status in ('matched', 'canceled', 'funding', 'bond_failed', 'bond_required'):
            offer.status = status
            db.session.commit()
            self.assertEqual(self.owner.post(path, data=data).status_code, 409)
        offer.status = 'open'
        db.session.add(P2PTrade(offer_id=offer.id, creator_id='maker', counterparty_id='other'))
        db.session.commit()
        self.assertEqual(self.owner.post(path, data=data).status_code, 409)

    def test_expiry_choice_creation_defaults_preserve_old_clients(self):
        data = dict(side='sell', amount_hns='100', price='0.01', payment_asset='usdt-ethereum', gems_stake='0')
        for expiry in (None, 'never', '1', '7', '30'):
            result = self.owner.post('/p2p/offers', data={**data, **({'expiry': expiry} if expiry else {})})
            self.assertEqual(result.status_code, 302)
            offer = P2POffer.query.order_by(P2POffer.id.desc()).first()
            if expiry in (None, 'never'):
                self.assertIsNone(offer.expires_at)
            else:
                self.assertEqual(offer.expires_at - offer.availability_confirmed_at, timedelta(days=int(expiry)))
        count = P2POffer.query.count()
        self.owner.post('/p2p/offers', data={**data, 'expiry': '1000'})
        self.assertEqual(P2POffer.query.count(), count)

    def test_pagination_is_bounded_and_views_do_not_mutate_state(self):
        for _ in range(25):
            self.offer()
        page1 = Elements(self.page()).tags
        self.assertEqual(sum(tag == 'article' for tag, attrs in page1), 24)
        page2 = Elements(self.page('/p2p?page=2')).tags
        self.assertEqual(sum(tag == 'article' for tag, attrs in page2), 1)
        self.assertEqual(P2POffer.query.count(), 25)
        self.assertEqual(P2PTrade.query.count(), 0)
        self.assertEqual(P2POffer.query.filter(P2POffer.availability_confirmed_at.is_not(None)).count(), 0)

    def test_atomic_label_and_unfinished_count_do_not_promise_available_liquidity(self):
        self.assertIn('Atomic swap · experimental · HNS/BTC', self.page('/orders'))
        body = self.page('/maker-mode')
        self.assertIn('Unfinished matched rooms', body)
        self.assertNotIn('Active P2P Trades', body)
        self.assertIn('not available liquidity', body)

    def test_additive_upgrade_preserves_legacy_state_and_explicit_expiry(self):
        engine = create_engine('sqlite://')
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE p2p_offers (id INTEGER PRIMARY KEY, status TEXT)'))
            conn.execute(text("INSERT INTO p2p_offers VALUES (1, 'matched'), (2, 'open')"))
        for _ in range(2):
            ensure_payment_schema(engine)
        with engine.begin() as conn:
            self.assertEqual(conn.execute(text('SELECT status, expires_at, availability_confirmed_at FROM p2p_offers ORDER BY id')).all(), [('matched', None, None), ('open', None, None)])
            conn.execute(text("UPDATE p2p_offers SET expires_at='2026-12-01 00:00:00' WHERE id=2"))
        ensure_payment_schema(engine)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT expires_at FROM p2p_offers WHERE id=2')).scalar(), '2026-12-01 00:00:00')
        columns = {col['name']: col for col in inspect(engine).get_columns('p2p_offers')}
        self.assertTrue(columns['expires_at']['nullable'])
        self.assertTrue(columns['availability_confirmed_at']['nullable'])
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
