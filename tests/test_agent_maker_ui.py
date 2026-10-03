"""Explicit maker choices, immutable review, and always-available revoke controls."""
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from app import create_app
from models import User, db
from services.agent_maker import AgentMakerPolicy
from services.agent_workspace import AgentConnection
from test_agent_sso_ui import Tags


class AgentMakerUITests(unittest.TestCase):
    ACCOUNT = {'id': '11111111-2222-4333-8444-555555555555',
               'username': 'test-maker-only', 'accountKind': 'ai_agent'}
    POLICY_FIELDS = {
        'maker_payment_asset': 'usdc-base', 'maker_side': 'buy',
        'maker_min_price': '0.02', 'maker_max_price': '0.03',
        'maker_max_offer_hns': '25', 'maker_total_hns_budget': '125',
        'maker_max_open_offers': '2', 'maker_max_offers_per_hour': '3',
        'maker_allow_replies': 'yes', 'maker_max_replies_per_hour': '4',
        'maker_max_replies_total': '12',
    }

    def setUp(self):
        self.app = create_app('testing')
        self.app.config['GFAVIP_WALLET_LOOKUP_API_KEY'] = 'test-ui-key-never-sent'
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            db.session.add(User(id='ui-owner', username='UI owner', tier='free'))
            db.session.commit()
        with self.client.session_transaction() as session:
            session['user_id'] = 'ui-owner'

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def csrf(self):
        self.client.get('/agents')
        with self.client.session_transaction() as session:
            return session['agent_workspace_csrf']

    def review(self, **overrides):
        self.app.config['AGENT_MAKER_ENABLED'] = True
        fields = {'csrf_token': self.csrf(), 'agent_username': self.ACCOUNT['username'],
                  'profile': 'maker-assistant', 'label': 'Test bounded maker', **self.POLICY_FIELDS,
                  **overrides}
        with patch('routes.agents.lookup_agent_username', return_value=dict(self.ACCOUNT)):
            return self.client.post('/agents/lookup', data=fields)

    def approve(self, response):
        tags = Tags(response.get_data(as_text=True))
        fields = {attrs['name']: attrs.get('value', '') for tag, attrs in tags.tags
                  if tag == 'input' and attrs.get('name')}
        return self.client.post('/agents/connections', data=fields)

    def test_disabled_mode_has_no_maker_form_and_no_existing_form_can_select_it(self):
        self.app.config['AGENT_MAKER_ENABLED'] = False
        document = self.client.get('/agents').get_data(as_text=True)
        tags = Tags(document)
        self.assertNotIn('agent-maker-form', tags.forms)
        self.assertIn('not enabled', document)
        for form_id in ('agent-lookup-form', 'agent-uuid-form', 'agent-token-form'):
            choices = [attrs['value'] for tag, attrs in tags.forms[form_id]['tags'] if tag == 'option']
            self.assertEqual(set(choices), {'trade-assistant', 'offer-drafts'})

    def test_enabled_maker_is_independent_username_form_with_no_financial_defaults(self):
        self.app.config['AGENT_MAKER_ENABLED'] = True
        document = self.client.get('/agents').get_data(as_text=True)
        tags = Tags(document)
        form = tags.forms['agent-maker-form']
        self.assertEqual(form['attributes']['method'], 'POST')
        self.assertEqual(form['attributes']['action'], '/agents/lookup')
        fields = {attrs['name']: attrs for tag, attrs in form['tags']
                  if tag in ('input', 'select') and attrs.get('name')}
        self.assertEqual(fields['profile']['value'], 'maker-assistant')
        self.assertNotIn('agent_gfavip_user_id', fields)
        for name in ('maker_min_price', 'maker_max_price', 'maker_max_offer_hns',
                     'maker_total_hns_budget', 'maker_max_open_offers', 'maker_max_offers_per_hour'):
            self.assertIn('required', fields[name])
            self.assertNotIn('value', fields[name])
            self.assertNotIn('disabled', fields[name])
        for name in ('maker_max_replies_per_hour', 'maker_max_replies_total'):
            self.assertNotIn('required', fields[name])
            self.assertNotIn('value', fields[name])
        for name in ('include_messages', 'maker_allow_replies'):
            self.assertNotIn('checked', fields[name])
        selected = [attrs['value'] for tag, attrs in form['tags'] if tag == 'option' and 'selected' in attrs]
        self.assertEqual(selected, ['', ''])
        self.assertIn('not in USDT', document)
        for form_id in ('agent-lookup-form', 'agent-uuid-form', 'agent-token-form'):
            self.assertNotIn('maker-assistant', [attrs.get('value') for _, attrs in tags.forms[form_id]['tags']])

    def test_maker_cannot_be_setup_when_lookup_unavailable(self):
        self.app.config.update(AGENT_MAKER_ENABLED=True, GFAVIP_WALLET_LOOKUP_API_KEY=None,
                               GFAVIP_WALLET_API_KEY=None)
        document = self.client.get('/agents').get_data(as_text=True)
        self.assertNotIn('agent-maker-form', Tags(document).forms)
        self.assertIn('Username lookup is required', document)
        self.assertIn('agent-uuid-form', Tags(document).forms)

    def test_review_shows_exact_policy_and_two_unchecked_required_consents(self):
        response = self.review(include_messages='yes')
        self.assertEqual(response.status_code, 200)
        document = response.get_data(as_text=True)
        self.assertNotIn('<script', document.lower())
        self.assertNotIn('<link', document.lower())
        for text in ('Buy HNS', 'USDC · Base', 'Chain 8453', '0.02–0.03 USDC',
                     '25 HNS', '125 HNS', '4 replies/hour', '12 over',
                     'trade_messages:read', 'Revocation does not withdraw existing offers'):
            self.assertIn(text, document)
        inputs = {attrs.get('name'): attrs for tag, attrs in Tags(document).tags if tag == 'input'}
        self.assertEqual(set(inputs), {'csrf_token', 'auth_mode', 'identity_mode', 'lookup_proof',
                                      'confirm_agent', 'confirm_maker_risk'})
        for name in ('confirm_agent', 'confirm_maker_risk'):
            self.assertIn('required', inputs[name])
            self.assertNotIn('checked', inputs[name])
        with self.app.app_context():
            self.assertEqual(AgentConnection.query.count(), 0)

    def test_issuance_and_existing_card_show_policy_usage_and_preserve_revoke_when_disabled(self):
        result = self.approve(self.review())
        self.assertEqual(result.status_code, 200)
        document = result.get_data(as_text=True)
        self.assertNotIn('<script', document.lower())
        self.assertIn('Lifetime published HNS budget', document)
        self.assertIn('125 HNS · 0 used · 125 remaining', document)
        self.assertNotIn('Draft-only access.', document)
        self.assertIn('Public offers and replies within your limits.', document)
        self.app.config['AGENT_MAKER_ENABLED'] = False
        document = self.client.get('/agents').get_data(as_text=True)
        self.assertIn('Manage offers &amp; reply (within my limits)', document)
        self.assertIn('Not active:', document)
        with self.app.app_context():
            connection_id = AgentConnection.query.one().id
        forms = Tags(document).forms.values()
        self.assertTrue(any(form['attributes']['action'] == f'/agents/connections/{connection_id}/revoke'
                            for form in forms))

    def test_unavailable_policy_and_unknown_profile_never_hide_revoke_controls(self):
        self.assertEqual(self.approve(self.review()).status_code, 200)
        with self.app.app_context():
            connection = AgentConnection.query.one()
            connection_id = connection.id
            # Simulate out-of-band damage, not an allowed application mutation.
            db.session.execute(AgentMakerPolicy.__table__.update().values(policy={'broken': True}))
            db.session.commit()
        response = self.client.get('/agents')
        self.assertEqual(response.status_code, 200)
        document = response.get_data(as_text=True)
        self.assertIn('Approved limits are unavailable. Writes are blocked', document)
        self.assertIn(f'/agents/connections/{connection_id}/revoke', document)
        with self.app.app_context():
            db.session.get(AgentConnection, connection_id).scope = 'unknown:scope'
            db.session.commit()
        response = self.client.get('/agents')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Unknown or unavailable profile', response.get_data(as_text=True))
        self.assertIn(f'/agents/connections/{connection_id}/revoke', response.get_data(as_text=True))

    def test_public_guide_never_claims_maker_live_when_flag_off(self):
        self.app.config['AGENT_MAKER_ENABLED'] = False
        document = self.client.get('/ai-assistant').get_data(as_text=True)
        self.assertIn('not enabled on this site yet', document)
        self.assertIn('Trading assistant brief', document)
        self.assertIn('No connection means no private access', document)
        self.app.config['AGENT_MAKER_ENABLED'] = True
        document = self.client.get('/ai-assistant').get_data(as_text=True)
        self.assertIn('Approve buying, selling, or both within limits you set', document)
        self.assertNotIn('The agent API cannot publish', document)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for optional enhancement behavior test')
    def test_optional_javascript_opens_maker_details_and_requires_only_enabled_reply_limits(self):
        self.app.config['AGENT_MAKER_ENABLED'] = True
        document = self.client.get('/agents').get_data(as_text=True)
        script = re.findall(r'<script>(.*?)</script>', document, re.S)[-1]
        harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync(0, 'utf8');
