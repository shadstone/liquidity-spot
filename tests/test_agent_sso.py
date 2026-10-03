"""No real credentials or network: Wallet identity never implies permissions."""
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from app import create_app
from models import db, User, P2POffer, P2PTrade
from services.agent_sso import (
    AgentSSOGrant, WALLET_VALIDATE_URL, create_sso_grant, validate_agent_identity,
)
from services.agent_workspace import (
    AgentConnection, AgentDraft, WorkspaceError, authenticate_bearer,
    create_draft, digest_token, issue_connection,
)


OWNER = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'
AGENT = '00000000-0000-4000-8000-000000000003'
STRANGER = '00000000-0000-4000-8000-000000000004'
SESSION = 'gfavip-session-' + 'a' * 64


class AgentSSOTests(TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all([User(id=OWNER, username='Owner', tier='free'),
                            User(id=OTHER, username='Other', tier='vip')])
        db.session.commit()
        self.client = self.app.test_client()
        self.human = self.app.test_client()
        with self.human.session_transaction() as state:
            state['user_id'] = OWNER
        self.assertFalse(self.app.config['ALLOW_EXTERNAL_HTTP'])

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def wallet(self, user_id=AGENT, payload=None, status=200):
        response = Mock(status_code=status)
        response.json.return_value = payload if payload is not None else {
            'valid': True, 'user': {'id': user_id, 'tier': 'superadmin',
                                   'email': 'private@example.invalid', 'credits': 999,
                                   'accountKind': 'ai_agent'},
            'agent_context': {'owner_id': OWNER, 'squad': [{'gfavip_id': OTHER}]},
        }
        return patch('services.agent_sso.http_post', return_value=response)

    def grant(self, owner=OWNER, agent=AGENT, profile='trade-assistant', messages=False):
        connection = create_sso_grant(owner, 'SSO agent', agent, profile, messages)
        db.session.commit()
        return connection

    def auth(self, connection, scopes=None, token=SESSION):
        return authenticate_bearer('Bearer ' + token, scopes, str(connection.id))

    def api(self, path='/api/agent/v1/events', connection=None, client=None, token=SESSION):
        headers = {'Authorization': 'Bearer ' + token}
        if connection is not None:
            headers['X-Liquidity-Connection'] = str(connection.id)
        return (client or self.client).get(path, headers=headers)

    def assert_workspace_error(self, status, callback):
        with self.assertRaises(WorkspaceError) as caught:
            callback()
        self.assertEqual(caught.exception.status, status)
        self.assertNotIn(SESSION, str(caught.exception))
        return caught.exception

    def test_identity_uses_only_validated_wallet_user_id_and_fixed_endpoint(self):
        with self.wallet(user_id=AGENT.upper()) as request:
            self.assertEqual(validate_agent_identity(SESSION), {'gfavip_user_id': AGENT})
        request.assert_called_once_with(WALLET_VALIDATE_URL,
            headers={'Authorization': 'Bearer ' + SESSION, 'Accept': 'application/json'},
            timeout=5, allow_redirects=False)
        self.assertEqual(User.query.count(), 2)
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(db.session.get(User, OWNER).tier, 'free')

    def test_identity_rejects_keys_handles_query_syntax_and_header_injection_before_http(self):
        for value in (None, '', 'pl_secret', 'ls_agent_' + 'a' * 43, '@myagent',
                      SESSION + '\r\nX-Injected: yes', SESSION + '?owner_id=' + OWNER,
                      'gfavip-session-' + 'a' * 201, 'gfavip-session-short'):
            with self.subTest(value_type=type(value).__name__), self.wallet() as request:
                self.assert_workspace_error(401, lambda: validate_agent_identity(value))
                request.assert_not_called()

    def test_no_identity_inference_from_agent_owner_context_tier_or_top_level_fields(self):
        payloads = [
            {'valid': True, 'user': {'tier': 'superadmin'}, 'agent_context': {'owner_id': AGENT}},
            {'valid': True, 'user_id': AGENT, 'id': AGENT},
            {'valid': True, 'user': {'id': 'pl-handle'}},
            {'valid': True, 'user': {'id': {'id': AGENT}}},
            {'valid': True, 'user': {'id': AGENT.replace('-', '')}},
            {'valid': True, 'user': {'id': '00000000-0000-0000-0000-000000000000'}},
            {'valid': True, 'user': []},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.wallet(payload=payload):
                self.assert_workspace_error(503, lambda: validate_agent_identity(SESSION))

    def test_exact_boolean_valid_required_and_no_upstream_error_echo(self):
        for payload in ({'valid': False, 'error': SESSION}, {'valid': 'true', 'user': {'id': AGENT}},
                        {'valid': 1, 'user': {'id': AGENT}}, {}, [], 'private upstream content'):
            with self.subTest(payload_type=type(payload).__name__), self.wallet(payload=payload):
                self.assert_workspace_error(401, lambda: validate_agent_identity(SESSION))

    def test_unavailable_timeout_redirect_and_invalid_json_fail_closed(self):
        for status in (301, 302, 307, 404, 429, 500, 503):
            with self.subTest(status=status), self.wallet(status=status) as request:
                self.assert_workspace_error(503, lambda: validate_agent_identity(SESSION))
                self.assertFalse(request.call_args.kwargs['allow_redirects'])
        for status in (401, 403):
            with self.wallet(status=status):
                self.assert_workspace_error(401, lambda: validate_agent_identity(SESSION))
        for error in (requests.Timeout(SESSION), requests.ConnectionError(SESSION)):
            with patch('services.agent_sso.http_post', side_effect=error):
                self.assert_workspace_error(503, lambda: validate_agent_identity(SESSION))
        with self.wallet() as request:
            request.return_value.json.side_effect = ValueError(SESSION)
            self.assert_workspace_error(503, lambda: validate_agent_identity(SESSION))
        # The app's disabled-HTTP test boundary also fails closed without a call.
        self.assert_workspace_error(503, lambda: validate_agent_identity(SESSION))

    def test_grant_has_shared_expiry_scope_quota_and_no_usable_legacy_credential(self):
        with patch('services.agent_workspace.secrets.token_urlsafe', return_value='b' * 43):
            connection = self.grant(agent=AGENT.upper(), messages=True)
        self.assertEqual(connection.scope, 'events:read trades:read trade_messages:read')
        self.assertEqual(connection.expires_at - connection.created_at, timedelta(days=7))
        self.assertEqual(connection.authentication_method, 'gfavip-sso')
        self.assertEqual(connection.sso_grant.agent_gfavip_user_id, AGENT)
        self.assertEqual(connection.sso_grant.connection_id, connection.id)
        self.assertEqual(connection.token_hash, digest_token('ls_agent_' + 'b' * 43))
        self.assertNotIn(SESSION, str(connection.__dict__))
        self.assert_workspace_error(401, lambda: self.auth(connection, token='ls_agent_' + 'b' * 43))
        self.assertEqual(User.query.count(), 2)
        for _ in range(4):
            self.grant()
        self.assert_workspace_error(429, self.grant)
        db.session.rollback()
        self.assertEqual(AgentSSOGrant.query.count(), 5)

    def test_invalid_grant_inputs_create_nothing(self):
        for agent in ('myagent', 'agent@example.invalid', SESSION, AGENT.replace('-', ''),
                      '{' + AGENT + '}', ' ' + AGENT, None):
            self.assert_workspace_error(400, lambda: self.grant(agent=agent))
        self.assert_workspace_error(400, lambda: self.grant(profile='admin'))
        self.assert_workspace_error(400, lambda: self.grant(profile='offer-drafts', messages=True))
        self.assert_workspace_error(400, lambda: self.grant(messages='yes'))
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(AgentSSOGrant.query.count(), 0)

    def test_missing_or_invalid_exact_connection_id_fails_before_network(self):
        connection = self.grant()
        for connection_id in (None, '', '0', '01', '-1', '1.0', ' 1', '2147483648', True, connection.id):
            with self.subTest(connection_id=connection_id), self.wallet() as request:
                self.assert_workspace_error(400, lambda: authenticate_bearer(
                    'Bearer ' + SESSION, connection_id=connection_id))
                request.assert_not_called()

    def test_matching_verified_identity_and_explicit_grant_required(self):
        connection = self.grant()
        with self.wallet() as request:
            self.assertEqual(self.auth(connection, ['events:read']).id, connection.id)
            # No positive cache: upstream revocation is checked on every use.
            self.assertEqual(self.auth(connection, ['trades:read']).id, connection.id)
            self.assertEqual(request.call_count, 2)
        for user_id in (OWNER, OTHER, STRANGER):
            with self.wallet(user_id=user_id):
                self.assert_workspace_error(401, lambda: self.auth(connection))
        with self.wallet():
            self.assert_workspace_error(401, lambda: authenticate_bearer(
                'Bearer ' + SESSION, connection_id=str(connection.id + 1)))
        legacy, _ = issue_connection(OWNER, 'Legacy')
        db.session.commit()
        with self.wallet(user_id=OWNER):
            self.assert_workspace_error(401, lambda: self.auth(legacy))

    def test_old_tokens_unchanged_and_not_widened_by_connection_header(self):
        old_connection, token = issue_connection(OWNER, 'Legacy')
        sso = self.grant(owner=OTHER)
        with self.wallet() as request:
            self.assertEqual(authenticate_bearer('Bearer ' + token, ['drafts:write'], str(sso.id)).id,
                             old_connection.id)
            self.assert_workspace_error(403, lambda: authenticate_bearer(
                'Bearer ' + token, ['events:read'], str(sso.id)))
            request.assert_not_called()
        self.assertEqual(old_connection.authentication_method, 'scoped-token')

    def test_sso_expiry_revocation_and_unknown_scopes_fail_closed(self):
        connection = self.grant()
        with self.wallet():
            connection.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()
            self.assert_workspace_error(401, lambda: self.auth(connection))
            connection.expires_at = datetime.utcnow() + timedelta(days=1)
            connection.revoked_at = datetime.utcnow()
            db.session.commit()
            self.assert_workspace_error(401, lambda: self.auth(connection))
            connection.revoked_at = None
            connection.scope = 'events:read trades:read trade:send'
            db.session.commit()
            self.assert_workspace_error(401, lambda: self.auth(connection))

    def test_profile_permissions_and_post_lock_rechecks_still_apply(self):
        reader = self.grant()
        writer = self.grant(profile='offer-drafts')
        payload = {'side': 'sell', 'payment_asset': 'usdc-base', 'amount_hns': '1000', 'price': '0.0035'}
        with self.wallet():
            self.assert_workspace_error(403, lambda: self.auth(reader, ['drafts:write']))
            self.assert_workspace_error(403, lambda: self.auth(reader, ['trade_messages:read']))
            self.assert_workspace_error(403, lambda: self.auth(writer, ['trades:read']))
            self.auth(writer, ['drafts:write'])
        writer.revoked_at = datetime.utcnow()
        db.session.commit()
        self.assert_workspace_error(401, lambda: create_draft(writer, 'revoked-write', payload))
        self.assertEqual(AgentDraft.query.count(), 0)

    def test_identity_endpoint_creates_no_user_session_or_automatic_grant(self):
        with self.wallet():
            response = self.api('/api/agent/v1/me')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['gfavip_user_id'], AGENT)
        self.assertNotIn('tier', response.get_json())
        self.assertNotIn('agent_context', response.get_json())
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(User.query.count(), 2)
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(self.api('/api/agent/v1/me', token='pl_not_a_wallet_session').status_code, 401)
        self.assertEqual(self.human.get('/api/agent/v1/me').status_code, 401)

    def test_cookie_cannot_replace_grant_or_override_owner_or_scopes(self):
        connection = self.grant(owner=OTHER)
        self.assertEqual(self.human.get('/api/agent/v1/events').status_code, 401)
        with self.wallet():
            self.assertEqual(self.api(connection=connection, client=self.human).status_code, 200)
            self.assertEqual(self.api('/api/agent/v1/drafts', connection, self.human).status_code, 403)
            self.assertEqual(self.api(client=self.human).status_code, 400)
        self.assertEqual(connection.owner_id, OTHER)
        with self.human.session_transaction() as state:
            self.assertEqual(state['user_id'], OWNER)

    def test_human_sso_issue_and_revoke_require_csrf_and_never_display_tokens(self):
        self.human.get('/agents')
        with self.human.session_transaction() as state:
            csrf = state['agent_workspace_csrf']
        form = {'auth_mode': 'gfavip-sso', 'agent_gfavip_user_id': AGENT,
                'label': 'Explicit consent', 'profile': 'trade-assistant', 'include_messages': 'yes'}
        self.assertEqual(self.human.post('/agents/connections', data=form).status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)
        with self.wallet() as request:
            response = self.human.post('/agents/connections', data={**form, 'csrf_token': csrf})
            request.assert_not_called()  # Owner approval is not session validation.
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(AGENT, html)
        self.assertNotIn('ls_agent_', html)
        self.assertNotIn(SESSION, html)
        self.assertNotIn('<script', html.lower())
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertIn('no-store', response.headers['Cache-Control'])
        connection = AgentConnection.query.one()
        self.assertEqual(connection.scope, 'events:read trades:read trade_messages:read')
        self.assertEqual(connection.owner_id, OWNER)
        self.assertEqual(connection.sso_grant.agent_gfavip_user_id, AGENT)
        with self.human.session_transaction() as state:
            self.assertNotIn(SESSION, str(dict(state)))
            csrf = state['agent_workspace_csrf']
        self.assertEqual(self.human.post(f'/agents/connections/{connection.id}/revoke').status_code, 400)
        self.assertEqual(self.human.post(f'/agents/connections/{connection.id}/revoke',
                                        data={'csrf_token': csrf}).status_code, 302)
        with self.wallet():
            self.assertEqual(self.api(connection=connection).status_code, 401)

    def test_sso_bearer_cannot_use_human_trade_or_management_paths(self):
        connection = self.grant()
        self.human.get('/agents')
        with self.human.session_transaction() as state:
            csrf = state['agent_workspace_csrf']
        headers = {'Authorization': 'Bearer ' + SESSION, 'X-Liquidity-Connection': str(connection.id)}
        for path in ('/p2p/offers/new', '/p2p/offers/1/accept', '/p2p/trades/1/action',
                     '/agents/connections', f'/agents/connections/{connection.id}/revoke'):
            with self.subTest(path=path), self.wallet() as request:
                response = self.human.post(path, headers=headers, data={'csrf_token': csrf,
                    'label': 'must not issue', 'action': 'payment_sent', 'side': 'sell',
                    'amount_hns': '1000', 'price_btc_per_hns': '0.1'})
                self.assertEqual(response.status_code, 403)
                request.assert_not_called()
        self.assertIsNone(connection.revoked_at)
        self.assertEqual(AgentConnection.query.count(), 1)
        self.assertEqual(P2POffer.query.count(), 0)
        self.assertEqual(P2PTrade.query.count(), 0)
        self.assertEqual(User.query.count(), 2)
