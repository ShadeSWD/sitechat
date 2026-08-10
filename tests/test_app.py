# -*- coding: utf-8 -*-
import json
import unittest
from unittest import mock

from sitechat import app as sc


class TestSitechat(unittest.TestCase):
    def setUp(self):
        sc._hits.clear()
        self.client = sc.app.test_client()

    def test_unknown_site_rejected(self):
        r = self.client.post('/chat', json={'site': 'nope', 'message': 'hi'})
        self.assertEqual(r.status_code, 400)

    def test_path_traversal_blocked(self):
        r = self.client.post('/chat', json={'site': '../etc/passwd', 'message': 'x'})
        self.assertEqual(r.status_code, 400)

    def test_empty_message(self):
        r = self.client.post('/chat', json={'site': 'relmet', 'message': '  '})
        self.assertEqual(r.status_code, 400)

    def test_chat_with_fake_llm(self):
        with mock.patch.object(sc, 'ask_llm', return_value='Привет!'):
            r = self.client.post('/chat', json={'site': 'relmet', 'message': 'что это?'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['answer'], 'Привет!')

    def test_rate_limit(self):
        with mock.patch.object(sc, 'ask_llm', return_value='ok'):
            for _ in range(sc.RATE_PER_MIN):
                self.client.post('/chat', json={'site': 'relmet', 'message': 'x'})
            r = self.client.post('/chat', json={'site': 'relmet', 'message': 'x'})
        self.assertEqual(r.status_code, 429)

    def test_llm_down_graceful(self):
        with mock.patch.object(sc, 'ask_llm', side_effect=OSError):
            r = self.client.post('/chat', json={'site': 'relmet', 'message': 'x'})
        self.assertEqual(r.status_code, 503)

    def test_knowledge_in_prompt(self):
        captured = {}
        def fake(system, history, message, timeout=180):
            captured['s'] = system
            return 'ok'
        with mock.patch.object(sc, 'ask_llm', fake):
            self.client.post('/chat', json={'site': 'reduktor', 'message': 'x'})
        self.assertIn('Редуктор Онлайн', captured['s'])
