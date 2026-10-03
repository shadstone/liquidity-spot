"""The public human guide explains consent without creating an account or grant."""
from html.parser import HTMLParser
from pathlib import Path
import unittest
from unittest.mock import patch

from app import create_app
from models import db


class GuideHTML(HTMLParser):
    """Small standard-library DOM inventory, including decoded textarea content."""
    VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = {'tag': tag, 'attrs': dict(attrs), 'parents': list(self.stack), 'text': []}
        self.elements.append(element)
        if tag not in self.VOID_TAGS:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        self.text.append(value)
        for element in self.stack:
            element['text'].append(value)

    def by_id(self, element_id):
        return [element for element in self.elements if element['attrs'].get('id') == element_id]

    def links(self):
        return [element for element in self.elements if element['tag'] == 'a']


class AgentGuideTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        self.assertFalse(self.app.config['ALLOW_EXTERNAL_HTTP'])
        # Intentionally no db.create_all(): the public guide must not require
        # an account, trade tables, or any database-backed onboarding operation.

    def guide(self):
        response = self.client.get('/ai-assistant')
        self.assertEqual(response.status_code, 200)
        return response, GuideHTML(response.get_data(as_text=True))

    def render_base(self, **overrides):
        context = {
            'current_user': None, 'nav_notification_count': 0, 'nav_notifications': [],
            'hellobar_items': [], 'hellobar_count': 0, 'hellobar_primary': None,
            'guest_recovery_notice': None,
        }
        context.update(overrides)
        with self.app.test_request_context('/tutorial'):
            # Direct template rendering supplies neutral notifications without
            # querying a live user or changing any room's last-viewed state.
            return GuideHTML(self.app.jinja_env.get_template('base.html').render(**context))

    def test_public_guide_get_head_creates_no_session_account_or_connection(self):
        with patch.object(db.session, 'add', side_effect=AssertionError('Unexpected database write')), \
             patch.object(db.session, 'add_all', side_effect=AssertionError('Unexpected database write')), \
             patch.object(db.session, 'commit', side_effect=AssertionError('Unexpected database commit')), \
             patch('routes.main._ensure_session_user', side_effect=AssertionError('Unexpected guest provisioning')):
            response, _ = self.guide()
            self.assertEqual(response.mimetype, 'text/html')
            self.assertNotIn('Set-Cookie', response.headers)
            head = self.client.head('/ai-assistant')
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.mimetype, 'text/html')
            self.assertEqual(head.data, b'')
            self.assertNotIn('Set-Cookie', head.headers)
        with self.client.session_transaction() as state:
            self.assertNotIn('user_id', state)
            self.assertNotIn('agent_workspace_csrf', state)

    def test_copy_prompt_comes_from_existing_routine_document(self):
        source = Path(self.app.root_path, 'docs', 'agent-prompt.md').read_text(encoding='utf-8')
        before, delimiter, expected = source.partition('\n---\n')
        self.assertTrue(delimiter, 'Routine document needs the copyable-prompt delimiter.')
        self.assertTrue(before.strip())
        _, document = self.guide()
        textarea = document.by_id('agent-setup-prompt')
        self.assertEqual(len(textarea), 1)
        self.assertEqual(textarea[0]['tag'], 'textarea')
        self.assertIn('readonly', textarea[0]['attrs'])
        self.assertEqual(''.join(textarea[0]['text']).strip(), expected.strip())
        button = document.by_id('copy-agent-prompt')
        self.assertEqual(len(button), 1)
        self.assertEqual(button[0]['attrs'].get('type'), 'button')
        status = document.by_id('copy-agent-status')
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]['attrs'].get('aria-live'), 'polite')

    def test_prompt_is_escaped_not_interpreted_as_html(self):
        unsafe = 'Intro\n---\nReview </textarea><script id="injected-prompt">bad()</script> literally.'
        with patch('routes.agent_docs.Path.read_text', return_value=unsafe):
            _, document = self.guide()
        self.assertEqual(document.by_id('injected-prompt'), [])
        textarea = document.by_id('agent-setup-prompt')
        self.assertEqual(''.join(textarea[0]['text']).strip(), unsafe.partition('\n---\n')[2].strip())

    def test_guide_has_docs_workspace_links_and_human_control_boundaries(self):
        _, document = self.guide()
        links = {element['attrs'].get('href') for element in document.links()}
        for path in ('/skill.md', '/skill_api.md', '/skill_prompt.md', '/agents'):
            self.assertTrue(path in links or 'https://liquidity.spot' + path in links, path)
        text = ' '.join(' '.join(document.text).lower().split())
        self.assertIn('poll', text)
        self.assertTrue('60-second' in text or '60 seconds' in text)
        self.assertTrue(any(phrase in text for phrase in
                            ('human', 'your approval', 'your review', 'you stay in control')))
        self.assertRegex(text, r'(?:do not|cannot|never|does not|won.t)[^.\n]{0,100}(?:send money|settle|transfer|execute trades|send funds)')
        self.assertIn('grant', text)

    def test_missing_prompt_keeps_public_guide_and_document_fallback_without_copy_button(self):
        with patch('routes.agent_docs.Path.read_text', side_effect=FileNotFoundError('private path')):
            response, document = self.guide()
        self.assertEqual(response.mimetype, 'text/html')
        self.assertNotIn('private path', response.get_data(as_text=True))
        links = {element['attrs'].get('href') for element in document.links()}
        self.assertTrue('/skill_prompt.md' in links or 'https://liquidity.spot/skill_prompt.md' in links)
        self.assertEqual(document.by_id('copy-agent-prompt'), [])
        self.assertEqual(document.by_id('agent-setup-prompt'), [])

    def test_guide_does_not_accept_post_or_create_anything(self):
        response = self.client.post('/ai-assistant', data={'profile': 'trade-assistant', 'agent_id': 'not-a-grant'})
        self.assertEqual(response.status_code, 405)
        self.assertNotIn('Set-Cookie', response.headers)

    def test_sticky_banner_is_public_and_loads_existing_shared_script(self):
        response = self.client.get('/tutorial')
        self.assertEqual(response.status_code, 200)
        document = GuideHTML(response.get_data(as_text=True))
        banner = document.by_id('agent-help-banner')
        self.assertEqual(len(banner), 1)
        self.assertTrue(any('sticky' in element['attrs'].get('class', '').split()
                            for element in [banner[0], *banner[0]['parents']]))
        dismiss = document.by_id('dismiss-agent-help')
        self.assertEqual(len(dismiss), 1)
        self.assertEqual(dismiss[0]['attrs'].get('type'), 'button')
        banner_links = [element for element in document.links() if banner[0] in element['parents']]
        self.assertTrue(any(element['attrs'].get('href') == '/ai-assistant' for element in banner_links))
        script_url = '/static/js/agent-guide.js'
        scripts = [element for element in document.elements if element['tag'] == 'script'
                   and element['attrs'].get('src') == script_url]
        self.assertEqual(len(scripts), 1)
        self.assertTrue(Path(self.app.static_folder, 'js', 'agent-guide.js').is_file())
        script = self.client.get(script_url)
        self.assertEqual(script.status_code, 200)
        self.assertIn(script.mimetype, ('text/javascript', 'application/javascript'))

    def test_banner_does_not_replace_or_wrap_recovery_and_active_trade_notices(self):
        recovery = {'title': 'Recovery fixture', 'detail': 'Save this account safely', 'href': '/recovery-fixture'}
        trade = {'kind': 'P2P', 'label': 'Trade fixture #13', 'detail': 'Action fixture awaiting you',
                 'href': '/p2p/trades/13', 'user_is_next': True}
        document = self.render_base(guest_recovery_notice=recovery, hellobar_items=[trade],
                                    hellobar_count=1, hellobar_primary=trade)
        self.assertEqual(len(document.by_id('agent-help-banner')), 1)
        text = ' '.join(document.text)
        self.assertIn('Recovery fixture', text)
        self.assertIn('Trade fixture #13', text)
        self.assertIn('Action fixture awaiting you', text)
        for href in ('/recovery-fixture', '/p2p/trades/13'):
            links = [element for element in document.links() if element['attrs'].get('href') == href]
            self.assertTrue(links)
            self.assertTrue(all(parent['attrs'].get('id') != 'agent-help-banner'
                                for element in links for parent in element['parents']))


if __name__ == '__main__':
    unittest.main()
