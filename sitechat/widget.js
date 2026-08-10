/* sitechat widget: плавающая кнопка чата. Подключение:
   <script src="/sitechat/widget.js" data-site="relmet" defer></script> */
(function () {
  var script = document.currentScript;
  var SITE = (script && script.dataset.site) || 'site';
  var API = (script && script.src.replace(/widget\.js.*$/, '')) + 'chat';
  var history = [];

  var css = '#scw-btn{position:fixed;right:18px;bottom:84px;width:52px;height:52px;' +
    'border-radius:50%;background:#2a78d6;color:#fff;border:0;font-size:24px;' +
    'cursor:pointer;z-index:2000;box-shadow:0 4px 14px rgba(0,0,0,.3)}' +
    '#scw-box{position:fixed;right:18px;bottom:146px;width:330px;max-width:92vw;' +
    'height:420px;max-height:70vh;background:#fff;color:#1a1a1a;border-radius:14px;' +
    'box-shadow:0 8px 30px rgba(0,0,0,.35);display:none;flex-direction:column;' +
    'z-index:2001;font:14px/1.45 system-ui,sans-serif}' +
    '#scw-head{padding:10px 14px;background:#2a78d6;color:#fff;' +
    'border-radius:14px 14px 0 0;font-weight:600}' +
    '#scw-log{flex:1;overflow-y:auto;padding:10px}' +
    '.scw-m{margin:6px 0;padding:8px 10px;border-radius:10px;white-space:pre-wrap}' +
    '.scw-u{background:#e8f0fe;margin-left:20%}' +
    '.scw-a{background:#f1f3f4;margin-right:10%}' +
    '#scw-form{display:flex;border-top:1px solid #ddd}' +
    '#scw-in{flex:1;border:0;padding:10px;font:inherit;outline:none;border-radius:0 0 0 14px}' +
    '#scw-send{border:0;background:#2a78d6;color:#fff;padding:0 16px;cursor:pointer;' +
    'border-radius:0 0 14px 0}';
  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  var btn = document.createElement('button');
  btn.id = 'scw-btn'; btn.title = 'Чат-помощник сайта (локальный ИИ)';
  btn.textContent = '💬';
  var box = document.createElement('div');
  box.id = 'scw-box';
  box.innerHTML = '<div id="scw-head">Помощник сайта · локальный ИИ</div>' +
    '<div id="scw-log"><div class="scw-m scw-a">Здравствуйте! Спросите меня о ' +
    'сайте — что здесь есть и как этим пользоваться. Отвечает локальная модель ' +
    'на нашем сервере, ответ может занять ~полминуты.</div></div>' +
    '<form id="scw-form"><input id="scw-in" placeholder="Ваш вопрос…" ' +
    'autocomplete="off"><button id="scw-send" type="submit">➤</button></form>';
  document.body.appendChild(btn);
  document.body.appendChild(box);

  btn.onclick = function () {
    box.style.display = box.style.display === 'flex' ? 'none' : 'flex';
  };
  function add(cls, text) {
    var log = document.getElementById('scw-log');
    var div = document.createElement('div');
    div.className = 'scw-m ' + cls;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }
  document.getElementById('scw-form').onsubmit = function (e) {
    e.preventDefault();
    var input = document.getElementById('scw-in');
    var text = input.value.trim();
    if (!text) return;
    input.value = '';
    add('scw-u', text);
    var wait = add('scw-a', '…думаю (локальная модель, до минуты)');
    fetch(API, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({site: SITE, message: text, history: history})
    }).then(function (r) { return r.json(); }).then(function (d) {
      wait.textContent = d.answer || d.error || 'что-то пошло не так';
      if (d.link) {
        var a = document.createElement('a');
        a.href = d.link; a.target = '_blank'; a.rel = 'noopener';
        a.textContent = d.link_text || 'Открыть →';
        a.style.cssText = 'display:block;margin-top:6px;font-weight:600';
        wait.appendChild(document.createElement('br'));
        wait.appendChild(a);
      }
      if (d.answer) {
        history.push({role: 'user', text: text});
        history.push({role: 'assistant', text: d.answer});
        history = history.slice(-8);
      }
    }).catch(function () { wait.textContent = 'Помощник недоступен.'; });
  };
})();
