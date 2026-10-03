"""Public offer sharing is exact, neutral, private-data-free and read-only."""
from decimal import Decimal, ROUND_HALF_UP, localcontext
from html.parser import HTMLParser
import unittest

from app import create_app
from models import db, User, P2POffer, P2PTrade, P2PTradeMessage, P2PTradeParticipantState
from services.payment_assets import PAYMENT_ASSETS
from services.trade_events import AgentTradeEvent


class ShareElements(HTMLParser):
    """Inspect actual HTML elements rather than accidentally matching script text."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements = {}
        self.share_text = None
        self._capturing = False
        self._parts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.elements[attrs['id']] = {'tag': tag, **attrs}
        if tag == 'textarea' and attrs.get('id') == 'offer-share-text':
            self._capturing = True
            self._parts = []

    def handle_endtag(self, tag):
        if self._capturing and tag == 'textarea':
            self.share_text = ''.join(self._parts)
            self._capturing = False

    def handle_data(self, data):
        if self._capturing:
            self._parts.append(data)


class OfferSharingTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all([
            User(id='private-owner-id', username='PRIVATE-CREATOR-NAME', email='private@example.invalid',
                 guest_recovery_digest='f' * 64, tier='free'),
            User(id='private-other-id', username='PRIVATE-COUNTERPARTY-NAME', tier='free'),
        ])
        db.session.commit()
        self.visitor = self.app.test_client()
        self.owner = self.app.test_client()
        with self.owner.session_transaction(base_url='https://liquidity.spot') as session:
            session['user_id'] = 'private-owner-id'
            session['token'] = 'PRIVATE-SSO-TOKEN'
            session['guest_recovery_key'] = 'PRIVATE-GUEST-RECOVERY-KEY'

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def offer(self, asset='usdc-base', side='sell', amount='1234.567891', price=None,
              status='open', notes='PRIVATE-OFFER-NOTE', **overrides):
        if price is None:
            price = '0.000000020319' if asset == 'btc-bitcoin' else '0.012345678912345678'
        offer = P2POffer(creator_id='private-owner-id', side=side, amount_hns=Decimal(amount),
            amount_hns_exact=amount, price_btc_per_hns=Decimal(price) if asset == 'btc-bitcoin' else Decimal('0'),
            price_quote_per_hns=price, payment_asset_id=asset, status=status,
            payment_method='PRIVATE-PAYMENT-METHOD', notes=notes, gems_stake=0,
            maker_bond_status='none')
        for name, value in overrides.items():
            setattr(offer, name, value)
        db.session.add(offer)
        db.session.commit()
        return offer

    def page(self, offer, client=None, **kwargs):
        response = (client or self.visitor).get(f'/p2p/offers/{offer.id}',
                                               base_url='https://liquidity.spot', **kwargs)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True), ShareElements(response.get_data(as_text=True))

    def exact_total(self, amount, price, decimals):
        with localcontext() as context:
            context.prec = 80
            value = (Decimal(amount) * Decimal(price)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
        text = format(value, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text

    def test_all_assets_and_both_sides_share_exact_terms_and_opposite_counterparty_role(self):
        self.assertEqual(len(PAYMENT_ASSETS), 8)
        for asset_id, asset in PAYMENT_ASSETS.items():
            for side in ('buy', 'sell'):
                with self.subTest(asset=asset_id, side=side):
                    offer = self.offer(asset=asset_id, side=side)
                    html, parsed = self.page(offer)
                    text = parsed.share_text
                    self.assertIsNotNone(text)
                    self.assertIn(f'Liquidity.spot offer #{offer.id}: {side.upper()} 1234.567891 HNS', text)
                    self.assertIn(f'{offer.price_quote_per_hns} {asset["symbol"]}/HNS', text)
                    total = self.exact_total(offer.amount_hns_exact, offer.price_quote_per_hns, asset['decimals'])
                    self.assertIn(f'{total} {asset["symbol"]}', text)
                    self.assertIn(asset['network_label'], text)
                    self.assertIn('HNS seller' if side == 'buy' else 'HNS buyer', text)
                    self.assertIn(f'https://liquidity.spot/p2p/offers/{offer.id}', text)
                    self.assertIn('readonly', parsed.elements['offer-share-text'])
                    self.assertEqual(parsed.elements['offer-share-text']['tag'], 'textarea')
                    self.assertIn('copy-offer-text', parsed.elements)
                    self.assertIn('share-offer-text', parsed.elements)
                    self.assertIn('Copy full message', html)
                    self.assertIn('Copy link only', html)

    def test_public_paragraph_explains_manual_risks_fees_and_does_not_promise_execution(self):
        offer = self.offer()
        _, parsed = self.page(offer)
        text = parsed.share_text
        self.assertIsNotNone(text)
        self.assertIn('manual p2p', text.lower())
        self.assertIn('no escrow', text.lower())
        self.assertIn('network fees', text.lower())
        self.assertRegex(text.lower(), r'network fees (?:extra|separately|paid separately)')
        self.assertNotIn('guaranteed', text.lower())
        self.assertNotIn('verified funds', text.lower())
        self.assertNotIn('instant settlement', text.lower())

    def test_paragraph_excludes_names_notes_credentials_and_private_room_links(self):
        malicious_note = '</textarea><script>alert("PRIVATE-NOTE")</script> PRIVATE-OFFER-NOTE /p2p/trades/991'
        offer = self.offer(notes=malicious_note)
        html, parsed = self.page(offer, self.owner)
        text = parsed.share_text
        self.assertIsNotNone(text)
        for private in ('PRIVATE-', 'private-owner-id', 'private-other-id', 'private@example.invalid',
                        'f' * 64, '/p2p/trades/', 'alert(', '<script', '</textarea>'):
            self.assertNotIn(private, text)
        self.assertNotIn(malicious_note, html)
        self.assertIn('&lt;/textarea&gt;&lt;script&gt;', html)
        self.assertEqual(parsed.elements['offer-share-url']['value'],
                         f'https://liquidity.spot/p2p/offers/{offer.id}')

    def test_anonymous_and_owner_get_the_same_neutral_public_paragraph(self):
        offer = self.offer(side='buy')
        owner_html, owner_page = self.page(offer, self.owner)
        visitor_html, visitor_page = self.page(offer, self.visitor)
        self.assertIsNotNone(owner_page.share_text)
        self.assertEqual(owner_page.share_text, visitor_page.share_text)
        self.assertIn('Your offer is live', owner_html)
        self.assertNotIn('Your offer is live', visitor_html)
        self.assertNotIn('I want', owner_page.share_text)
        self.assertNotIn('My offer', owner_page.share_text)

    def test_closed_or_unfunded_statuses_only_expose_reference_link(self):
        for status in ('matched', 'canceled', 'funding', 'bond_failed', 'bond_required'):
            with self.subTest(status=status):
                offer = self.offer(status=status)
                for client in (self.visitor, self.owner):
                    html, parsed = self.page(offer, client)
                    self.assertIsNone(parsed.share_text)
                    for identity in ('offer-share-text', 'copy-offer-text', 'share-offer-text'):
                        self.assertNotIn(identity, parsed.elements)
                    self.assertIn('offer-share-url', parsed.elements)
                    self.assertIn('copy-offer-link', parsed.elements)
                    self.assertIn('Copy link only', html)
                    self.assertIn('no longer open', html.lower())

    def test_share_link_drops_request_query_and_forces_production_https(self):
        offer = self.offer()
        response = self.visitor.get(f'/p2p/offers/{offer.id}?recovery=PRIVATE-QUERY&room=991',
                                    base_url='http://liquidity.spot')
        self.assertEqual(response.status_code, 200)
        parsed = ShareElements(response.get_data(as_text=True))
        self.assertIsNotNone(parsed.share_text)
        expected = f'https://liquidity.spot/p2p/offers/{offer.id}'
        self.assertEqual(parsed.elements['offer-share-url']['value'], expected)
        self.assertIn(expected, parsed.share_text)
        self.assertNotIn('PRIVATE-QUERY', parsed.share_text)
        self.assertNotIn('?recovery=', parsed.share_text)
        self.assertNotIn('room=991', parsed.share_text)

    def test_get_sharing_never_creates_users_trades_messages_events_or_modifies_offer(self):
        offer = self.offer()
        before = {column.name: getattr(offer, column.name) for column in P2POffer.__table__.columns}
        models = (User, P2POffer, P2PTrade, P2PTradeMessage, P2PTradeParticipantState, AgentTradeEvent)
        counts = {model.__tablename__: model.query.count() for model in models}
        for client in (self.visitor, self.owner):
            self.page(offer, client)
        db.session.expire_all()
        after = db.session.get(P2POffer, offer.id)
        self.assertEqual({column.name: getattr(after, column.name) for column in P2POffer.__table__.columns}, before)
        self.assertEqual({model.__tablename__: model.query.count() for model in models}, counts)
        with self.visitor.session_transaction(base_url='https://liquidity.spot') as session:
            self.assertNotIn('user_id', session)

    def test_smallest_eth_unit_and_usdc_rounding_are_not_converted_to_float_or_exponent(self):
        for asset, amount, price, total in (
            ('eth-base', '1', '0.000000000000000001', '0.000000000000000001'),
            ('usdc-optimism', '1', '0.0000005', '0.000001'),
            ('btc-bitcoin', '4444', '0.000000020319', '0.0000903'),
        ):
            with self.subTest(asset=asset):
                offer = self.offer(asset=asset, amount=amount, price=price)
                _, parsed = self.page(offer)
                self.assertIsNotNone(parsed.share_text)
                symbol = PAYMENT_ASSETS[asset]['symbol']
                self.assertIn(f'{price} {symbol}/HNS', parsed.share_text)
                self.assertIn(f'{total} {symbol}', parsed.share_text)
                self.assertNotRegex(parsed.share_text, r'\d[eE][+-]\d')


if __name__ == '__main__':
    unittest.main()
