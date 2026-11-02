"""Small local VK ID pages; no external scripts, assets or returned tokens."""

from html import escape


STYLE = """
body{font-family:system-ui,sans-serif;max-width:560px;margin:12vh auto;padding:24px;color:#17212b}
h1{font-size:26px}p{line-height:1.6}button{border:0;border-radius:10px;padding:14px 24px;
background:#0077ff;color:white;font-size:17px;cursor:pointer}button:disabled{opacity:.6}
a{color:#0066dd}#message{color:#a33}
"""


def page(title, content, nonce):
    return (f'<!doctype html><html lang="ru"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><style nonce="{nonce}">{STYLE}</style>'
            f'<body><h1>{escape(title)}</h1>{content}</body></html>')


def login_page(nonce):
    # PKCE verifier originates in the browser, as required by VK ID's Web flow.
    script = """
const button = document.querySelector('button');
button.addEventListener('click', async () => {
  button.disabled = true;
  try {
    const bytes = crypto.getRandomValues(new Uint8Array(64));
    const verifier = btoa(String.fromCharCode(...bytes)).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
    const challenge = btoa(String.fromCharCode(...digest)).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    const result = await fetch('/auth/vk/prepare', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'AudienceDashboard'},
      body: JSON.stringify({code_verifier: verifier, code_challenge: challenge})
    });
    const data = await result.json();
    if (!result.ok) throw new Error(data.detail || 'Не удалось начать вход.');
    const target = new URL(data.authorize_url);
    if (target.origin !== 'https://id.vk.ru' || target.pathname !== '/authorize') throw new Error('Некорректный адрес входа.');
    window.location.assign(target.href);
  } catch (error) {
    document.querySelector('#message').textContent = 'Не удалось начать вход. Обновите страницу и повторите попытку.';
    button.disabled = false;
  }
});
"""
    content = ('<p>Подключите свой аккаунт к локальному дашборду аудитории. '
               'После входа вы вернётесь в приложение.</p>'
               '<button type="button">Войти через VK ID</button><p id="message" role="alert"></p>'
               f'<script nonce="{nonce}">{script}</script>')
    return page("Подключение ВКонтакте", content, nonce)


def error_page(message, frontend_url, nonce):
    return page("Вход не завершён", f'<p>{escape(message)}</p>'
                f'<p><a href="{escape(frontend_url, quote=True)}">Вернуться в дашборд</a></p>', nonce)
