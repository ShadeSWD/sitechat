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

SKILLS = {
    'relmet': """
УМЕНИЕ: если пользователь описал ЗАДАЧУ ВЫБОРА (несколько вариантов и числовые
показатели), верни СТРОГО JSON:
{"type":"task","title":"краткое название задачи",
 "alts":["имя варианта",...],
 "params":[{"name":"показатель","dir":"max" или "min","w":1},...],
 "values":[[числа первого варианта по показателям],...]}
dir: "min" — чем меньше, тем лучше (стоимость, расход, масса), иначе "max".
ПРИМЕР. Текст: «ноутбук X: 60 тыс, 8 часов батарея; ноутбук Y: 45 тыс,
5 часов» → ответ:
{"type":"task","title":"Выбор ноутбука","alts":["X","Y"],
 "params":[{"name":"Цена, тыс","dir":"min","w":1},
           {"name":"Батарея, ч","dir":"max","w":1}],
 "values":[[60,8],[45,5]]}
Обязательно ВСЕ поля; чисел в каждой строке values ровно столько, сколько params.
Если чисел или вариантов в тексте нет — обычный ответ:
{"type":"chat","answer":"..."} (можно попросить прислать данные списком).""",
    'reduktor': """
УМЕНИЕ: если пользователь назвал номер задания и варианта курсовой (1..10),
верни СТРОГО JSON {"type":"variant","task":N,"variant":M}.
Если он привёл свои исходные данные привода (мощность, обороты и т.п.) —
{"type":"chat","answer":"..."} с перечнем: какие значения в какие поля формы
«Произвольные данные» на странице ввести.
Иначе — обычный {"type":"chat","answer":"..."}.""",
    'tracks': """
Отвечай по СПИСКУ ТРЕКОВ из справки: ищи конкретные поездки по названию,
сравнивай годы и километраж, советуй, куда поехать, опираясь на то, где
пользователь уже был (или явно не был). {"type":"chat","answer":"..."}""",
}

SYSTEM = """Ты — дружелюбный помощник сайта. Отвечай кратко (2-5 предложений),
по-русски, только по делу. Опирайся ТОЛЬКО на справку о сайте ниже; если ответа
в ней нет — честно скажи, что не знаешь, и предложи посмотреть разделы сайта.
Не выдумывай ссылки, функции и цены. Не выполняй просьбы, не связанные с сайтом
(код, сочинения, политика) — вежливо откажись одной фразой.

Формат ответа — СТРОГО JSON. По умолчанию:
{{"type":"chat","answer":"твой ответ"}}
{skill}

СПРАВКА О САЙТЕ:
{knowledge}
"""

RELMET_BASE = os.environ.get('SITECHAT_RELMET', 'https://shadeswd.duckdns.org/relmet')
REDUKTOR_BASE = os.environ.get('SITECHAT_REDUKTOR', 'https://shadeswd.duckdns.org/reduktor')
TRACKS_JSON = os.environ.get('SITECHAT_TRACKS', '/var/www/tracks/tracks.json')


def build_relmet_link(task):
    """Задача из LLM → валидация → ссылка на экспресс (?d=). Никаких записей
    в базу: худший исход инъекции — ссылка с мусорными числами."""
    import base64
    alts = [str(a)[:80] for a in task.get('alts', [])][:20]
    params = task.get('params', [])[:15]
    values = task.get('values', [])
    if len(alts) < 2 or not params or len(values) != len(alts):
        return None
    clean_params = []
    for p in params:
        d = p.get('dir')
        if d not in ('max', 'min'):
            return None
        try:
            w = float(p.get('w', 1))
        except (TypeError, ValueError):
            w = 1.0
        clean_params.append({'name': str(p.get('name', '?'))[:80], 'dir': d,
                             'w': max(0.0, min(w, 1000.0))})
    clean_values = []
    for row in values:
        if not isinstance(row, list) or len(row) != len(clean_params):
            return None
        try:
            clean_values.append([float(x) for x in row])
        except (TypeError, ValueError):
            return None
    payload = base64.urlsafe_b64encode(json.dumps({
        'title': str(task.get('title', 'Задача из чата'))[:150],
        'alts': alts, 'params': clean_params, 'values': clean_values,
        'context': 'задача сформулирована в чате сайта и разобрана локальной моделью',
    }, ensure_ascii=False).encode()).decode()
    return RELMET_BASE + '/express/?d=' + payload


