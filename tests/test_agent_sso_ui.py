"""Human-facing SSO consent and the isolated issuance confirmations."""
from html.parser import HTMLParser
import re
import unittest

from app import create_app
from models import db, User
from services.agent_workspace import AgentConnection
from services.agent_sso import AgentSSOGrant


class Tags(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        self.tags.append((tag, dict(attributes)))

    def by_id(self, element_id):
        return next(attributes for _, attributes in self.tags if attributes.get('id') == element_id)


class AgentSSOUITests(unittest.TestCase):
    AGENT_ID = '11111111-2222-4333-8444-555555555555'

    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            db.session.add(User(id='human-owner', username='Human owner', tier='free'))
            db.session.commit()
        with self.client.session_transaction() as state:
            state['user_id'] = 'human-owner'

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def create_connection(self, **overrides):
        self.client.get('/agents')
        with self.client.session_transaction() as state:
            csrf = state['agent_workspace_csrf']
        return self.client.post('/agents/connections', data={
            'csrf_token': csrf,
            'label': 'My separate agent',
            'auth_mode': 'gfavip-sso',
            'agent_gfavip_user_id': self.AGENT_ID,
            'profile': 'trade-assistant',
            **overrides,
        })

    def assert_isolated_confirmation(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store, private')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        document = response.get_data(as_text=True)
        self.assertNotIn('<script', document.lower())
        self.assertNotIn('cdn.tailwindcss.com', document)
        self.assertNotIn('<link', document.lower())
        return document

    def test_workspace_defaults_to_sso_with_required_uuid_and_public_guides(self):
        response = self.client.get('/agents')
        self.assertEqual(response.status_code, 200)
        document = response.get_data(as_text=True)
        tags = Tags(document)
        self.assertEqual(tags.by_id('agent-auth-mode')['name'], 'auth_mode')
        sso_option = next(attrs for tag, attrs in tags.tags
                          if tag == 'option' and attrs.get('value') == 'gfavip-sso')
        self.assertIn('selected', sso_option)
        wallet_input = tags.by_id('agent-gfavip-user-id')
        self.assertEqual(wallet_input['name'], 'agent_gfavip_user_id')
        self.assertIn('required', wallet_input)
        self.assertNotIn('disabled', wallet_input)
        self.assertIn('agentWalletId.required = usesSso', document)
        self.assertIn('agentWalletId.disabled = !usesSso', document)
        self.assertIn('GET /api/agent/v1/me', document)
        self.assertIn('not your own account ID', document)
        links = {attrs.get('href') for tag, attrs in tags.tags if tag == 'a'}
        self.assertTrue({'/skill.md', '/skill_api.md', '/skill_prompt.md'} <= links)

    def test_sso_confirmation_has_actual_permissions_and_no_issued_secret(self):
        response = self.create_connection(include_messages='yes')
        document = self.assert_isolated_confirmation(response)
        self.assertIn('Your agent’s access is approved', document)
        self.assertIn(self.AGENT_ID, document)
        self.assertIn('events:read trades:read trade_messages:read', document)
        self.assertIn('allowed access to private trade-room messages', document)
        self.assertIn('The UUID above is the identity you supplied', document)
        self.assertIn('Expires', document)
        self.assertNotIn('issued-agent-token', document)
        self.assertNotIn('ls_agent_', document)
        with self.app.app_context():
            connection = AgentConnection.query.one()
            self.assertIn(f'X-Liquidity-Connection: {connection.id}', document)
            self.assertEqual(connection.owner_id, 'human-owner')
            self.assertEqual(connection.sso_grant.agent_gfavip_user_id, self.AGENT_ID)
        with self.client.session_transaction() as state:
            self.assertNotIn(self.AGENT_ID, str(dict(state)))

    def test_sso_draft_profile_does_not_claim_trade_access(self):
        response = self.create_connection(profile='offer-drafts')
        document = self.assert_isolated_confirmation(response)
        self.assertIn('drafts:read drafts:write', document)
        self.assertIn('Draft-only access.', document)
        self.assertIn('It cannot read your trade rooms', document)
        self.assertNotIn('Read-only trade assistance.', document)

    def test_scoped_token_fallback_still_shows_once_without_sso_fields(self):
        response = self.create_connection(auth_mode='scoped-token', agent_gfavip_user_id='')
        document = self.assert_isolated_confirmation(response)
        token = re.search(r'ls_agent_[A-Za-z0-9_-]{43}', document).group()
        self.assertIn('issued-agent-token', document)
        self.assertIn('Private message text is not included', document)
        self.assertNotIn('Approved agent Wallet UUID', document)
        self.assertNotIn('drafts:read drafts:write', document)
        self.assertNotIn(token, self.client.get('/agents').get_data(as_text=True))
        self.assertNotIn(token, response.headers.get('Set-Cookie', ''))
        with self.app.app_context():
            self.assertEqual(AgentSSOGrant.query.count(), 0)

    def test_connections_distinguish_modes_escape_labels_and_revoke_grant(self):
        self.create_connection(label='<script>alert(1)</script>')
        self.create_connection(auth_mode='scoped-token', agent_gfavip_user_id='', label='Fallback')
        response = self.client.get('/agents')
        document = response.get_data(as_text=True)
        self.assertIn('GFAVIP SSO grant', document)
        self.assertIn('Scoped-token fallback', document)
        self.assertIn(self.AGENT_ID, document)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', document)
        self.assertNotIn('<script>alert(1)</script>', document)
        with self.app.app_context():
            connection_id = AgentSSOGrant.query.one().connection_id
        with self.client.session_transaction() as state:
            csrf = state['agent_workspace_csrf']
        response = self.client.post(f'/agents/connections/{connection_id}/revoke',
                                    data={'csrf_token': csrf}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Revoked', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
