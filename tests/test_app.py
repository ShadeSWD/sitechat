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


class TestSkills(unittest.TestCase):
    def setUp(self):
        sc._hits.clear()
        self.client = sc.app.test_client()

    def _fake(self, obj):
        return mock.patch.object(sc, 'ask_llm', return_value=json.dumps(obj))

    def test_relmet_task_builds_safe_link(self):
        task = {'type': 'task', 'title': 'Выбор насоса',
                'alts': ['А', 'Б'],
                'params': [{'name': 'Цена', 'dir': 'min', 'w': 1},
                           {'name': 'Ресурс', 'dir': 'max', 'w': 2}],
                'values': [[10, 5], [8, 7]]}
        with self._fake(task):
            r = self.client.post('/chat', json={'site': 'relmet', 'message': 'вот задача'})
        d = r.get_json()
        self.assertIn('/relmet/express/?d=', d['link'])

    def test_relmet_task_injection_rejected(self):
        for bad in (
            {'type': 'task', 'alts': ['А', 'Б'],
             'params': [{'name': 'x', 'dir': 'max; DROP TABLE', 'w': 1}],
             'values': [[1], [2]]},
            {'type': 'task', 'alts': ['А', 'Б'],
             'params': [{'name': 'x', 'dir': 'max', 'w': 1}],
             'values': [['<script>'], [2]]},
            {'type': 'task', 'alts': ['только один'],
             'params': [{'name': 'x', 'dir': 'max', 'w': 1}], 'values': [[1]]},
        ):
            with self._fake(bad):
                r = self.client.post('/chat', json={'site': 'relmet', 'message': 'x'})
            self.assertNotIn('link', r.get_json(), bad)

    def test_reduktor_variant_clamped(self):
        with self._fake({'type': 'variant', 'task': 99, 'variant': -5}):
            r = self.client.post('/chat', json={'site': 'reduktor', 'message': 'задание 99'})
        self.assertIn('task=10', r.get_json()['link'])
        self.assertIn('variant=1', r.get_json()['link'])

    def test_plain_json_chat(self):
        with self._fake({'type': 'chat', 'answer': 'Привет'}):
            r = self.client.post('/chat', json={'site': 'tracks', 'message': 'привет'})
        self.assertEqual(r.get_json()['answer'], 'Привет')