function element() { return {open:false, checked:false, value:'old', events:{}, addEventListener(name, fn) { this.events[name] = fn; }}; }
const setup=element(), replies=element(), hourly=element(), total=element(), link=element();
const elements={'agent-maker-setup':setup,'maker-allow-replies':replies,'maker-max-replies-per-hour':hourly,'maker-max-replies-total':total};
const win={location:{hash:'#agent-maker-setup'}, events:{}, addEventListener(name, fn){this.events[name]=fn;}};
const doc={getElementById(id){return elements[id] || null;},querySelectorAll(selector){return selector.includes('href=') ? [link] : [];}};
vm.runInNewContext(source,{document:doc,window:win});
assert.equal(setup.open,true);
assert.equal(hourly.disabled,true); assert.equal(total.required,false); assert.equal(hourly.value,'');
replies.checked=true; replies.events.change();
assert.equal(hourly.disabled,false); assert.equal(hourly.required,true); assert.equal(total.required,true);
hourly.value='3'; total.value='4'; replies.checked=false; replies.events.change();
assert.equal(hourly.value,''); assert.equal(total.value,''); assert.equal(total.disabled,true);
setup.open=false; win.events.hashchange(); assert.equal(setup.open,true);
setup.open=false; link.events.click(); assert.equal(setup.open,true);
vm.runInNewContext(source,{document:{getElementById(){return null;},querySelectorAll(){return [];}},window:win});
'''
        result = subprocess.run([shutil.which('node'), '-e', harness], input=script,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
