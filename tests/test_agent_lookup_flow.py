"""Human-approved username lookup cannot mint permissions or change identity."""
from html import unescape
from datetime import timedelta
import re
import unittest
import time
from unittest.mock import patch

from app import create_app
from models import db, User
from routes.agents import lookup_signer
from services.agent_sso import AgentSSOGrant
from services.agent_workspace import AgentConnection, WorkspaceError

AGENT = '00000000-0000-4000-8000-000000000003'
OTHER_AGENT = '00000000-0000-4000-8000-000000000004'
ACCOUNT = {'id': AGENT, 'username': 'pl-sample-bot', 'accountKind': 'ai_agent'}


class AgentLookupFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        db.session.add_all([User(id='owner', username='Owner'), User(id='other', username='Other')])
        db.session.commit()
        self.human = self.login('owner')
        self.other = self.login('other')
        self.anonymous = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login(self, owner):
        client = self.app.test_client()
        with client.session_transaction() as state:
            state['user_id'] = owner
        client.get('/agents')
        return client

    def csrf(self, client=None):
        with (client or self.human).session_transaction() as state:
            return state['agent_workspace_csrf']

    def lookup(self, client=None, **overrides):
        client = client or self.human
        with patch('routes.agents.lookup_agent_username', return_value=dict(ACCOUNT)) as fetch:
            response = client.post('/agents/lookup', data={'csrf_token': self.csrf(client),
                'agent_username': ACCOUNT['username'], 'label': 'My helper', **overrides})
        return response, fetch

    def proof(self, response):
        match = re.search(r'name="lookup_proof"[^>]*value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return unescape(match.group(1))

    def approve(self, proof, client=None, **overrides):
        client = client or self.human
        return client.post('/agents/connections', data={'csrf_token': self.csrf(client),
            'auth_mode': 'gfavip-sso', 'identity_mode': 'username', 'lookup_proof': proof,
            'confirm_agent': 'yes', **overrides})

    def test_lookup_needs_human_login_and_csrf_and_never_accepts_agent_bearer(self):
        with patch('routes.agents.lookup_agent_username') as fetch:
            self.assertEqual(self.anonymous.post('/agents/lookup').status_code, 302)
            self.assertEqual(self.human.post('/agents/lookup').status_code, 400)
            response = self.human.post('/agents/lookup', headers={
                'Authorization': 'Bearer gfavip-session-' + 'a' * 64},
                data={'csrf_token': self.csrf(), 'agent_username': ACCOUNT['username'], 'label': 'Helper'})
            self.assertEqual(response.status_code, 403)
            fetch.assert_not_called()
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(self.human.get('/agents/lookup').status_code, 405)

    def test_lookup_creates_nothing_and_review_is_script_free_private_and_exact(self):
        response, fetch = self.lookup(include_messages='yes')
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with(ACCOUNT['username'])
        html = response.get_data(as_text=True)
        self.assertIn(ACCOUNT['username'], html)
        self.assertIn(AGENT, html)
        self.assertIn('Watch trades (read-only)', html)
        self.assertIn('7 days from approval', html)
        self.assertIn('valid for 5 minutes', html)
        self.assertNotIn('<script', html.lower())
        self.assertNotIn('cdn.tailwind', html)
        self.assertNotIn('ls_agent_', html)
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(AgentSSOGrant.query.count(), 0)
        self.assertEqual(User.query.count(), 2)
        proof = lookup_signer().loads(self.proof(response))
        self.assertEqual(proof['account'], ACCOUNT)
        self.assertEqual(proof['profile'], 'trade-assistant')
        self.assertTrue(proof['messages'])

    def test_approval_binds_reviewed_uuid_and_scope_without_resolving_name_again(self):
        review, _ = self.lookup()
        with patch('routes.agents.lookup_agent_username', return_value={**ACCOUNT, 'id': OTHER_AGENT}) as fetch:
            response = self.approve(self.proof(review))
            fetch.assert_not_called()
        self.assertEqual(response.status_code, 200)
        connection = AgentConnection.query.one()
        self.assertEqual(connection.owner_id, 'owner')
        self.assertEqual(connection.scope, 'events:read trades:read')
        self.assertEqual(connection.sso_grant.agent_gfavip_user_id, AGENT)
        self.assertEqual(connection.expires_at - connection.created_at, timedelta(days=7))
        self.assertNotIn('<script', response.get_data(as_text=True).lower())

    def test_approval_requires_explicit_confirmation_and_untampered_proof(self):
        review, _ = self.lookup()
        proof = self.proof(review)
        for data in ({'confirm_agent': ''}, {'confirm_agent': 'true'}, {'lookup_proof': ''},
                     {'lookup_proof': proof + 'bad'}, {'profile': 'offer-drafts'},
                     {'include_messages': 'yes'}, {'agent_gfavip_user_id': OTHER_AGENT},
                     {'label': 'Changed'}, {'auth_mode': 'scoped-token'}, {'identity_mode': 'uuid'}):
            with self.subTest(fields=tuple(data)):
                self.assertEqual(self.approve(proof, **data).status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_review_expiry_cross_owner_cross_session_and_stale_csrf_are_rejected(self):
        review, _ = self.lookup()
        proof = self.proof(review)
        with patch('itsdangerous.timed.time.time', return_value=time.time() + 301):
            self.assertEqual(self.approve(proof).status_code, 400)
        self.assertEqual(self.approve(proof, client=self.other).status_code, 400)
        self.assertEqual(self.approve(proof, client=self.login('owner')).status_code, 400)
        # A successful approval rotates the page CSRF, invalidating old reviews.
        self.assertEqual(self.approve(proof).status_code, 200)
        self.assertEqual(self.approve(proof).status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 1)

    def test_consumed_review_cannot_create_grant_again_with_an_old_session_cookie(self):
        review, _ = self.lookup()
        proof = self.proof(review)
        old_cookie = self.human.get_cookie('session').value
        self.assertEqual(self.approve(proof).status_code, 200)
        replay = self.app.test_client()
        replay.set_cookie('session', old_cookie)
        self.assertEqual(self.approve(proof, client=replay).status_code, 409)
        self.assertEqual(self.approve(proof + '=', client=replay).status_code, 409)
        self.assertEqual(AgentConnection.query.count(), 1)
        self.assertEqual(AgentSSOGrant.query.count(), 1)

    def test_offer_drafts_and_message_consent_are_exactly_what_was_reviewed(self):
        review, _ = self.lookup(profile='offer-drafts')
        self.assertEqual(self.approve(self.proof(review)).status_code, 200)
        review, _ = self.lookup(include_messages='yes')
        self.assertEqual(self.approve(self.proof(review)).status_code, 200)
        self.assertEqual(sorted(connection.scope for connection in AgentConnection.query.all()),
                         ['drafts:read drafts:write', 'events:read trades:read trade_messages:read'])

    def test_bad_profile_consent_and_label_fail_before_lookup(self):
        for data in ({'profile': 'admin'}, {'profile': 'offer-drafts', 'include_messages': 'yes'},
                     {'include_messages': 'true'}, {'label': ''}, {'label': 'a' * 81}, {'label': 'bad\x00name'}):
            response, fetch = self.lookup(**data)
            self.assertEqual(response.status_code, 400)
            fetch.assert_not_called()
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_duplicate_fields_fail_before_lookup_or_approval(self):
        from werkzeug.datastructures import MultiDict
        with patch('routes.agents.lookup_agent_username') as fetch:
            response = self.human.post('/agents/lookup', data=MultiDict([
                ('csrf_token', self.csrf()), ('label', 'Helper'), ('agent_username', 'one'), ('agent_username', 'two')]))
            self.assertEqual(response.status_code, 400)
            fetch.assert_not_called()
        review, _ = self.lookup()
        response = self.human.post('/agents/connections', data=MultiDict([
            ('csrf_token', self.csrf()), ('auth_mode', 'gfavip-sso'), ('identity_mode', 'username'),
            ('lookup_proof', self.proof(review)), ('confirm_agent', 'yes'), ('confirm_agent', 'no')]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_service_failure_returns_workspace_with_manual_fallback_and_no_grant(self):
        for status in (400, 404, 409, 429, 503):
            with patch('routes.agents.lookup_agent_username', side_effect=WorkspaceError('Safe lookup error', status)):
                response = self.human.post('/agents/lookup', data={'csrf_token': self.csrf(),
                    'agent_username': 'pl-sample-bot', 'label': 'Helper'})
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.mimetype, 'text/html')
            self.assertIn('Safe lookup error', response.get_data(as_text=True))
            self.assertIn('agent_gfavip_user_id', response.get_data(as_text=True))
            self.assertIn('no-store', response.headers['Cache-Control'])
            if status == 429:
                self.assertEqual(response.headers['Retry-After'], '60')
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_local_lookup_quota_is_owner_scoped_bounded_and_recovers(self):
        for _ in range(10):
            self.assertEqual(self.lookup()[0].status_code, 200)
        response, fetch = self.lookup()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '60')
        fetch.assert_not_called()
        self.assertEqual(self.lookup(client=self.other)[0].status_code, 200)
        with patch('routes.agents.monotonic', return_value=time.monotonic() + 61):
            self.assertEqual(self.lookup()[0].status_code, 200)

    def test_manual_uuid_fallback_and_capabilities_default_are_read_only(self):
        response = self.human.post('/agents/connections', data={'csrf_token': self.csrf(),
            'auth_mode': 'gfavip-sso', 'identity_mode': 'uuid', 'label': 'Manual helper',
            'agent_gfavip_user_id': AGENT})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AgentConnection.query.one().scope, 'events:read trades:read')
        self.assertEqual(self.anonymous.get('/api/agent/v1/capabilities').get_json()['default_profile'], 'trade-assistant')


if __name__ == '__main__':
    unittest.main()
