"""Posting/review, comparable offers and access telemetry, with synthetic data only."""
from datetime import datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
import re
import unittest
from unittest.mock import patch

from app import create_app
from models import db, User, P2POffer, P2PTrade
from services.agent_workspace import AgentConnection, issue_connection
from services.payment_assets import PAYMENT_ASSETS, parse_offer_amounts, format_decimal


class Fields(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.hidden = {}
        self.attrs = {}
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.attrs[attrs['id']] = attrs
        if tag == 'input' and attrs.get('type') == 'hidden':
            self.hidden[attrs['name']] = attrs.get('value', '')


class UXFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all([User(id='owner', username='Owner'), User(id='other', username='Other')])
        db.session.commit()
        self.client = self.app.test_client()
        self.owner = self.app.test_client()
        with self.owner.session_transaction() as state:
            state['user_id'] = 'owner'
        self.price = patch('routes.main._current_hns_btc_price', return_value=0.0000001)
        self.price.start()

    def tearDown(self):
        self.price.stop()
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def fields(self, **extra):
        return dict(side='sell', payment_asset='usdc-base', amount_hns='1000.000001',
                    price='0.123456789012345678', expiry='30', gems_stake='0',
                    payment_method='My wallet', notes='Line one\n<script>not code</script>',
                    inquiry_setting_present='yes', _form_action='review', **extra)

    def offer(self, **extra):
        values = dict(creator_id='owner', side='sell', payment_asset_id='usdc-base',
                      amount_hns=Decimal('1000'), amount_hns_exact='1000',
                      price_btc_per_hns=Decimal('0'), price_quote_per_hns='2', status='open',
                      gems_stake=0, created_at=datetime(2026, 1, 1))
        values.update(extra)
        offer = P2POffer(**values)
        db.session.add(offer)
        db.session.commit()
        return offer

    def ids(self, response):
        self.assertEqual(response.status_code, 200)
        return [int(i) for i in re.findall(r'<article id="offer-(\d+)"', response.get_data(as_text=True))]

    def test_review_all_routes_and_sides_is_exact_and_has_no_writes(self):
        with patch('routes.main.wallet_deduct_gems') as wallet, patch('routes.main._ensure_session_user') as user:
            for asset in PAYMENT_ASSETS.values():
                for side in ('buy', 'sell'):
                    fields = {**self.fields(), 'payment_asset': asset['id'], 'side': side,
                              'price': '0.00000001' if asset['id'] == 'btc-bitcoin' else '0.123456789012345678'}
                    response = self.client.post('/p2p/offers', data=fields)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('no-store', response.headers['Cache-Control'])
                    body = response.get_data(as_text=True).split('id="publish-review"')[1]
                    total = format_decimal(parse_offer_amounts(fields['amount_hns'], fields['price'], asset['id'])[2])
                    self.assertIn(total + ' ' + asset['symbol'], body)
                    send = body.split('You send</dt>')[1].split('</dd>')[0]
                    self.assertIn(fields['amount_hns'] + ' HNS' if side == 'sell' else total + ' ' + asset['symbol'], send)
                    self.assertIn(asset['network_label'], body)
                    self.assertIn('Private questions before acceptance</dt><dd>Off', body)
                    self.assertEqual(Fields(body).hidden, {k: v for k, v in fields.items() if k != '_form_action'})
                    self.assertNotIn('<script>not code</script>', body)
                    self.assertNotIn('Set-Cookie', response.headers)
            wallet.assert_not_called()
            user.assert_not_called()
        self.assertEqual(User.query.count(), 2)
        self.assertEqual(P2POffer.query.count(), 0)
        self.assertEqual(P2PTrade.query.count(), 0)

    def test_no_javascript_review_edit_publish_preserves_terms(self):
        fields = self.fields()
        review = self.client.post('/p2p/offers', data=fields)
        hidden = Fields(review.get_data(as_text=True)).hidden
        edit = self.client.post('/p2p/offers', data={**hidden, '_form_action': 'edit'})
        body = edit.get_data(as_text=True)
        self.assertEqual(edit.status_code, 200)
        self.assertEqual(Fields(body).attrs['p2p-price']['value'], fields['price'])
        self.assertNotIn('checked', Fields(body).attrs['p2p-allow-pretrade-chat'])
        self.assertIn('&lt;script&gt;not code&lt;/script&gt;', body)
        self.assertEqual(P2POffer.query.count(), 0)
        publish = self.client.post('/p2p/offers', data={**hidden, '_form_action': 'publish'})
        self.assertEqual(publish.status_code, 302)
        offer = P2POffer.query.one()
        self.assertEqual(offer.price_quote_per_hns, fields['price'])
        self.assertEqual(offer.amount_hns_exact, fields['amount_hns'])
        self.assertEqual(offer.notes, fields['notes'])
        self.assertFalse(offer.allow_pretrade_chat)
        self.assertEqual(offer.expires_at - offer.availability_confirmed_at, timedelta(days=30))
        self.assertTrue(publish.location.endswith(f'/p2p/offers/{offer.id}'))

    def test_validation_failure_preserves_fields_and_publish_revalidates(self):
        for action in ('review', 'publish'):
            data = {**self.fields(), '_form_action': action, 'price': '1e-8'}
            response = self.client.post('/p2p/offers', data=data)
            self.assertEqual(response.status_code, 400)
            body = response.get_data(as_text=True)
            parsed = Fields(body)
            for field, element in [('price', 'p2p-price'), ('amount_hns', 'p2p-amount'), ('payment_method', 'p2p-payment-method')]:
                self.assertEqual(parsed.attrs[element]['value'], data[field])
            self.assertIn('value="usdc-base" selected', body)
            self.assertIn('value="30" selected', body)
            self.assertIn('value="sell" selected', body)
            self.assertNotIn('checked', parsed.attrs['p2p-allow-pretrade-chat'])
            self.assertIn('role="alert"', body)
            self.assertEqual(P2POffer.query.count(), 0)
            self.assertNotIn('user_id', self.client.get('/p2p').headers.get('Set-Cookie', ''))

    def test_bond_review_does_not_lock_or_publish(self):
        with patch('routes.main.wallet_deduct_gems') as wallet:
            response = self.owner.post('/p2p/offers', data={**self.fields(), 'gems_stake': '25'})
            self.assertEqual(response.status_code, 200)
            body = response.get_data(as_text=True)
            self.assertIn('25 Gems will be locked when you publish', body)
            self.assertIn('Lock 25 Gems &amp; publish offer', body)
            wallet.assert_not_called()
        self.assertEqual(P2POffer.query.count(), 0)

    def test_filter_range_and_numeric_sort_are_scoped_to_side_and_network(self):
        low = self.offer(price_quote_per_hns='2')
        high = self.offer(price_quote_per_hns='10')
        self.offer(payment_asset_id='usdc-ethereum', price_quote_per_hns='1')
        self.offer(side='buy', price_quote_per_hns='1')
        self.offer(amount_hns_exact='1000.000001')
        self.offer(amount_hns_exact='999.999999')
        self.offer(status='matched')
        query = '/p2p?intent=buy&payment_asset=usdc-base&min_hns=1000&max_hns=1000&sort='
        self.assertEqual(self.ids(self.client.get(query + 'rate_asc')), [low.id, high.id])
        self.assertEqual(self.ids(self.client.get(query + 'rate_desc')), [high.id, low.id])

    def test_rate_sort_falls_back_for_mixed_routes_or_sides(self):
        first = self.offer(price_quote_per_hns='1')
        second = self.offer(price_quote_per_hns='2')
        for query in ('sort=rate_asc', 'sort=rate_asc&intent=buy', 'sort=rate_desc&payment_asset=usdc-base'):
            response = self.client.get('/p2p?' + query)
            self.assertEqual(self.ids(response), [second.id, first.id])
            self.assertIn('Showing newest first', response.get_data(as_text=True))

    def test_sort_legacy_btc_exact_rates_pagination_and_no_record_changes(self):
        for i in range(26):
            self.offer(payment_asset_id=None, price_quote_per_hns=None,
                       price_btc_per_hns=Decimal('0.00000001'), amount_hns_exact=None)
        query = '/p2p?intent=buy&payment_asset=btc-bitcoin&min_hns=999&max_hns=1001&sort=rate_asc'
        first = self.client.get(query)
        self.assertEqual(self.ids(first), list(range(26, 2, -1)))
        body = first.get_data(as_text=True)
        for value in ('min_hns=999', 'max_hns=1001', 'sort=rate_asc'):
            self.assertIn(value, body)
        self.assertEqual(self.ids(self.client.get(query + '&page=2')), [2, 1])
        self.assertEqual(P2POffer.query.count(), 26)
        self.assertEqual(P2PTrade.query.count(), 0)

    def test_bad_filters_keep_form_and_never_return_listings(self):
        self.offer()
        for query in ('min_hns=NaN', 'min_hns=1e3', 'min_hns=-1', 'max_hns=0.0000001',
                      'min_hns=2&max_hns=1', 'sort=price', 'max_hns=10000000000000000'):
            response = self.client.get('/p2p?' + query)
            self.assertEqual(response.status_code, 400)
            body = response.get_data(as_text=True)
            self.assertIn('id="min-hns"', body)
            self.assertIn('role="alert"', body)
            self.assertNotIn('<article id="offer-', body)

    def test_navigation_accessibility_and_quiet_anonymous_board(self):
        body = self.client.get('/p2p').get_data(as_text=True)
        fields = Fields(body)
        button = fields.attrs['mobile-menu-btn']
        self.assertEqual(button['aria-controls'], 'mobile-menu')
        self.assertEqual(button['aria-expanded'], 'false')
        self.assertEqual(button['aria-label'], 'Open navigation')
        self.assertNotIn('hidden', fields.attrs['mobile-menu'])  # no-JS fallback
        self.assertNotIn('trade-notice-stack', fields.attrs)
        self.assertIn('Skip to content', body)
        self.assertIn('Maker Mode · preview', body)
        self.assertNotIn('View Atomic Swaps', body)

    def test_assistant_activity_is_recorded_usage_not_approval_or_human_visits(self):
        connection, token = issue_connection('owner', 'Owner bot', profile='trade-assistant')
        other, _ = issue_connection('other', 'PRIVATE OTHER BOT', profile='trade-assistant')
        db.session.commit()
        before = self.owner.get('/agents').get_data(as_text=True)
        self.assertIn('No API activity recorded yet', before)
        self.assertIn('Access approved', before)
        self.assertIn('Cannot publish offers with this connection.', before)
        self.assertIn('not a heartbeat', before)
        self.assertNotIn(other.label, before)
        self.assertIsNone(connection.last_used_at)
        response = self.client.get('/api/agent/v1/events', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 200)
        db.session.refresh(connection)
        recorded = connection.last_used_at
        self.assertIsNotNone(recorded)
        after = self.owner.get('/agents').get_data(as_text=True)
        self.assertIn(recorded.strftime('%Y-%m-%d %H:%M:%S') + ' UTC', after)
        db.session.refresh(connection)
        self.assertEqual(connection.last_used_at, recorded)
        connection.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertIn('Expired', self.owner.get('/agents').get_data(as_text=True))
        self.assertEqual(self.client.get('/agents').status_code, 302)
        self.assertEqual(P2POffer.query.count(), 0)
        self.assertEqual(AgentConnection.query.count(), 2)


if __name__ == '__main__':
    unittest.main()
