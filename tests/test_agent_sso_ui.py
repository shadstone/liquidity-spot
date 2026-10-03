"""Human-facing SSO consent and the isolated issuance confirmations."""
from html.parser import HTMLParser
import re
import unittest
from unittest.mock import patch

from app import create_app
from models import db, User
from services.agent_workspace import AgentConnection, WorkspaceError
from services.agent_sso import AgentSSOGrant


class Tags(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.forms = {}
        self.current_form = None
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        self.tags.append((tag, attributes))
        if tag == 'form':
            self.current_form = attributes.get('id', str(len(self.forms)))
            self.forms[self.current_form] = {'attributes': attributes, 'tags': []}
        elif self.current_form is not None:
            self.forms[self.current_form]['tags'].append((tag, attributes))

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current_form = None

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
            'identity_mode': 'uuid',
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

    def test_workspace_defaults_to_username_review_and_independent_fallback_forms(self):
        self.app.config['GFAVIP_WALLET_LOOKUP_API_KEY'] = 'test-ui-key-never-sent'
        response = self.client.get('/agents')
        self.assertEqual(response.status_code, 200)
        document = response.get_data(as_text=True)
        tags = Tags(document)
        lookup = tags.forms['agent-lookup-form']
        self.assertEqual(lookup['attributes']['method'], 'POST')
        self.assertEqual(lookup['attributes']['action'], '/agents/lookup')
        self.assertEqual(tags.by_id('agent-username')['name'], 'agent_username')
        self.assertIn('required', tags.by_id('agent-username'))
        self.assertEqual(tags.by_id('agent-username')['maxlength'], '201')
        lookup_names = {attrs.get('name') for _, attrs in lookup['tags']}
        self.assertIn('agent_username', lookup_names)
        self.assertNotIn('agent_gfavip_user_id', lookup_names)
        self.assertNotIn('confirm_agent', lookup_names)
        for form_id in ('agent-lookup-form', 'agent-uuid-form', 'agent-token-form'):
            form = tags.forms[form_id]
            selected = [attrs['value'] for tag, attrs in form['tags']
                        if tag == 'option' and 'selected' in attrs]
            self.assertEqual(selected, ['trade-assistant'])
            inputs = {attrs.get('name'): attrs for tag, attrs in form['tags'] if tag == 'input'}
            self.assertIn('csrf_token', inputs)
            self.assertIn('label', inputs)
            self.assertNotIn('checked', inputs['include_messages'])
        manual = {attrs.get('name'): attrs for tag, attrs in tags.forms['agent-uuid-form']['tags'] if tag == 'input'}
        self.assertEqual(manual['auth_mode']['value'], 'gfavip-sso')
        self.assertEqual(manual['identity_mode']['value'], 'uuid')
        token = {attrs.get('name'): attrs for tag, attrs in tags.forms['agent-token-form']['tags'] if tag == 'input'}
        self.assertEqual(token['auth_mode']['value'], 'scoped-token')
        self.assertNotIn('agent_gfavip_user_id', token)
        wallet_input = tags.by_id('agent-gfavip-user-id')
        self.assertEqual(wallet_input['name'], 'agent_gfavip_user_id')
        self.assertIn('required', wallet_input)
        self.assertNotIn('disabled', wallet_input)
        self.assertIn("document.querySelectorAll('[data-agent-access-form]')", document)
        self.assertIn('GET /api/agent/v1/me', document)
        self.assertIn('not your own account ID', document)
        links = {attrs.get('href') for tag, attrs in tags.tags if tag == 'a'}
        self.assertTrue({'/skill.md', '/skill_api.md', '/skill_prompt.md'} <= links)

    def test_unavailable_lookup_keeps_advanced_forms_usable_without_javascript(self):
        self.app.config['GFAVIP_WALLET_LOOKUP_API_KEY'] = None
        self.app.config['GFAVIP_WALLET_API_KEY'] = None
        document = self.client.get('/agents').get_data(as_text=True)
        tags = Tags(document)
        self.assertNotIn('agent-lookup-form', tags.forms)
        self.assertIn('Username lookup is not available', document)
        self.assertIn('agent-uuid-form', tags.forms)
        self.assertIn('agent-token-form', tags.forms)
        self.assertNotIn('disabled', tags.by_id('agent-gfavip-user-id'))
        self.assertNotIn('hidden', tags.by_id('agent-advanced'))

    def lookup(self, account=None, **overrides):
        self.app.config['GFAVIP_WALLET_LOOKUP_API_KEY'] = 'test-ui-key-never-sent'
        self.client.get('/agents')
        with self.client.session_transaction() as state:
            csrf = state['agent_workspace_csrf']
        account = account or {'id': self.AGENT_ID, 'username': 'helga-helper', 'accountKind': 'ai_agent'}
        with patch('routes.agents.lookup_agent_username', return_value=account):
            return self.client.post('/agents/lookup', data={
                'csrf_token': csrf, 'agent_username': account['username'],
                'label': 'Helga', 'profile': 'trade-assistant', **overrides,
            })

    def test_lookup_review_is_isolated_explicit_and_does_not_grant_access(self):
        response = self.lookup(include_messages='yes')
        document = self.assert_isolated_confirmation(response)
        self.assertIn('helga-helper', document)
        self.assertIn('AI agent', document)
        self.assertIn('ai_agent', document)
        self.assertIn(self.AGENT_ID, document)
        self.assertIn('Human owner — your currently signed-in', document)
        self.assertIn('The connection name is only a label', document)
        self.assertIn('events:read trades:read trade_messages:read', document)
        self.assertIn('has not granted access or started a bot', document)
        tags = Tags(document)
        inputs = {attrs.get('name'): attrs for tag, attrs in tags.tags if tag == 'input'}
        self.assertEqual(set(inputs), {'csrf_token', 'auth_mode', 'identity_mode', 'lookup_proof', 'confirm_agent'})
        self.assertEqual(inputs['auth_mode']['value'], 'gfavip-sso')
        self.assertEqual(inputs['identity_mode']['value'], 'username')
        self.assertEqual(inputs['confirm_agent']['value'], 'yes')
        self.assertIn('required', inputs['confirm_agent'])
        self.assertNotIn('checked', inputs['confirm_agent'])
        self.assertNotIn('ls_agent_', document)
        with self.app.app_context():
            self.assertEqual(AgentConnection.query.count(), 0)
            self.assertEqual(AgentSSOGrant.query.count(), 0)
        approval = {name: attrs['value'] for name, attrs in inputs.items()}
        result = self.client.post('/agents/connections', data=approval)
        success = self.assert_isolated_confirmation(result)
        self.assertIn('Approved agent username', success)
        self.assertIn('helga-helper', success)
        with self.app.app_context():
            self.assertEqual(AgentConnection.query.one().owner_id, 'human-owner')
            self.assertEqual(AgentSSOGrant.query.one().agent_gfavip_user_id, self.AGENT_ID)

    def test_lookup_review_escapes_names_and_shows_exact_draft_permissions(self):
        response = self.lookup(account={'id': self.AGENT_ID, 'username': '<script>unsafe</script>',
                                       'accountKind': 'ai_agent'}, profile='offer-drafts', label='<b>not owner</b>')
        document = self.assert_isolated_confirmation(response)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', document)
        self.assertIn('&lt;b&gt;not owner&lt;/b&gt;', document)
        self.assertIn('drafts:read drafts:write', document)
        self.assertIn('Private offer drafts only', document)
        self.assertIn('Not allowed — private message text is excluded', document)
        self.assertNotIn('trade_messages:read', document)

    def test_lookup_error_is_accessible_and_points_to_manual_fallback(self):
        self.app.config['GFAVIP_WALLET_LOOKUP_API_KEY'] = 'test-ui-key-never-sent'
        self.client.get('/agents')
        with self.client.session_transaction() as state:
            csrf = state['agent_workspace_csrf']
        with patch('routes.agents.lookup_agent_username', side_effect=WorkspaceError('No exact match.', 404)):
            response = self.client.post('/agents/lookup', data={
                'csrf_token': csrf, 'agent_username': 'missing-agent', 'label': 'Review only',
                'profile': 'trade-assistant'})
        self.assertEqual(response.status_code, 404)
        document = response.get_data(as_text=True)
        tags = Tags(document)
        self.assertTrue(any(attrs.get('role') == 'alert' for _, attrs in tags.tags))
        self.assertIn('No exact match.', document)
        self.assertIn('No access was granted.', document)
        self.assertIn('#agent-advanced', {attrs.get('href') for tag, attrs in tags.tags if tag == 'a'})
        self.assertIn('agent-uuid-form', tags.forms)

    def test_sso_confirmation_has_actual_permissions_and_no_issued_secret(self):
        response = self.create_connection(include_messages='yes')
        document = self.assert_isolated_confirmation(response)
        self.assertIn('Your agent’s access is approved', document)
        self.assertIn(self.AGENT_ID, document)
        self.assertIn('events:read trades:read trade_messages:read', document)
        self.assertIn('allowed access to private trade-room messages', document)
        self.assertIn('The UUID above is the identity you approved', document)
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
