import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts.poll_trade_events import (PollError, NoRedirects, acknowledge, fetch_events,
                                       fetch_step, validate_base_url, validate_batch)


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state = Path(self.folder.name) / 'private' / 'events.json'
        self.base = 'https://liquidity.spot'
        self.batch = {'stream_id': 'owner-A', 'events': [{'id': 2, 'type': 'p2p.offer_accepted'}],
                      'next_cursor': 2, 'has_more': False}

    def fetch(self, batch=None):
        with patch('scripts.poll_trade_events.fetch_events', return_value=batch or self.batch):
            return fetch_step(self.state, self.base, 'private-test-token')

    def test_fetch_does_not_ack_and_replays_until_handled(self):
        first = self.fetch()
        self.assertTrue(first['ack_required'])
        self.assertEqual(json.loads(self.state.read_text())['cursor'], 0)
        second = self.fetch({**self.batch, 'events': [{'id': 2}, {'id': 3}], 'next_cursor': 3})
        self.assertEqual(second['events'], first['events'])
        self.assertEqual(acknowledge(self.state, self.base, 2), {'acknowledged_cursor': 2})
        self.assertEqual(json.loads(self.state.read_text())['cursor'], 2)
        self.assertNotIn('private-test-token', self.state.read_text())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_bad_ack_cannot_skip_events(self):
        self.fetch()
        before = self.state.read_text()
        with self.assertRaises(PollError):
            acknowledge(self.state, self.base, 100)
        self.assertEqual(self.state.read_text(), before)

    def test_wrong_owner_and_denied_access_cannot_replay_pending(self):
        self.fetch()
        before = self.state.read_text()
        with self.assertRaises(PollError):
            self.fetch({**self.batch, 'stream_id': 'another-owner'})
        with patch('scripts.poll_trade_events.fetch_events', side_effect=PollError('denied')):
            with self.assertRaises(PollError):
                fetch_step(self.state, self.base, 'revoked')
        self.assertEqual(self.state.read_text(), before)

    def test_invalid_response_order_and_cursor_fail(self):
        bad = [[], {**self.batch, 'next_cursor': 900},
               {**self.batch, 'events': [{'id': 2}, {'id': 1}], 'next_cursor': 1},
               {**self.batch, 'events': [], 'next_cursor': 0, 'has_more': True},
               {**self.batch, 'next_cursor': True}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(PollError):
                validate_batch(data, 0)

    def test_endpoint_allowlist_and_redirect_denial(self):
        self.assertEqual(validate_base_url('http://127.0.0.1:8765/'), 'http://127.0.0.1:8765')
        for url in ['http://liquidity.spot', 'https://evil.example', 'https://liquidity.spot.evil.example',
                    'https://user:password@liquidity.spot', 'https://liquidity.spot?token=x',
                    'http://[broken', 'https://liquidity.spot:invalid', 'http://localhost:99999']:
            with self.subTest(url=url), self.assertRaises(PollError):
                validate_base_url(url)
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, '', {}, 'https://evil.example'))

    def test_remote_error_does_not_leak_body_or_url(self):
        with patch('scripts.poll_trade_events.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError('https://private-url.example', 401, 'secret message', {}, None)
            with self.assertRaises(PollError) as error:
                fetch_events(self.base, 'private-test-token', 0)
        self.assertNotIn('secret', str(error.exception))
        self.assertNotIn('private-url', str(error.exception))

    def test_invalid_state_is_not_overwritten(self):
        self.state.parent.mkdir()
        self.state.write_text('unrelated file')
        with self.assertRaises(PollError):
            self.fetch()
        self.assertEqual(self.state.read_text(), 'unrelated file')


if __name__ == '__main__':
    unittest.main()
