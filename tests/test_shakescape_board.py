import json
import time
import unittest
from unittest.mock import patch

from requests import ConnectionError as RequestsConnectionError

from app import create_app
from models import Order, db
from services import shakescape_board


OFFER_ID = 'ab' * 32
SIGNER = '02' + 'cd' * 32


class FeedResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def iter_content(self, chunk_size):
        yield self.payload

    def close(self):
        pass


def snapshot(now, offers=None):
    return {
        'schema_version': 1,
        'network': 'regtest',
        'revision': 'rev-1',
        'generated_at': now,
        'expires_at': now + 20,
        'offers': offers if offers is not None else [
            {
                'offer_id': OFFER_ID,
                'signer_public_key': SIGNER,
                'offered_asset': 'HNS',
                'offered_amount_base_units': '100000000',
                'received_asset': 'BTC',
                'received_amount_base_units': '50000',
                'created_at': now - 1,
                'expires_at': now + 100,
            },
        ],
    }


class ShakescapeBoardTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
        with shakescape_board._cache_lock:
            shakescape_board._cache_key = None
            shakescape_board._cache_until = 0
            shakescape_board._cache_value = None

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_external_feed_normalizes_exact_values_and_never_creates_local_order(self):
        now = int(time.time())
        self.app.config.update(
            SHAKESCAPE_BOARD_FEED_URL='https://feed.example/offers',
            SHAKESCAPE_OFFER_URI_TEMPLATE='shakescape://offer/{offer_id}?network={network}',
        )
        with patch('services.shakescape_board.http_get', return_value=FeedResponse(snapshot(now))):
            result = self.client.get('/api/external-offers/shakescape')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        offer = result.json['offers'][0]
        self.assertEqual(offer['side'], 'sell')
        self.assertEqual(offer['amount_hns'], '100')
        self.assertEqual(offer['total_btc'], '0.0005')
        self.assertEqual(offer['price_btc_per_hns'], '0.000005')
        self.assertEqual(offer['offer_uri'], f'shakescape://offer/{OFFER_ID}?network=regtest')
        with self.app.app_context():
            self.assertEqual(Order.query.count(), 0)
        page = self.client.get('/orders').get_data(as_text=True)
        self.assertIn('Atomic Swap Order Book', page)
        self.assertIn('shakescape-offer-rows', page)
        self.assertIn('No open Liquidity Spot orders available.', page)

    def test_buy_side_and_no_uri_until_recipient_supports_it(self):
        now = int(time.time())
        item = snapshot(now)['offers'][0]
        item.update(
            offered_asset='BTC', offered_amount_base_units='75000',
            received_asset='HNS', received_amount_base_units='25000000',
        )
        value = shakescape_board._normalize(snapshot(now, [item]), 'regtest', '', now)
        self.assertEqual(value['offers'][0]['side'], 'buy')
        self.assertEqual(value['offers'][0]['amount_hns'], '25')
        self.assertEqual(value['offers'][0]['total_btc'], '0.00075')
        self.assertIsNone(value['offers'][0]['offer_uri'])

    def test_stale_duplicate_and_wrong_network_feeds_fail_closed(self):
        now = int(time.time())
        for candidate in (
            {**snapshot(now), 'network': 'mainnet'},
            {**snapshot(now), 'generated_at': now - 31},
            snapshot(now, snapshot(now)['offers'] * 2),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(shakescape_board.BoardUnavailable):
                shakescape_board._normalize(candidate, 'regtest', '', now)

    def test_unconfigured_feed_does_not_block_local_order_book(self):
        result = self.client.get('/api/external-offers/shakescape')
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json['offers'], [])
        self.assertIn('Create Atomic Swap Order', self.client.get('/orders').get_data(as_text=True))

    def test_loopback_http_is_accepted_but_remote_plain_http_is_rejected(self):
        now = int(time.time())
        with patch('services.shakescape_board.http_get', return_value=FeedResponse(snapshot(now))):
            local = shakescape_board.board_snapshot(
                'http://127.0.0.1:12039/api/v1/shakescape/direct-offers',
                'regtest', '', now,
            )
        self.assertEqual(len(local['offers']), 1)
        with self.assertRaises(shakescape_board.BoardUnavailable):
            shakescape_board.board_snapshot('http://198.51.100.10/offers', 'regtest', '', now)

    def test_expired_cached_offer_is_not_served_when_relay_is_unreachable(self):
        now = int(time.time())
        feed_url = 'http://127.0.0.1:12039/api/v1/shakescape/direct-offers'
        with patch('services.shakescape_board.http_get', return_value=FeedResponse(snapshot(now))):
            first = shakescape_board.board_snapshot(feed_url, 'regtest', '', now)
        self.assertEqual(len(first['offers']), 1)
        with patch('services.shakescape_board.http_get', side_effect=RequestsConnectionError):
            with self.assertRaises(shakescape_board.BoardUnavailable):
                shakescape_board.board_snapshot(feed_url, 'regtest', '', now + 21)


if __name__ == '__main__':
    unittest.main()
