"""One setup, separately bounded grants, and one atomic owner-approved bundle."""
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
import hashlib
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from app import create_app
from models import db, User, P2POffer, P2PTradeMessage
from routes.agents import setup_signer, lookup_signer
from services.agent_workspace import AgentConnection, WorkspaceError, issue_connection
from services.agent_sso import AgentSSOGrant, create_sso_grant
from services.agent_maker import AgentMakerPolicy


ACCOUNT = {'id': '11111111-2222-4333-8444-555555555555', 'username': 'pl-test-assistant', 'accountKind': 'ai_agent'}
POLICY = {'payment_asset': 'usdc-base', 'min_price': '0.003', 'max_price': '0.01',
          'max_offer_hns': '10', 'total_hns_budget': '100', 'max_open_offers': '2',
          'max_offers_per_hour': '3', 'allow_replies': '', 'max_replies_per_hour': '', 'max_replies_total': ''}


class SetupTags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms, self.tags = {}, []
        self.form = None
        self.details = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == 'details':
            self.details.append(attrs)
        if tag == 'form':
            self.form = attrs.get('id', str(len(self.forms)))
            self.forms[self.form] = {'attrs': attrs, 'fields': {}, 'details': list(self.details)}
        elif self.form and tag in ('input', 'select', 'textarea'):
            if attrs.get('name'):
                self.forms[self.form]['fields'][attrs['name']] = attrs

    def handle_endtag(self, tag):
        if tag == 'form':
            self.form = None
        elif tag == 'details':
            self.details.pop()


class TradingAssistantSetupTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app.config.update(AGENT_MAKER_ENABLED=True, AGENT_LISTING_CONVERSATIONS_ENABLED=True,
                               GFAVIP_WALLET_LOOKUP_API_KEY='fake-ui-key-never-sent')
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all([User(id=name, username=name, tier='free') for name in ('owner', 'other')])
        db.session.commit()
        self.human = self.login('owner')
        self.other = self.login('other')
        self.machine = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self, owner):
        client = self.app.test_client()
        with client.session_transaction() as state:
            state['user_id'] = owner
        client.get('/agents')
        return client

    def csrf(self, client=None):
        with (client or self.human).session_transaction() as state:
            return state['agent_workspace_csrf']

    def fields(self, sides=(), inquiries=False, **overrides):
        fields = {'csrf_token': self.csrf(), 'agent_username': ACCOUNT['username'], 'label': 'My assistant'}
        for side in sides:
            fields['publish_' + side] = 'yes'
            fields.update({side + '_' + field: value for field, value in POLICY.items()})
        if inquiries:
            fields['ask_listing_owners'] = 'yes'
        return {**fields, **overrides}

    def review(self, sides=(), inquiries=False, **overrides):
        with patch('routes.agents.lookup_agent_username', return_value=dict(ACCOUNT)) as lookup:
            response = self.human.post('/agents/setup/review', data=self.fields(sides, inquiries, **overrides))
        return response, lookup

    def proof(self, response):
        match = re.search(r'name="setup_proof"[^>]*value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match, response.get_data(as_text=True))
        return unescape(match.group(1))

    def approval_fields(self, proof, client=None):
        plan = setup_signer().loads(proof)
        fields = {'csrf_token': self.csrf(client), 'setup_proof': proof, 'confirm_agent': 'yes'}
        if any(grant['profile'] == 'maker-assistant' for grant in plan['grants']):
            fields['confirm_maker_risk'] = 'yes'
        if any(grant['profile'] == 'listing-conversations' for grant in plan['grants']):
            fields['confirm_inquiries'] = 'yes'
        return fields

    def approve(self, proof, client=None, **overrides):
        return (client or self.human).post('/agents/setup/approve', data={**self.approval_fields(proof, client), **overrides})

    def assert_isolated(self, response):
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        html = response.get_data(as_text=True)
        self.assertNotIn('<script', html.lower())
        self.assertNotIn('<link', html.lower())
        self.assertNotIn('ls_agent_', html)
        return html

    def test_primary_setup_asks_identity_once_and_legacy_forms_are_collapsed(self):
        html = self.human.get('/agents').get_data(as_text=True)
        tags = SetupTags(html)
        self.assertRegex(html, r'<h1[^>]*>Trading assistant</h1>')
        primary = tags.forms['trading-assistant-form']
        self.assertEqual(primary['attrs']['action'], '/agents/setup/review')
        self.assertFalse(primary['details'])
        self.assertEqual(primary['fields']['agent_username']['maxlength'], '201')
        self.assertEqual(primary['fields']['label']['maxlength'], '64')
        self.assertNotIn('profile', primary['fields'])
        for field in ('include_messages', 'publish_buy', 'publish_sell', 'ask_listing_owners',
                      'buy_allow_replies', 'sell_allow_replies'):
            self.assertNotIn('checked', primary['fields'][field])
        for side in ('buy', 'sell'):
            for field in POLICY:
                attrs = primary['fields'][side + '_' + field]
                if field != 'allow_replies':
                    self.assertNotIn('value', attrs)
                self.assertNotIn('required', attrs)  # no-JS server validates only selected policies
        for form in ('agent-lookup-form', 'agent-maker-form', 'agent-uuid-form', 'agent-token-form'):
            self.assertTrue(any(item.get('id') == 'agent-legacy-setup' and 'open' not in item
                                for item in tags.forms[form]['details']))

    def test_watch_only_default_does_not_grant_optional_authority(self):
        response, lookup = self.review()
        self.assert_isolated(response)
        lookup.assert_called_once_with(ACCOUNT['username'])
        proof = self.proof(response)
        grants = setup_signer().loads(proof)['grants']
        self.assertEqual([grant['profile'] for grant in grants], ['trade-assistant'])
        self.assertFalse(grants[0]['include_messages'])
        self.assertIsNone(grants[0]['maker_policy'])
        self.assertEqual(AgentConnection.query.count(), 0)
        html = self.assert_isolated(self.approve(proof))
        connection = AgentConnection.query.one()
        self.assertEqual(connection.scope, 'events:read trades:read')
        self.assertIn(f'X-Liquidity-Connection: {connection.id}', html)
        self.assertIn('Expires', html)
        self.assertNotIn('confirm_inquiries', html)
        self.assertEqual(AgentMakerPolicy.query.count(), 0)

    def test_buy_sell_and_inquiries_create_exact_three_grants_without_duplicate_watcher(self):
        response, _ = self.review(('buy', 'sell'), True, include_messages='yes',
            sell_payment_asset='usdt-ethereum', sell_total_hns_budget='250',
            sell_allow_replies='yes', sell_max_replies_per_hour='4', sell_max_replies_total='16')
        html = self.assert_isolated(response)
        for text in ('BUY HNS', 'SELL HNS', 'USDC · Base', 'USDT · Ethereum mainnet',
                     '100 HNS', '250 HNS', '10 new conversations/day', '40 messages/hour', '200 messages',
                     'trade_messages:read', 'private listing conversations', '3 separate connections'):
            self.assertIn(text, html)
        for name in ('confirm_agent', 'confirm_maker_risk', 'confirm_inquiries'):
            attrs = next(attrs for tag, attrs in SetupTags(html).tags if tag == 'input' and attrs.get('name') == name)
            self.assertIn('required', attrs)
            self.assertNotIn('checked', attrs)
        proof = self.proof(response)
        with patch('routes.agents.lookup_agent_username') as lookup:
            issued = self.assert_isolated(self.approve(proof))
            lookup.assert_not_called()
        connections = AgentConnection.query.order_by(AgentConnection.id).all()
        self.assertEqual([connection.profile_id for connection in connections], ['maker-assistant', 'maker-assistant', 'listing-conversations'])
        self.assertEqual(len({grant.agent_gfavip_user_id for grant in AgentSSOGrant.query.all()}), 1)
        self.assertEqual(AgentSSOGrant.query.first().agent_gfavip_user_id, ACCOUNT['id'])
        policies = AgentMakerPolicy.query.order_by(AgentMakerPolicy.id).all()
        self.assertEqual([record.policy['side'] for record in policies], ['buy', 'sell'])
        self.assertEqual(policies[1].policy['payment_asset'], 'usdt-ethereum')
        self.assertEqual(policies[1].policy['total_hns_budget'], '250')
        self.assertFalse(policies[0].policy['allow_replies'])
        self.assertTrue(policies[1].policy['allow_replies'])
        self.assertNotIn('trade_messages:read', connections[2].scope_list)
        for connection in connections:
            self.assertIn(f'X-Liquidity-Connection: {connection.id}', issued)
            self.assertIn(connection.scope, issued)
            self.assertEqual(connection.expires_at - connection.created_at, timedelta(days=7))
        self.assertIn('buy: connection', issued)
        self.assertIn('sell: connection', issued)
        self.assertIn('inquiries: connection', issued)
        self.assertEqual(P2POffer.query.count(), 0)
        self.assertEqual(P2PTradeMessage.query.count(), 0)

    def test_watch_plus_inquiries_keeps_private_trade_chat_separate(self):
        response, _ = self.review(inquiries=True)
        self.assertEqual(self.approve(self.proof(response)).status_code, 200)
        scopes = [row.scope for row in AgentConnection.query.order_by(AgentConnection.id)]
        self.assertEqual(scopes, ['events:read trades:read', 'listings:read inquiries:read inquiries:write'])

    def test_invalid_missing_or_unselected_policy_fields_fail_before_lookup(self):
        for fields in ({'buy_min_price': ''}, {'buy_max_open_offers': '1.0'},
                       {'buy_allow_replies': 'yes'}, {'buy_side': 'sell'},
                       {'buy_total_hns_budget': '0'}, {'sell_min_price': '1'},
                       {'ask_listing_owners': 'true'}, {'include_messages': 'on'},
                       {'label': 'x' * 65}, {'label': ''}, {'owner_id': 'other'}):
            with self.subTest(fields=fields):
                response, lookup = self.review(('buy',), **fields)
                self.assertEqual(response.status_code, 400)
                lookup.assert_not_called()
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_validation_and_lookup_errors_preserve_explicit_choices_without_new_defaults(self):
        fields = self.fields(('buy', 'sell'), True, include_messages='yes', label='<b>My assistant</b>',
                             buy_min_price='not-a-number', sell_allow_replies='yes',
                             sell_max_replies_per_hour='5', sell_max_replies_total='20')
        response = self.human.post('/agents/setup/review', data=fields)
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        restored = SetupTags(html).forms['trading-assistant-form']['fields']
        self.assertEqual(restored['buy_min_price']['value'], 'not-a-number')
        self.assertEqual(restored['sell_total_hns_budget']['value'], '100')
        self.assertEqual(restored['sell_max_replies_total']['value'], '20')
        self.assertEqual(restored['label']['value'], '<b>My assistant</b>')
        self.assertNotIn('value="<b>', html)
        for field in ('publish_buy', 'publish_sell', 'ask_listing_owners', 'include_messages', 'sell_allow_replies'):
            self.assertIn('checked', restored[field])
        self.assertNotIn('checked', restored['buy_allow_replies'])
        with patch('routes.agents.lookup_agent_username', side_effect=WorkspaceError('No exact username.', 404)):
            response = self.human.post('/agents/setup/review', data=self.fields(('buy', 'sell'), True))
        self.assertEqual(response.status_code, 404)
        restored = SetupTags(response.get_data(as_text=True)).forms['trading-assistant-form']['fields']
        self.assertEqual(restored['agent_username']['value'], ACCOUNT['username'])
        self.assertEqual(restored['sell_max_offer_hns']['value'], '10')
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_error_preservation_never_echoes_secret_like_or_oversized_inputs(self):
        secret = 'gfavip-session-' + 'a' * 64
        response, _ = self.review(('buy',), agent_username=secret, buy_min_price=secret, label='x' * 300)
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertNotIn(secret, html)
        self.assertNotIn('x' * 300, html)
        restored = SetupTags(html).forms['trading-assistant-form']['fields']
        self.assertNotIn('value', restored['agent_username'])
        self.assertNotIn('value', restored['buy_min_price'])

    def test_capabilities_disclose_inquiry_only_when_enabled_and_leave_old_profiles_unchanged(self):
        enabled = self.machine.get('/api/agent/v1/capabilities').get_json()
        self.assertEqual(enabled['listing_conversations']['limits']['new_daily'], 10)
        self.assertEqual(enabled['listing_conversations']['limits']['messages_hourly'], 40)
        self.assertEqual(enabled['listing_conversations']['limits']['messages_total'], 200)
        self.assertEqual(enabled['profiles']['trade-assistant']['scopes'], ['events:read', 'trades:read'])
        self.app.config['AGENT_LISTING_CONVERSATIONS_ENABLED'] = False
        disabled = self.machine.get('/api/agent/v1/capabilities').get_json()
        self.assertNotIn('listing_conversations', disabled)
        self.assertNotIn('listing-conversations', disabled['profiles'])
        for action in ('accept', 'trade-action', 'fund', 'sign', 'withdraw'):
            self.assertIn(action, disabled['forbidden_actions'])

    def test_duplicate_setup_and_approval_fields_are_rejected(self):
        fields = MultiDict(self.fields())
        fields.add('ask_listing_owners', 'yes')
        fields.add('ask_listing_owners', '')
        with patch('routes.agents.lookup_agent_username') as lookup:
            self.assertEqual(self.human.post('/agents/setup/review', data=fields).status_code, 400)
            lookup.assert_not_called()
        response, _ = self.review(inquiries=True)
        fields = MultiDict(self.approval_fields(self.proof(response)))
        fields.add('confirm_inquiries', '')
        self.assertEqual(self.human.post('/agents/setup/approve', data=fields).status_code, 400)

    def test_approval_requires_all_explicit_consents_and_forbids_policy_or_identity_overrides(self):
        response, _ = self.review(('buy', 'sell'), True)
        proof = self.proof(response)
        for fields in ({'confirm_agent': ''}, {'confirm_maker_risk': ''}, {'confirm_inquiries': ''},
                       {'confirm_inquiries': 'true'}, {'agent_username': 'someone-else'},
                       {'label': 'Changed'}, {'buy_max_offer_hns': '100000'},
                       {'profile': 'maker-assistant'}, {'owner_id': 'other'}, {'setup_proof': proof + 'changed'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.approve(proof, **fields).status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(AgentMakerPolicy.query.count(), 0)

    def test_no_extra_consent_field_can_smuggle_unreviewed_permissions(self):
        response, _ = self.review()
        proof = self.proof(response)
        self.assertEqual(self.approve(proof, confirm_inquiries='yes').status_code, 400)
        self.assertEqual(self.approve(proof, confirm_maker_risk='yes').status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_review_binding_expiry_and_durable_replay_marker(self):
        response, _ = self.review(('buy', 'sell'), True)
        proof = self.proof(response)
        with patch('itsdangerous.timed.time.time', return_value=time.time() + 301):
            fields = self.approval_fields(proof)
            self.assertEqual(self.human.post('/agents/setup/approve', data=fields).status_code, 400)
        self.assertEqual(self.approve(proof, client=self.other).status_code, 400)
        self.assertEqual(self.approve(proof, client=self.login('owner')).status_code, 400)
        cookie = self.human.get_cookie('session').value
        self.assertEqual(self.approve(proof).status_code, 200)
        replay = self.app.test_client()
        replay.set_cookie('session', cookie)
        self.assertEqual(self.approve(proof + '=', client=replay).status_code, 409)
        self.assertEqual(AgentConnection.query.count(), 3)
        self.assertEqual(AgentMakerPolicy.query.count(), 2)

    def test_bundle_capacity_failure_creates_nothing_and_keeps_existing_connections(self):
        for index in range(3):
            issue_connection('owner', f'Existing {index}', profile='trade-assistant')
        db.session.commit()
        before = [(row.id, row.scope, row.token_hash) for row in AgentConnection.query.all()]
        response, _ = self.review(('buy', 'sell'), True)
        self.assertEqual(self.approve(self.proof(response)).status_code, 429)
        self.assertEqual([(row.id, row.scope, row.token_hash) for row in AgentConnection.query.all()], before)
        self.assertEqual(AgentSSOGrant.query.count(), 0)
        self.assertEqual(AgentMakerPolicy.query.count(), 0)

    def test_failure_mid_bundle_rolls_back_all_rows_and_does_not_consume_review(self):
        response, _ = self.review(('buy', 'sell'), True)
        proof = self.proof(response)
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise WorkspaceError('Simulated grant failure.', 503)
            return create_sso_grant(*args, **kwargs)
        with patch('routes.agents.create_sso_grant', side_effect=fail_second):
            self.assertEqual(self.approve(proof).status_code, 503)
        self.assertEqual(AgentConnection.query.count(), 0)
        self.assertEqual(AgentSSOGrant.query.count(), 0)
        self.assertEqual(AgentMakerPolicy.query.count(), 0)
        self.assertEqual(self.approve(proof).status_code, 200)
        self.assertEqual(AgentConnection.query.count(), 3)

    def test_hourly_connection_quota_rolls_back_partial_bundle(self):
        now = datetime.utcnow()
        for index in range(19):
            db.session.add(AgentConnection(owner_id='owner', label='Expired test', scope='events:read trades:read',
                token_hash=hashlib.sha256(f'fake-key-{index}'.encode()).hexdigest(),
                created_at=now, expires_at=now - timedelta(seconds=1)))
        db.session.commit()
        response, _ = self.review(inquiries=True)
        self.assertEqual(self.approve(self.proof(response)).status_code, 429)
        self.assertEqual(AgentConnection.query.count(), 19)
        self.assertEqual(AgentSSOGrant.query.count(), 0)

    def test_feature_flags_hide_choices_and_reject_selection_and_stale_review(self):
        response, _ = self.review(('buy',), True)
        proof = self.proof(response)
        for flag in ('AGENT_MAKER_ENABLED', 'AGENT_LISTING_CONVERSATIONS_ENABLED'):
            self.app.config[flag] = False
            self.assertEqual(self.approve(proof).status_code, 403)
            denied, lookup = self.review(('buy',), True)
            self.assertEqual(denied.status_code, 403)
            lookup.assert_not_called()
            html = self.human.get('/agents').get_data(as_text=True)
            fields = SetupTags(html).forms['trading-assistant-form']['fields']
            self.assertNotIn('publish_buy' if flag == 'AGENT_MAKER_ENABLED' else 'ask_listing_owners', fields)
            self.app.config[flag] = True
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_human_login_csrf_and_bearer_browser_boundaries(self):
        self.assertEqual(self.machine.post('/agents/setup/review').status_code, 302)
        self.assertEqual(self.human.post('/agents/setup/review', data={'label': 'No CSRF'}).status_code, 400)
        self.assertEqual(self.human.post('/agents/setup/approve').status_code, 400)
        with patch('routes.agents.lookup_agent_username') as lookup:
            self.assertEqual(self.human.post('/agents/setup/review', data=self.fields(),
                headers={'Authorization': 'Bearer gfavip-session-' + 'a' * 64}).status_code, 403)
            lookup.assert_not_called()
        self.assertEqual(self.human.get('/agents/setup/review').status_code, 405)
        self.assertEqual(self.human.get('/agents/setup/approve').status_code, 405)

    def test_listing_permission_is_never_available_through_legacy_forms_or_proof(self):
        with patch('routes.agents.lookup_agent_username') as lookup:
            result = self.human.post('/agents/lookup', data={**self.fields(), 'profile': 'listing-conversations'})
            self.assertEqual(result.status_code, 400)
            lookup.assert_not_called()
        for auth in ('gfavip-sso', 'scoped-token'):
            result = self.human.post('/agents/connections', data={'csrf_token': self.csrf(), 'label': 'Bypass',
                'auth_mode': auth, 'identity_mode': 'uuid', 'agent_gfavip_user_id': ACCOUNT['id'], 'profile': 'listing-conversations'})
            self.assertEqual(result.status_code, 400)
        # A previously signed legacy proof must not bypass new inquiry consent.
        with self.app.test_request_context('/agents/connections'):
            legacy = lookup_signer().dumps({'owner_id': 'owner',
                'csrf': hashlib.sha256(self.csrf().encode()).hexdigest(), 'nonce': 'a' * 32,
                'account': ACCOUNT, 'label': 'Legacy', 'profile': 'listing-conversations', 'messages': False})
        response = self.human.post('/agents/connections', data={'csrf_token': self.csrf(),
            'auth_mode': 'gfavip-sso', 'identity_mode': 'username', 'lookup_proof': legacy, 'confirm_agent': 'yes'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AgentConnection.query.count(), 0)

    def test_new_setup_does_not_modify_existing_grants_and_labels_are_escaped(self):
        old, _ = issue_connection('owner', 'Legacy', profile='offer-drafts')
        db.session.commit()
        old_identity = (old.scope, old.token_hash, old.expires_at)
        response, _ = self.review(('buy',), True, label='<script>not-owner</script>')
        html = self.assert_isolated(response)
        self.assertIn('&lt;script&gt;not-owner&lt;/script&gt;', html)
        self.assertNotIn('<script>not-owner</script>', html)
        self.assertEqual(self.approve(self.proof(response)).status_code, 200)
        db.session.refresh(old)
        self.assertEqual((old.scope, old.token_hash, old.expires_at), old_identity)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for optional enhancement test')
    def test_javascript_requires_only_selected_side_and_reply_fields_without_guessing_values(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/trading-assistant.js').read_text()
        harness = r'''
const assert=require('node:assert/strict'), vm=require('node:vm');
const source=require('node:fs').readFileSync(0,'utf8');
function input(type='text',value=''){return {type,value,checked:false,events:{},addEventListener(k,f){this.events[k]=f;}};}
const selected=input('checkbox'), replies=input('checkbox'), amount=input('text',''), hourly=input('number',''), total=input('number','');
const fields={hidden:false,querySelectorAll(s){if(s==='[data-policy-required]')return [amount];if(s==='[data-reply-limit]')return [hourly,total];return [amount,replies,hourly,total];}};
const panel={querySelector(s){return s==='[data-publish-side]'?selected:s==='[data-side-fields]'?fields:replies;}};
const form={querySelectorAll(){return [panel];}};
vm.runInNewContext(source,{document:{getElementById(){return form;}}});
assert.equal(fields.hidden,true);assert.equal(amount.disabled,true);assert.equal(amount.required,false);assert.equal(amount.value,'');
selected.checked=true;selected.events.change();
assert.equal(fields.hidden,false);assert.equal(amount.required,true);assert.equal(amount.value,'');
assert.equal(hourly.disabled,true);assert.equal(hourly.required,false);
amount.value='123';replies.checked=true;replies.events.change();
assert.equal(amount.value,'123');assert.equal(hourly.required,true);assert.equal(hourly.value,'');
hourly.value='4';total.value='8';replies.checked=false;replies.events.change();
assert.equal(hourly.value,'');assert.equal(total.value,'');assert.equal(total.required,false);
selected.checked=false;selected.events.change();assert.equal(amount.value,'');assert.equal(replies.checked,false);
// An error response can restore selected, explicit values; initialization preserves those.
selected.checked=true;replies.checked=true;amount.value='456';hourly.value='5';total.value='9';
vm.runInNewContext(source,{document:{getElementById(){return form;}}});
assert.equal(amount.value,'456');assert.equal(hourly.value,'5');assert.equal(total.value,'9');
vm.runInNewContext(source,{document:{getElementById(){return null;}}});
'''
        result = subprocess.run([shutil.which('node'), '-e', harness], input=source,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
