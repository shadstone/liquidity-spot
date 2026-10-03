import json
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from scripts.poll_trade_events import (PollError, NoRedirects, acknowledge, fetch_events,
                                       fetch_step, main, validate_base_url, validate_batch)


LEGACY_TOKEN = 'ls_agent_' + 'b' * 43
SSO_TOKEN = 'gfavip-session-' + 'a' * 64
CONNECTION_ID = '174321965'


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
                fetch_events(self.base, LEGACY_TOKEN, 0)
        self.assertNotIn('secret', str(error.exception))
        self.assertNotIn('private-url', str(error.exception))

    def test_invalid_state_is_not_overwritten(self):
        self.state.parent.mkdir()
        self.state.write_text('unrelated file')
        with self.assertRaises(PollError):
            self.fetch()
        self.assertEqual(self.state.read_text(), 'unrelated file')

    def http_response(self, opener, batch=None):
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            self.batch if batch is None else batch).encode()

    def run_cli(self, environment):
        stdout, stderr = StringIO(), StringIO()
        with patch.dict('os.environ', environment, clear=True), \
                patch('sys.argv', ['poll_trade_events.py', 'fetch', '--state', str(self.state)]), \
                patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            status = main()
        return status, stdout.getvalue(), stderr.getvalue()

    def test_sso_connection_and_token_are_headers_only(self):
        with patch('scripts.poll_trade_events.build_opener') as opener:
            self.http_response(opener)
            self.assertEqual(fetch_events(self.base, SSO_TOKEN, 0, CONNECTION_ID), self.batch)
        request = opener.return_value.open.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers, {'authorization': 'Bearer ' + SSO_TOKEN,
                                   'accept': 'application/json', 'x-liquidity-connection': CONNECTION_ID})
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'after': ['0'], 'limit': ['50']})
        self.assertNotIn(SSO_TOKEN, request.full_url)
        self.assertNotIn(CONNECTION_ID, request.full_url)
        self.assertIsInstance(opener.call_args.args[0], NoRedirects)

    def test_legacy_headers_are_unchanged_without_connection_selection(self):
        with patch('scripts.poll_trade_events.build_opener') as opener:
            self.http_response(opener)
            fetch_events(self.base, LEGACY_TOKEN, 0)
        request = opener.return_value.open.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers, {'authorization': 'Bearer ' + LEGACY_TOKEN, 'accept': 'application/json'})
        self.assertNotIn(LEGACY_TOKEN, request.full_url)

    def test_sso_fetch_never_saves_or_outputs_token_or_connection_id(self):
        with patch('scripts.poll_trade_events.build_opener') as opener:
            self.http_response(opener)
            status, output, error = self.run_cli({'LIQUIDITY_GFAVIP_SSO_TOKEN': SSO_TOKEN,
                                                 'LIQUIDITY_AGENT_CONNECTION_ID': CONNECTION_ID})
        self.assertEqual(status, 0)
        self.assertEqual(error, '')
        self.assertTrue(json.loads(output)['ack_required'])
        saved = self.state.read_text()
        self.assertEqual(json.loads(saved)['cursor'], 0)
        for private_value in (SSO_TOKEN, CONNECTION_ID, 'LIQUIDITY_GFAVIP_SSO_TOKEN',
                              'LIQUIDITY_AGENT_CONNECTION_ID', 'X-Liquidity-Connection'):
            self.assertNotIn(private_value, saved + output)
        self.assertEqual(set(json.loads(saved)), {'version', 'base_url', 'stream_id', 'cursor', 'pending'})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_sso_missing_or_malformed_connection_fails_before_http(self):
        for connection_id in (None, '', '0', '01', '1.0', '+1', ' 1', '1 ', '-1', '2147483648',
                              '1\r\nX-Injected: yes', 1, True):
            with self.subTest(connection_type=type(connection_id).__name__), \
                    patch('scripts.poll_trade_events.build_opener') as opener:
                with self.assertRaises(PollError) as caught:
                    fetch_events(self.base, SSO_TOKEN, 0, connection_id)
                self.assertNotIn(SSO_TOKEN, str(caught.exception))
                opener.assert_not_called()

    def test_malformed_credentials_and_mixed_direct_modes_fail_before_http(self):
        cases = [(None, None), ('', None), (123, None), (True, None),
                 ('private-test-token', None), ('pl_private_key', None),
                 ('ls_agent_short', None), (LEGACY_TOKEN + 'a', None),
                 ('gfavip-session-short', '1'), ('gfavip-session-' + 'a' * 201, '1'),
                 (SSO_TOKEN + '\n', '1'), (SSO_TOKEN + '?owner=x', '1'),
                 (LEGACY_TOKEN, CONNECTION_ID), (LEGACY_TOKEN, '')]
        for token, connection_id in cases:
            with self.subTest(token_type=type(token).__name__), \
                    patch('scripts.poll_trade_events.build_opener') as opener:
                with self.assertRaises(PollError):
                    fetch_events(self.base, token, 0, connection_id)
                opener.assert_not_called()

    def test_cli_auth_modes_must_match_selected_environment_and_fail_without_state(self):
        cases = [{}, {'LIQUIDITY_AGENT_CONNECTION_ID': CONNECTION_ID},
                 {'LIQUIDITY_GFAVIP_SSO_TOKEN': SSO_TOKEN},
                 {'LIQUIDITY_GFAVIP_SSO_TOKEN': LEGACY_TOKEN},
                 {'LIQUIDITY_GFAVIP_SSO_TOKEN': LEGACY_TOKEN, 'LIQUIDITY_AGENT_CONNECTION_ID': CONNECTION_ID},
                 {'LIQUIDITY_AGENT_TOKEN': SSO_TOKEN},
                 {'LIQUIDITY_AGENT_TOKEN': SSO_TOKEN, 'LIQUIDITY_AGENT_CONNECTION_ID': CONNECTION_ID},
                 {'LIQUIDITY_GFAVIP_SSO_TOKEN': SSO_TOKEN, 'LIQUIDITY_AGENT_TOKEN': LEGACY_TOKEN,
                  'LIQUIDITY_AGENT_CONNECTION_ID': CONNECTION_ID},
                 {'LIQUIDITY_AGENT_TOKEN': LEGACY_TOKEN, 'LIQUIDITY_AGENT_CONNECTION_ID': CONNECTION_ID},
                 {'LIQUIDITY_GFAVIP_SSO_TOKEN': 'pl_private_key', 'LIQUIDITY_AGENT_CONNECTION_ID': CONNECTION_ID}]
        for environment in cases:
            with self.subTest(keys=sorted(environment)), patch('scripts.poll_trade_events.build_opener') as opener:
                status, output, error = self.run_cli(environment)
                self.assertEqual(status, 1)
                self.assertEqual(output, '')
                self.assertTrue(error)
                self.assertNotIn(SSO_TOKEN, error)
                self.assertNotIn(LEGACY_TOKEN, error)
                self.assertNotIn(CONNECTION_ID, error)
                self.assertFalse(self.state.exists())
                opener.assert_not_called()

    def test_cli_legacy_mode_retains_fetch_behavior(self):
        with patch('scripts.poll_trade_events.build_opener') as opener:
            self.http_response(opener)
            status, output, error = self.run_cli({'LIQUIDITY_AGENT_TOKEN': LEGACY_TOKEN})
        self.assertEqual(status, 0)
        self.assertEqual(error, '')
        self.assertEqual(json.loads(output)['events'], self.batch['events'])
        self.assertEqual(json.loads(self.state.read_text())['cursor'], 0)
        headers = dict(opener.return_value.open.call_args.args[0].header_items())
        self.assertNotIn('X-liquidity-connection', headers)
        self.assertNotIn(LEGACY_TOKEN, self.state.read_text() + output)

    def test_denied_sso_does_not_replay_or_change_pending_batch(self):
        self.fetch()
        saved = self.state.read_text()
        with patch('scripts.poll_trade_events.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError('https://private.example/' + SSO_TOKEN,
                403, 'private upstream body ' + CONNECTION_ID, {}, None)
            status, output, error = self.run_cli({'LIQUIDITY_GFAVIP_SSO_TOKEN': SSO_TOKEN,
                                                 'LIQUIDITY_AGENT_CONNECTION_ID': CONNECTION_ID})
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertEqual(self.state.read_text(), saved)
        for private_value in (SSO_TOKEN, CONNECTION_ID, 'private.example', 'private upstream body'):
            self.assertNotIn(private_value, error)


if __name__ == '__main__':
    unittest.main()
