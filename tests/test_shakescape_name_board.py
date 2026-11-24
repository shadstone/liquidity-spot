import json
import time
import unittest
from unittest.mock import patch

from app import create_app
from models import Order, db
from services import shakescape_name_board


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
        'revision': 'aa' * 32,
        'generated_at': now,
        'expires_at': now + 20,
        'offers': offers if offers is not None else [{
            'offer_id': 'ab' * 32,
            'seller_public_key': '02' + 'cd' * 32,
            'name': 'example_name',
            'price_base_units': '1000000',
            'marketplace_fee_base_units': '50000',
            'created_at': now - 1,
            'expires_at': now + 100,
        }],
    }


class ShakescapeNameBoardTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
        with shakescape_name_board._cache_lock:
            shakescape_name_board._cache_key = None
            shakescape_name_board._cache_until = 0
            shakescape_name_board._cache_value = None

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_signed_advertisement_projection_does_not_create_an_order(self):
        now = int(time.time())
        self.app.config.update(
            SHAKESCAPE_NAME_BOARD_FEED_URL='https://feed.example/name-offers',
            SHAKESCAPE_NAME_OFFER_URI_TEMPLATE='shakescape://name-offer/v1/{offer_id}?network={network}',
        )
        with patch('services.shakescape_board.http_get',
                   return_value=FeedResponse(snapshot(now))):
            result = self.client.get('/api/external-offers/shakescape/names')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        offer = result.json['offers'][0]
        self.assertEqual(offer['name'], 'example_name')
        self.assertEqual(offer['price_hns'], '1')
        self.assertEqual(offer['marketplace_fee_hns'], '0.05')
        self.assertEqual(offer['offer_uri'],
                         'shakescape://name-offer/v1/' + 'ab' * 32 + '?network=regtest')
        with self.app.app_context():
            self.assertEqual(Order.query.count(), 0)
        page = self.client.get('/orders').get_data(as_text=True)
        self.assertIn('shakescape-name-offer-rows', page)
        self.assertIn('Atomic Swap Order Book', page)

    def test_stale_duplicate_and_bad_name_feed_fail_closed(self):
        now = int(time.time())
        good = snapshot(now)
        malformed = [{**good, 'network': 'mainnet'},
                     {**good, 'generated_at': now - 31},
                     snapshot(now, good['offers'] * 2),
                     snapshot(now, [{**good['offers'][0], 'name': '<script>'}]),
                     snapshot(now, [{**good['offers'][0], 'offer_id': '0' * 64}])]
        for candidate in malformed:
            with self.subTest(candidate=candidate), self.assertRaises(
                    shakescape_name_board.BoardUnavailable):
                shakescape_name_board._normalize(candidate, 'regtest', '', now)
        with self.assertRaises(shakescape_name_board.BoardUnavailable):
            shakescape_name_board._normalize(
                {**good, 'network': 'unsupported'}, 'unsupported',
                shakescape_name_board.CANONICAL_NAME_OFFER_URI_TEMPLATE, now)

    def test_name_link_uses_only_the_app_recipient_contract(self):
        now = int(time.time())
        data = snapshot(now)
        canonical = 'shakescape://name-offer/v1/{offer_id}?network={network}'
        offer = shakescape_name_board._normalize(data, 'regtest', canonical, now)['offers'][0]
        self.assertEqual(offer['offer_uri'],
                         'shakescape://name-offer/v1/' + 'ab' * 32 + '?network=regtest')
        for template in ('', 'shakescape://other/v1/{offer_id}?network={network}',
                         'https://example.com/{offer_id}?network={network}'):
            with self.subTest(template=template):
                disabled = shakescape_name_board._normalize(data, 'regtest', template, now)
                self.assertIsNone(disabled['offers'][0]['offer_uri'])

    def test_missing_name_feed_leaves_order_book_available(self):
        result = self.client.get('/api/external-offers/shakescape/names')
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json['offers'], [])
        self.assertIn('Create Atomic Swap Order',
                      self.client.get('/orders').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
