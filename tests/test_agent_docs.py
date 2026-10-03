from pathlib import Path
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from app import create_app
from routes.agent_docs import DOCUMENT_PATHS


class AgentDocumentationTests(unittest.TestCase):
    PATHS = {
        '/skill.md': ('skill.md',),
        '/SKILL.md': ('skill.md',),
        '/skill_api.md': ('docs', 'agent-api.md'),
        '/api/docs.md': ('docs', 'agent-api.md'),
        '/skill_prompt.md': ('docs', 'agent-prompt.md'),
    }

    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()

    def test_public_get_and_head_are_inline_utf8_text_without_session(self):
        content = '# Agent documentation\nAmounts are exact — tokens stay private.\n'
        with patch('routes.agent_docs.Path.read_text', return_value=content):
            for path, relative_path in self.PATHS.items():
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_data(as_text=True), content)
                    self.assertEqual(response.headers['Content-Type'], 'text/plain; charset=utf-8')
                    self.assertEqual(response.headers['Content-Disposition'],
                                     f'inline; filename="{relative_path[-1]}"')
                    self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                    self.assertNotIn('Set-Cookie', response.headers)
                    head = self.client.head(path)
                    self.assertEqual(head.status_code, 200)
                    self.assertEqual(head.data, b'')
                    for header in ('Content-Type', 'Content-Disposition', 'X-Content-Type-Options', 'Content-Length'):
                        self.assertEqual(head.headers[header], response.headers[header])
        with self.client.session_transaction() as state:
            self.assertNotIn('user_id', state)

    def test_aliases_read_only_the_fixed_utf8_document(self):
        for path, relative_path in self.PATHS.items():
            with self.subTest(path=path), patch('routes.agent_docs.Path.read_text', autospec=True, return_value='safe') as read:
                response = self.client.get(path + '?path=../../config.py&filename=app.py')
                self.assertEqual(response.status_code, 200)
                read.assert_called_once_with(Path(self.app.root_path, *relative_path), encoding='utf-8')

    def test_missing_document_is_inline_safe_404_for_get_and_head(self):
        with patch('routes.agent_docs.Path.read_text', side_effect=FileNotFoundError('private filesystem path')):
            for path in self.PATHS:
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.mimetype, 'text/plain')
                    self.assertTrue(response.headers['Content-Disposition'].startswith('inline;'))
                    self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                    self.assertNotIn('private filesystem path', response.get_data(as_text=True))
                    head = self.client.head(path)
                    self.assertEqual(head.status_code, 404)
                    self.assertEqual(head.data, b'')

    def test_api_docs_redirects_to_one_source_document(self):
        for method in ('get', 'head'):
            response = getattr(self.client, method)('/api/docs')
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers['Location'], '/skill_api.md')
        with patch('routes.agent_docs.Path.read_text', return_value='# API reference'):
            response = self.client.get('/api/docs', follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'text/plain')

    def test_no_arbitrary_path_or_mutating_document_endpoint(self):
        paths = ('/skill.md/../config.py', '/skill.md/%2e%2e/config.py',
                 '/skill_api.md/../../app.py', '/api/docs/../../config.py',
                 '/docs/agent-api.md', '/skill_config.py', '/skill_unknown.md')
        with patch('routes.agent_docs.Path.read_text') as read:
            for path in paths:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)
            read.assert_not_called()
        for path in self.PATHS:
            self.assertEqual(self.client.post(path).status_code, 405)

    def test_documentation_sources_and_public_markdown_links_exist(self):
        for relative_path in DOCUMENT_PATHS.values():
            source = Path(self.app.root_path, *relative_path)
            with self.subTest(source=str(source)):
                self.assertTrue(source.is_file(), f'Missing public documentation source: {source.name}')
                content = source.read_text(encoding='utf-8')
                self.assertTrue(content.strip())
                for target in re.findall(r'\]\(([^)\s]+)', content):
                    parsed = urlsplit(target)
                    if parsed.netloc not in ('', 'liquidity.spot'):
                        continue
                    if parsed.path.startswith('/') and parsed.path.endswith('.md'):
                        self.assertIn(parsed.path, self.PATHS)
                        self.assertEqual(self.client.get(parsed.path).status_code, 200)


if __name__ == '__main__':
    unittest.main()