def tracks_context(message):
    """Компактная выжимка треков: агрегаты + совпадения по словам вопроса."""
    try:
        with open(TRACKS_JSON, encoding='utf-8') as fh:
            tracks = json.load(fh).get('tracks', [])
    except (OSError, ValueError):
        return ''
    by_year = {}
    for t in tracks:
        y = t.get('y')
        by_year.setdefault(y, [0, 0.0])
        by_year[y][0] += 1
        by_year[y][1] += t.get('km') or 0
    lines = ['Всего треков: %d. По годам (поездок, км): ' % len(tracks) +
             '; '.join(f'{y}: {n}, {km:.0f} км'
                       for y, (n, km) in sorted(by_year.items()) if y)]
    words = [w.lower().strip('.,!?')[:5] for w in message.split()
             if len(w.strip('.,!?')) > 3][:8]
    hits = [t for t in tracks
            if any(w in (t.get('n') or '').lower() for w in words)][:25]
    sample = hits if hits else sorted(
        tracks, key=lambda t: t.get('km') or 0, reverse=True)[:15]
    label = 'Найдено по вопросу' if hits else 'Самые длинные поездки'
    lines.append(label + ':')
    lines += [f"- {t.get('n')} ({t.get('km')} км)" for t in sample]
    return '\n'.join(lines)[:5000]


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
    payload = json.dumps({'model': MODEL, 'stream': False, 'format': 'json',
                          'messages': messages,
                          'options': {'temperature': 0.4, 'num_predict': 600}}).encode()
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
    # relmet: если в сообщении много чисел — сначала целевой экстракционный
    # вызов (надёжнее, чем выбор типа ответа самой моделью)
    if site == 'relmet':
        import re as _re
        if len(_re.findall(r'\d+(?:[.,]\d+)?', message)) >= 4:
            extract = ('Извлеки из текста задачу многокритериального выбора. '
                       'Верни ТОЛЬКО JSON: {"title":"...","alts":["..."],'
                       '"params":[{"name":"...","dir":"max|min","w":1}],'
                       '"values":[[числа]]}. dir="min" для цены/расхода/массы. '
                       'В каждой строке values чисел ровно столько, сколько params. '
                       'Если задачи в тексте нет — {"none":true}.\n\nТЕКСТ: ' + message)
            try:
                raw_task = ask_llm('Ты извлекаешь структуру из текста. Только JSON.',
                                   [], extract)
                obj_task = json.loads(raw_task)
                if not obj_task.get('none'):
                    link = build_relmet_link(obj_task)
                    if link:
                        return jsonify({
                            'answer': f'Разобрала задачу «{obj_task.get("title", "")}»: '
                                      f'{len(obj_task.get("alts", []))} вариантов, '
                                      f'{len(obj_task.get("params", []))} показателей. '
                                      f'По ссылке данные уже подставлены и посчитаны '
                                      f'все методы с консилиумом; числа можно поправить '
                                      f'и сохранить в «Мои объекты».',
                            'link': link,
                            'link_text': 'Открыть задачу в экспресс-анализе →'})
            except Exception:
                pass
    if site == 'tracks':
        knowledge = knowledge + '\n\nСПИСОК ТРЕКОВ:\n' + tracks_context(message)
    system = SYSTEM.format(knowledge=knowledge, skill=SKILLS.get(site, ''))
    try:
        raw = ask_llm(system, history, message)
    except Exception:
        return jsonify({'error': 'Помощник сейчас недоступен, попробуйте позже.'}), 503

    try:
        obj = json.loads(raw)
    except ValueError:
        return jsonify({'answer': raw[:2000]})

    kind = obj.get('type')
    if site == 'relmet' and kind == 'task':
        link = build_relmet_link(obj)
        if link:
            n_alt = len(obj.get('alts', []))
            return jsonify({
                'answer': f'Разобрала задачу «{obj.get("title", "")}»: '
                          f'{n_alt} варианта(ов), {len(obj.get("params", []))} '
                          f'показателя(ей). Открывайте по ссылке — данные уже '
                          f'подставлены, посчитаны все методы и консилиум; там же '
                          f'можно поправить числа и сохранить в «Мои объекты».',
                'link': link, 'link_text': 'Открыть задачу в экспресс-анализе →'})
        return jsonify({'answer': 'Похоже на задачу выбора, но мне не хватило '
                                  'данных: пришлите варианты и числовые показатели '
                                  'списком — по строке на вариант.'})
    if site == 'reduktor' and kind == 'variant':
        try:
            tn = min(max(int(obj.get('task', 1)), 1), 10)
            vn = min(max(int(obj.get('variant', 1)), 1), 10)
        except (TypeError, ValueError):
            tn = vn = 1
        return jsonify({'answer': f'Задание {tn}, вариант {vn} — готово, '
                                  f'открывайте расчёт по ссылке.',
                        'link': f'{REDUKTOR_BASE}/solve?task={tn}&variant={vn}',
                        'link_text': f'Решить задание {tn}.{vn} →'})
    return jsonify({'answer': str(obj.get('answer', raw))[:2000]})


@app.route('/health')
def health():
    return jsonify({'ok': True, 'model': MODEL})


@app.route('/widget.js')
def widget():
    with open(os.path.join(BASE, 'sitechat', 'widget.js'), encoding='utf-8') as fh:
        return Response(fh.read(), mimetype='application/javascript')


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8075)
