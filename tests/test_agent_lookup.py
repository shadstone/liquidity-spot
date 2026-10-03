"""Isolated exact-name lookup tests: no real keys, accounts, HTTP or grants."""
from unittest import TestCase
from unittest.mock import Mock, patch

from flask import Flask
import requests

from services.agent_lookup import WALLET_LOOKUP_URL, lookup_agent_username, lookup_available
from services.agent_workspace import WorkspaceError


AGENT_ID = 'abcdefab-1234-4000-8000-abcdef123456'
NAME = 'pl-test_helper'
KEY = 'synthetic-lookup-service-key'
FALLBACK_KEY = 'synthetic-wallet-service-key'


class AgentLookupTests(TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(ALLOW_EXTERNAL_HTTP=False, GFAVIP_WALLET_LOOKUP_API_KEY=KEY,
                               GFAVIP_WALLET_API_KEY=FALLBACK_KEY)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        # No SQLAlchemy app initialization: this service cannot need a DB write.

    def wallet(self, payload=None, status=200):
        response = Mock(status_code=status)
        response.json.return_value = payload if payload is not None else {
            'id': AGENT_ID, 'username': NAME, 'accountKind': 'ai_agent',
        }
        return patch('services.agent_lookup.http_get', return_value=response)

    def fails(self, status, raw=NAME):
        with self.assertRaises(WorkspaceError) as caught:
            lookup_agent_username(raw)
        self.assertEqual(caught.exception.status, status)
        for secret in (KEY, FALLBACK_KEY, 'private-upstream-data', 'private.example'):
            self.assertNotIn(secret, str(caught.exception))
        return str(caught.exception)

    def test_exact_match_fixed_https_header_only_key_timeout_no_redirects(self):
        self.app.config['GFAVIP_WALLET_BASE_URL'] = 'https://private.example/steal'
        with self.wallet() as request:
            result = lookup_agent_username('  @PL-Test_Helper  ')
        self.assertEqual(result, {'id': AGENT_ID, 'username': NAME, 'accountKind': 'ai_agent'})
        request.assert_called_once_with(WALLET_LOOKUP_URL, params={'username': 'PL-Test_Helper'},
            headers={'Authorization': 'Bearer ' + KEY, 'Accept': 'application/json'},
            timeout=5, allow_redirects=False)
        self.assertNotIn(KEY, request.call_args.args[0])
        self.assertNotIn(KEY, str(request.call_args.kwargs['params']))
        self.assertNotIn(KEY, str(result))

    def test_canonicalizes_returned_uuid_without_inferring_id_from_username(self):
        with self.wallet({'id': AGENT_ID.upper(), 'username': 'PL-Test_Helper', 'accountKind': 'ai_agent'}):
            result = lookup_agent_username(NAME)
        self.assertEqual(result['id'], AGENT_ID)
        self.assertEqual(result['username'], 'PL-Test_Helper')

    def test_documented_ascii_names_do_not_require_a_pl_prefix(self):
        for name in ('helper', 'a.b_c-d', 'A' * 200):
            with self.subTest(name_length=len(name)), self.wallet({
                    'id': AGENT_ID, 'username': name, 'accountKind': 'ai_agent'}):
                self.assertEqual(lookup_agent_username(name)['username'], name)

    def test_invalid_names_are_rejected_before_http(self):
        for raw in (None, 7, True, {}, [], '', '   ', '@', '@@helper', '@ helper', 'a' * 201,
                    'a b', 'a/b', 'a?name=x', 'a#x', 'a%20b', 'a\x00b', 'a\r\nb',
                    'hélga', 'рl-helga', 'helper@example.invalid', 'https://wallet.gfavip.com'):
            with self.subTest(raw_type=type(raw).__name__), self.wallet() as request:
                self.fails(400, raw)
                request.assert_not_called()

    def test_obvious_credentials_never_become_query_parameters(self):
        for raw in ('gfavip-session-' + 'a' * 64, 'GFAVIP_SESSION_TOKEN', 'gfavip_' + 'a' * 64,
                    'ls_agent_' + 'b' * 43, 'ls-guest-' + 'c' * 32, 'pl_live_private',
                    'pl_test_private', 'pl_api_private', 'pl_sk_private', 'sk-private',
                    'sk_private', 'pk_private'):
            with self.subTest(prefix=raw[:9]), self.wallet() as request:
                message = self.fails(400, '@' + raw)
                self.assertNotIn(raw, message)
                request.assert_not_called()

    def test_uuid_input_guides_to_advanced_flow_without_http(self):
        for raw in (AGENT_ID, AGENT_ID.upper(), AGENT_ID.replace('-', ''), '{' + AGENT_ID + '}',
                    '00000000-0000-0000-0000-000000000000'):
            with self.subTest(raw_length=len(raw)), self.wallet() as request:
                message = self.fails(400, raw)
                self.assertIn('Advanced Wallet UUID', message)
                request.assert_not_called()

    def test_key_preference_and_fallback_do_not_probe_or_escalate_on_denial(self):
        for preferred in (None, '', '   ', False):
            self.app.config['GFAVIP_WALLET_LOOKUP_API_KEY'] = preferred
            with self.wallet() as request:
                lookup_agent_username(NAME)
                self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer ' + FALLBACK_KEY)
        self.app.config['GFAVIP_WALLET_LOOKUP_API_KEY'] = KEY
        with self.wallet(status=403) as request:
            self.assertIn('Advanced Wallet UUID', self.fails(503))
            request.assert_called_once()
            self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer ' + KEY)

    def test_availability_is_config_only_not_a_permission_or_health_claim(self):
        with self.wallet(status=403) as request:
            self.assertTrue(lookup_available())
            request.assert_not_called()
        self.app.config['GFAVIP_WALLET_LOOKUP_API_KEY'] = None
        self.assertTrue(lookup_available())
        self.app.config['GFAVIP_WALLET_API_KEY'] = None
        self.assertFalse(lookup_available())
        with self.wallet() as request:
            self.assertIn('Advanced Wallet UUID', self.fails(503))
            request.assert_not_called()

    def test_service_failure_statuses_do_not_forward_errors_or_json(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 500, 502, 503):
            with self.subTest(status=status), self.wallet(status=status) as request:
                request.return_value.json.side_effect = AssertionError('Do not inspect error bodies')
                self.assertIn('Advanced Wallet UUID', self.fails(503))
        for status in (404, 409, 429):
            with self.subTest(status=status), self.wallet(status=status) as request:
                request.return_value.json.side_effect = AssertionError('Do not inspect error bodies')
                self.assertIn('Advanced Wallet UUID', self.fails(status))

    def test_timeout_network_and_disabled_external_http_fail_closed(self):
        for error in (requests.Timeout(KEY), requests.ConnectionError('https://private.example/' + KEY),
                      requests.exceptions.InvalidHeader(FALLBACK_KEY)):
            with patch('services.agent_lookup.http_get', side_effect=error):
                self.assertIn('Advanced Wallet UUID', self.fails(503))
        # Test app has no real HTTP permission, even with its synthetic key.
        self.fails(503)

    def test_invalid_json_or_response_schema_fails_closed(self):
        with self.wallet() as request:
            request.return_value.json.side_effect = ValueError('private-upstream-data ' + KEY)
            self.fails(503)
        for payload in ([], 'private-upstream-data', {}, {'id': AGENT_ID, 'username': NAME},
                        {'id': AGENT_ID, 'username': NAME, 'accountKind': 'ai_agent', 'email': 'private-upstream-data'},
                        {'id': AGENT_ID, 'username': NAME, 'accountKind': 'ai_agent', 'token': KEY}):
            with self.subTest(payload_type=type(payload).__name__), self.wallet(payload):
                self.fails(503)

    def test_returned_uuid_and_username_must_match_strict_contract(self):
        for field, value in [('id', None), ('id', 123), ('id', AGENT_ID.replace('-', '')),
                             ('id', '{' + AGENT_ID + '}'), ('id', '00000000-0000-0000-0000-000000000000'),
                             ('username', 'another-agent'), ('username', NAME + ' '),
                             ('username', '@' + NAME), ('username', None), ('username', [NAME])]:
            with self.subTest(field=field, value_type=type(value).__name__), self.wallet({
                    'id': AGENT_ID, 'username': NAME, 'accountKind': 'ai_agent', field: value}):
                self.fails(503)

    def test_human_account_refused_and_unknown_kinds_fail_closed(self):
        with self.wallet({'id': AGENT_ID, 'username': NAME, 'accountKind': 'human'}):
            self.assertIn('human Wallet account', self.fails(400))
        for kind in (None, 'agent', 'AI_AGENT', '', {}, ['ai_agent']):
            with self.subTest(kind_type=type(kind).__name__), self.wallet({
                    'id': AGENT_ID, 'username': NAME, 'accountKind': kind}):
                self.fails(503)
