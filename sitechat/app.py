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
import shutil
import subprocess
import time
import urllib.request
from collections import defaultdict, deque

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLLAMA = os.environ.get('SITECHAT_OLLAMA', 'http://127.0.0.1:11434')
MODEL = os.environ.get('SITECHAT_MODEL', 'qwen2.5:3b')
CLAUDE_BIN = shutil.which('claude') or '/root/.local/bin/claude'
CLAUDE_MODEL = os.environ.get('SITECHAT_CLAUDE_MODEL', 'claude-haiku-4-5-20251001')
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
Отвечай по СПИСКУ ТРЕКОВ из справки. Если в справке есть строка
«ФАКТ ДЛЯ ОТВЕТА» — это точный посчитанный ответ: перескажи его живой фразой,
числа бери ровно оттуда, ничего не пересчитывай и не добавляй лишних поездок. Ищи конкретные поездки по
названию, сравнивай годы и километраж, советуй, куда поехать, опираясь на то,
где пользователь уже был. {"type":"chat","answer":"..."}""",
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


# категории tracks.json и слова, которыми их называют в вопросах
TRACK_CATS = {
    'other': ('EUC (моноколесо)', ['euc', 'моноколес', 'моноколёс', 'колес', 'колёс']),
    'bicycle': ('Велосипед', ['вело', 'велик', 'велосипед', 'bike', 'bicycle']),
    'water': ('SUP/каяк', ['sup', 'сап', 'сапборд', 'каяк', 'kayak', 'греб']),
    'windsurf': ('Виндсерф/wing', ['виндсерф', 'винг', 'windsurf', 'parawing', 'катамаран']),
    'hike': ('Пешком', ['пешком', 'пеших', 'пешие', 'hike', 'поход', 'прогулк']),
    'ski': ('Лыжи', ['лыж', 'ski']),
    'nordic': ('Коньки', ['коньк', 'nordic']),
    'paraglide': ('Параплан', ['параплан', 'paraglid']),
    'quad': ('Квадро/эндуро/снегоход', ['квадр', 'эндуро', 'снегоход', 'питбайк', 'atv']),
    'motoboat': ('Мотолодка/гидроцикл', ['мотолодк', 'гидроцикл', 'моторк', 'jetski']),
    'yacht': ('Яхта', ['яхт', 'yacht']),
    'horse': ('Конные', ['конн', 'лошад', 'horse']),
    'bmw': ('Авто (BMW)', ['бмв', 'bmw', 'машин', 'авто ']),
    'rv': ('Автодом', ['автодом', 'кемпер', 'camper', ' rv']),
    'train': ('Поезд', ['поезд', 'поезда']),
    'suburban': ('Электричка', ['электричк', 'ласточк']),
    'flight': ('Самолёт', ['самолёт', 'самолет', 'перелёт', 'перелет', 'рейс']),
    'ferry': ('Паром/теплоход', ['паром', 'теплоход', 'метеор', 'катер', 'круиз']),
    'cable': ('Канатки/фуникулёры', ['канатк', 'фуникул', 'подъёмник', 'подъемник']),
    'diveboat': ('Дайв-бот', ['дайв-бот', 'дайвбот']),
}
ACT_WORDS = {'skydives': ['прыж', 'парашют', 'skydiv'], 'dives': ['дайв', 'погружен', 'scuba'],
             'tunnelMinutes': ['труб', 'tunnel', 'аэродинамич'], 'ropejumps': ['роуп', 'rope'],
             'paraglides': ['параплан']}


def tracks_context(message):
    """Выжимка под вопрос: точные агрегаты считает КОД, модель их пересказывает.
    Понимает категории по синонимам, подстроки в именах (MixWheels, город...),
    годы и активности из логбука."""
    try:
        with open(TRACKS_JSON, encoding='utf-8') as fh:
            tracks = json.load(fh).get('tracks', [])
    except (OSError, ValueError):
        return ''
    msg = message.lower()
    lines = []
    by_year = {}
    for t in tracks:
        y = t.get('y')
        by_year.setdefault(y, [0, 0.0])
        by_year[y][0] += 1
        by_year[y][1] += t.get('km') or 0
    lines.append('Всего треков: %d, суммарно %.0f км. По годам: ' %
                 (len(tracks), sum(v[1] for v in by_year.values())) +
                 '; '.join(f'{y}: {n} шт, {km:.0f} км'
                           for y, (n, km) in sorted(by_year.items()) if y))
    # фильтры из вопроса
    import re as _re
    sel = tracks
    desc = []
    # из текста для матчинга категорий вырезаем «вопросные» слова: «поездки»
    # содержит «поезд» и раньше ловило категорию Поезд
    msg_cat = _re.sub(r'\b(поездк\w*|поездок|проехал\w*|съездит\w*|ездил\w*|'
                      r'покатушк\w*|наездил\w*)\b', ' ', msg)
    cats = [c for c, (_, syns) in TRACK_CATS.items() if any(s in msg_cat for s in syns)]
    if cats:
        sel = [t for t in sel if t.get('c') in cats]
        desc.append('категория ' + '/'.join(TRACK_CATS[c][0] for c in cats))
    years = [int(y) for y in _re.findall(r'\b(20\d\d)\b', msg)]
    if years:
        sel = [t for t in sel if t.get('y') in years]
        desc.append('год ' + '/'.join(map(str, years)))
    # слова-подстроки для поиска по именам (латиница и «содержательные» русские)
    stop = {'сколько', 'какой', 'какая', 'какие', 'когда', 'где', 'самый', 'самая',
            'самое', 'всего', 'итого', 'проехала', 'проехал', 'проехали', 'ездила',
            'ездил', 'ездили', 'наездила', 'намотала', 'намотал', 'была', 'был',
            'были', 'быть', 'треки', 'трек', 'треков', 'поездки', 'поездка',
            'поездок', 'этом', 'году', 'год', 'года', 'меня', 'мной', 'мои', 'моих',
            'моя', 'мой', 'что', 'как', 'для', 'при', 'над', 'под', 'про', 'это',
            'вообще', 'примерно', 'общая', 'общий', 'сумма', 'километр', 'километров',
            'километра', 'покажи', 'скажи', 'посчитай', 'дистанция', 'пробег'}
    # бренды/названия, которые пишут по-русски → как они лежат в именах треков
    ALIAS = {'mixwheels': ('миксвилс', 'миксвилз', 'миксвилc', 'миксвил', 'mixwheels'),
             'flystation': ('флайстейшн', 'flystation'),
             'narvaman': ('нарваман', 'narvaman')}
    for canon, forms in ALIAS.items():
        if any(f in msg for f in forms):
            msg = msg + ' ' + canon
            message = message + ' ' + canon
    used = {s for c in cats for s in TRACK_CATS[c][1]}
    words = []
    for w in _re.findall(r'[a-zA-Zа-яёА-ЯЁ][\w-]{3,}', message):
        wl = w.lower()
        if wl in stop or any(wl.startswith(u) or u.startswith(wl) for u in used):
            continue
        if wl in words:
            continue
        words.append(wl)
    name_hits = []
    if words:
        name_hits = [t for t in sel
                     if all(w in (t.get('n') or '').lower() for w in words[:4])]
        if not name_hits:
            name_hits = [t for t in sel
                         if any(w in (t.get('n') or '').lower() for w in words[:4])]
        if name_hits:
            sel = name_hits
            desc.append('в названии: ' + ', '.join(words[:4]))
    if desc:
        km_sel = sum(t.get('km') or 0 for t in sel)
        y_sel = {}
        for t in sel:
            y_sel.setdefault(t.get('y'), [0, 0.0])
            y_sel[t.get('y')][0] += 1
            y_sel[t.get('y')][1] += t.get('km') or 0
        human = []
        if cats:
            human.append('/'.join(TRACK_CATS[c][0] for c in cats))
        if name_hits and words:
            human.append('«' + ' '.join(words[:4]) + '»')
        if years:
            human.append('за ' + '/'.join(map(str, years)))
        lines.append('ФАКТ ДЛЯ ОТВЕТА (числа брать отсюда, своими словами): %s — '
                     '%d поездок, %.0f км.' %
                     (' '.join(human) or 'по вашему запросу', len(sel), km_sel))
        if len(y_sel) > 1:
            lines.append('Из них по годам: ' + '; '.join(
                f'{y}: {n} шт, {km:.0f} км' for y, (n, km) in sorted(y_sel.items()) if y))
        top = sorted(sel, key=lambda t: t.get('km') or 0, reverse=True)[:10]
        lines.append('Примеры (самые длинные из найденных):')
        lines += [f"- {t.get('n')} ({t.get('km'):.0f} км)" for t in top]
    else:
        top = sorted(tracks, key=lambda t: t.get('km') or 0, reverse=True)[:10]
        lines.append('Самые длинные поездки:')
        lines += [f"- {t.get('n')} ({t.get('km'):.0f} км)" for t in top]
    # активности из логбука, если спрашивают про них
    if any(s in msg for ws in ACT_WORDS.values() for s in ws):
        try:
            with open(os.path.join(os.path.dirname(TRACKS_JSON), 'activities.json'),
                      encoding='utf-8') as fh:
                a = json.load(fh)
            lines.append('Логбук активностей: прыжков с парашютом %s (за 365 дней %s), '
                         'дайвов %s, аэротрубы %d мин, роупджампов %s, парапланов %s.' % (
                             a.get('skydives'), a.get('skydives365'), a.get('dives'),
                             round(a.get('tunnelMinutes') or 0), a.get('ropejumps'),
                             a.get('paraglides')))
        except (OSError, ValueError):
            pass
    # страны/регионы, если спрашивают
    if any(s in msg for s in ('стран', 'регион', 'росси', 'мир', 'посет', 'был')):
        try:
            with open(os.path.join(os.path.dirname(TRACKS_JSON), 'visits.json'),
                      encoding='utf-8') as fh:
                v = json.load(fh)
            cs = v.get('countries') or []
            rr = v.get('ruRegions') or []
            lines.append('Стран посещено: %d (%s). Регионов России: %d (%s).' % (
                len(cs), ', '.join(c['name'] for c in cs[:40]),
                len(rr), ', '.join(r['name'] for r in rr[:45])))
        except (OSError, ValueError):
            pass
    # планы поездок — для советов «куда съездить»
    if any(s in msg for s in ('куда', 'совет', 'план', 'предлож', 'выходн', 'поехать',
                              'съездить', 'маршрут', 'идеи', 'новое')):
        try:
            with open(TRACKS_JSON, encoding='utf-8') as fh:
                routes = json.load(fh).get('routes', [])
            if routes:
                lines.append('ГОТОВЫЕ ПЛАНЫ МАРШРУТОВ на сайте (слой «План мира» и '
                             'секция «Планы»), их можно предлагать: ' +
                             '; '.join('%s (%.0f км)' % (r.get('n', '').replace('_', ' '),
                                                         r.get('km') or 0) for r in routes))
        except (OSError, ValueError):
            pass
    return '\n'.join(lines)[:7000]


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
    """LLM-вызов: Claude Haiku через локальный CLI `claude -p` (подписка
    Claude Code хозяина сервера). Ollama с сервера убрана по требованию
    владельца — локальная модель занимала память и своп; переменная
    SITECHAT_OLLAMA оставлена как явный путь отката."""
    if os.environ.get('SITECHAT_BACKEND') == 'ollama':
        messages = [{'role': 'system', 'content': system}]
        for h in history[-6:]:
            role = 'assistant' if h.get('role') == 'assistant' else 'user'
            messages.append({'role': role, 'content': str(h.get('text', ''))[:1000]})
        messages.append({'role': 'user', 'content': message})
        payload = json.dumps({'model': MODEL, 'stream': False, 'format': 'json',
                              'messages': messages,
                              'options': {'temperature': 0.4, 'num_predict': 600,
                                          'num_ctx': 8192}}).encode()
        req = urllib.request.Request(OLLAMA.rstrip('/') + '/api/chat', data=payload,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())['message']['content'].strip()
    parts = [system, '', 'ДИАЛОГ ДО ЭТОГО:']
    for h in history[-6:]:
        who = 'Помощник' if h.get('role') == 'assistant' else 'Пользователь'
        parts.append(who + ': ' + str(h.get('text', ''))[:1000])
    parts += ['', 'Пользователь: ' + message, '',
              'Ответь СТРОГО одним JSON-объектом по правилам выше, без пояснений вокруг.']
    env = dict(os.environ)
    env.setdefault('HOME', '/root')
    r = subprocess.run(
        [CLAUDE_BIN, '--model', CLAUDE_MODEL, '-p'],
        input='\n'.join(parts).encode(), capture_output=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError('claude cli: ' + r.stderr.decode()[:200])
    text = r.stdout.decode().strip()
    # модель любит заворачивать JSON в ```‑ограду — срезаем до чистого объекта
    if text.startswith('```'):
        text = text.split('\n', 1)[-1]
        text = text.rsplit('```', 1)[0].strip()
    return text


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
    tr_ctx = ''
    if site == 'tracks':
        tr_ctx = tracks_context(message)
        knowledge = knowledge + '\n\nСПИСОК ТРЕКОВ:\n' + tr_ctx
    system = SYSTEM.format(knowledge=knowledge, skill=SKILLS.get(site, ''))
    try:
        raw = ask_llm(system, history, message)
    except Exception:
        # модель лежит, но точный факт уже посчитан кодом — отдаём его напрямую
        fact = next((l for l in tr_ctx.split('\n') if l.startswith('ФАКТ ДЛЯ ОТВЕТА')), '')
        if fact:
            return jsonify({'answer': fact.replace('ФАКТ ДЛЯ ОТВЕТА (числа брать отсюда, своими словами): ', '')})
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
