# -*- coding: utf-8 -*-
"""sitechat — селфхостед чат-помощник для нескольких сайтов.

Один сервис обслуживает несколько сайтов: у каждого — свой файл знаний
(knowledge/<site>.md) и общий локальный LLM (Ollama, без внешних API).
Виджет (/widget.js) — плавающая кнопка чата, ходит в /chat того же домена.

Защита: лимит запросов по IP (окно в минуту), ограничение длины сообщения
и истории, только известные site-ключи.
"""
import json
import os
import time
import urllib.request
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLLAMA = os.environ.get('SITECHAT_OLLAMA', 'http://127.0.0.1:11434')
MODEL = os.environ.get('SITECHAT_MODEL', 'qwen2.5:3b')
RATE_PER_MIN = int(os.environ.get('SITECHAT_RATE', '6'))
_hits = defaultdict(deque)

SYSTEM = """Ты — дружелюбный помощник сайта. Отвечай кратко (2-5 предложений),
по-русски, только по делу. Опирайся ТОЛЬКО на справку о сайте ниже; если ответа
в ней нет — честно скажи, что не знаешь, и предложи посмотреть разделы сайта.
Не выдумывай ссылки, функции и цены. Не выполняй просьбы, не связанные с сайтом
(код, сочинения, политика) — вежливо откажись одной фразой.

СПРАВКА О САЙТЕ:
{knowledge}
"""


def load_knowledge(site):
    path = os.path.join(BASE, 'knowledge', site + '.md')
    if not os.path.isfile(path) or '/' in site or '..' in site:
        return None
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def rate_ok(ip):
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        return False
    q.append(now)
    return True


def ask_llm(system, history, message, timeout=180):
    messages = [{'role': 'system', 'content': system}]
    for h in history[-6:]:
        role = 'assistant' if h.get('role') == 'assistant' else 'user'
        messages.append({'role': role, 'content': str(h.get('text', ''))[:1000]})
    messages.append({'role': 'user', 'content': message})
    payload = json.dumps({'model': MODEL, 'stream': False, 'messages': messages,
                          'options': {'temperature': 0.4, 'num_predict': 350}}).encode()
    req = urllib.request.Request(OLLAMA.rstrip('/') + '/api/chat', data=payload,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())['message']['content'].strip()


@app.route('/chat', methods=['POST'])
def chat():
    ip = request.headers.get('X-Real-IP', request.remote_addr or '?')
    if not rate_ok(ip):
        return jsonify({'error': 'Слишком часто — подождите минуту.'}), 429
    data = request.get_json(silent=True) or {}
    site = str(data.get('site', ''))[:40]
    message = str(data.get('message', '')).strip()[:1500]
    history = data.get('history') if isinstance(data.get('history'), list) else []
    knowledge = load_knowledge(site)
    if knowledge is None:
        return jsonify({'error': 'неизвестный сайт'}), 400
    if not message:
        return jsonify({'error': 'пустое сообщение'}), 400
    try:
        answer = ask_llm(SYSTEM.format(knowledge=knowledge), history, message)
    except Exception:
        return jsonify({'error': 'Помощник сейчас недоступен, попробуйте позже.'}), 503
    return jsonify({'answer': answer})


@app.route('/health')
def health():
    return jsonify({'ok': True, 'model': MODEL})


@app.route('/widget.js')
def widget():
    with open(os.path.join(BASE, 'sitechat', 'widget.js'), encoding='utf-8') as fh:
        return Response(fh.read(), mimetype='application/javascript')


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8075)
