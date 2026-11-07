"""Read-only, password-protected local web dashboard for Gemini usage.

The server uses only the Python standard library. It binds to loopback by
default and is disabled unless an explicit password is configured.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from services import analytics

logger = logging.getLogger(__name__)

_PERIODS = frozenset({"today", "7", "30", "all"})

_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Shorts Chill — Gemini Analytics</title>
<style>
:root{font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;color:#e8edf5;background:#10151e}
*{box-sizing:border-box}body{margin:0;padding:28px;max-width:1400px;margin-inline:auto}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:24px}
h1{font-size:24px;margin:0}p{color:#9ba8ba;margin:6px 0 0}
select,button{font:inherit;padding:10px 14px;border:1px solid #39475b;border-radius:9px;background:#1a2432;color:#e8edf5}
button{cursor:pointer}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:14px;margin-bottom:22px}
.card,section{background:#182230;border:1px solid #2a394d;border-radius:13px;padding:17px}
.card small{display:block;color:#9ba8ba;margin-bottom:8px}.value{font-size:23px;font-weight:700;overflow-wrap:anywhere}
section{margin-top:18px;overflow:hidden}h2{font-size:17px;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #2a394d;vertical-align:top}
th{color:#9ba8ba;font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:8px;background:#597fae;border-radius:10px;min-width:2px;margin-top:5px}
.muted{color:#9ba8ba}.error{color:#ffb4a9;padding:14px 0}
@media(max-width:640px){body{padding:16px}h1{font-size:20px}table{font-size:12px}th,td{padding:8px 5px}}
</style>
</head>
<body>
<header>
<div><h1>Gemini — расходы и токены</h1><p>Shorts Chill Shop Bot · локальная аналитика</p></div>
<div><select id="period" aria-label="Период">
<option value="today">Сегодня (UTC)</option><option value="7">7 дней</option>
<option value="30" selected>30 дней</option><option value="all">Всё время</option>
</select> <button id="refresh">Обновить</button></div>
</header>
<div class="grid">
<div class="card"><small>Вызовов Gemini</small><div class="value" id="attempts">—</div></div>
<div class="card"><small>Входящие токены</small><div class="value" id="input">—</div></div>
<div class="card"><small>Выходящие + thinking</small><div class="value" id="output">—</div></div>
<div class="card"><small>Thinking tokens</small><div class="value" id="thinking">—</div></div>
<div class="card"><small>Расчётная стоимость USD</small><div class="value" id="cost">—</div></div>
<div class="card"><small>Стоимость неизвестна</small><div class="value" id="unknown">—</div></div>
</div>
<section><h2>Расходы по моделям</h2><div id="models" class="muted">Загрузка…</div></section>
<section><h2>Расходы по операциям</h2><div id="operations" class="muted">Загрузка…</div></section>
<section><h2>Последние пользовательские запросы</h2><div id="requests" class="muted">Загрузка…</div></section>
<script>
const fmt = n => Number(n||0).toLocaleString("ru-RU");
const money = n => "$"+Number(n||0).toFixed(8);
const esc = s => String(s??"—").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#39;");
function table(rows, columns, costBar=false){
 if(!rows || !rows.length) return '<p class="muted">Пока нет данных</p>';
 const head='<thead><tr>'+columns.map(c=>'<th class="'+(c.num?'num':'')+'">'+c.label+'</th>').join('')+'</tr></thead>';
 const max=Math.max(0,...rows.map(r=>Number(r.estimated_cost_usd||0)));
 const body='<tbody>'+rows.map(r=>'<tr>'+columns.map(c=>{
  let v=c.render?c.render(r):r[c.key];
  if(c.key==='estimated_cost_usd' && costBar){
   const w=max?Math.max(2,Number(r.estimated_cost_usd||0)/max*100):2;
   v=money(r.estimated_cost_usd)+'<div class="bar" style="width:'+w+'%"></div>';
  } else {
   v=esc(v);
  }
  return '<td class="'+(c.num?'num':'')+'">'+String(v??'—')+'</td>';
 }).join('')+'</tr>').join('')+'</tbody>';
 return '<table>'+head+body+'</table>';
}
async function load(){
 const p=document.getElementById('period').value;
 try{
  const [u,r]=await Promise.all([
   fetch('/api/usage?period='+encodeURIComponent(p),{cache:'no-store'}),
   fetch('/api/requests?period='+encodeURIComponent(p),{cache:'no-store'})
  ]);
  if(!u.ok || !r.ok) throw new Error('HTTP '+u.status+'/'+r.status);
  const data=await u.json(), req=await r.json(), t=data.totals||{};
  document.getElementById('attempts').textContent=fmt(t.attempts);
  document.getElementById('input').textContent=fmt(t.prompt_tokens);
  document.getElementById('output').textContent=fmt(Math.max(0,Number(t.total_tokens||0)-Number(t.prompt_tokens||0)));
  document.getElementById('thinking').textContent=fmt(t.thoughts_tokens);
  document.getElementById('cost').textContent=money(t.estimated_cost_usd);
  document.getElementById('unknown').textContent=fmt(t.unknown_costs);
  document.getElementById('models').innerHTML=table(data.models,[
   {key:'model',label:'Модель'},
   {key:'attempts',label:'Вызовы',num:true,render:x=>fmt(x.attempts)},
   {key:'prompt_tokens',label:'Input',num:true,render:x=>fmt(x.prompt_tokens)},
   {key:'total_tokens',label:'Всего токенов',num:true,render:x=>fmt(x.total_tokens)},
   {key:'estimated_cost_usd',label:'Стоимость',num:true}
  ],true);
  document.getElementById('operations').innerHTML=table(data.operations,[
   {key:'operation',label:'Операция'},
   {key:'attempts',label:'Вызовы',num:true,render:x=>fmt(x.attempts)},
   {key:'prompt_tokens',label:'Input',num:true,render:x=>fmt(x.prompt_tokens)},
   {key:'estimated_cost_usd',label:'Стоимость',num:true}
  ],true);
  document.getElementById('requests').innerHTML=table(req.requests,[
   {key:'created_at',label:'Время'},
   {key:'models',label:'Модели'},
   {key:'attempts',label:'Вызовы',num:true,render:x=>fmt(x.attempts)},
   {key:'total_tokens',label:'Токены',num:true,render:x=>fmt(x.total_tokens)},
   {key:'estimated_cost_usd',label:'Стоимость',num:true,render:x=>money(x.estimated_cost_usd)},
   {key:'unknown_costs',label:'Неизвестно',num:true,render:x=>fmt(x.unknown_costs)}
  ]);
 }catch(e){
  for(const id of ['models','operations','requests']) document.getElementById(id).innerHTML='<div class="error">Не удалось загрузить статистику. Обновите страницу или проверьте подключение.</div>';
 }
}
document.getElementById('refresh').addEventListener('click',load);
document.getElementById('period').addEventListener('change',load);
load();
</script>
</body></html>"""


class GeminiDashboardServer:
    """Small read-only HTTP dashboard, protected by HTTP Basic authentication."""

    def __init__(
        self,
        database: Any,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> None:
        if not password:
            raise ValueError("Dashboard password must not be empty")
        self.database = database
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _authorized(header: str | None, username: str, password: str) -> bool:
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(supplied_user, username) and hmac.compare_digest(
            supplied_password, password
        )

    def start(self) -> "GeminiDashboardServer":
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ShortsChillAnalytics"
            sys_version = ""

            def log_message(self, fmt: str, *args: object) -> None:
                # Never log Authorization headers or query data.
                logger.debug("Gemini dashboard request from %s", self.client_address[0])

            def _headers(self, content_type: str) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'; "
                    "base-uri 'none'; frame-ancestors 'none'",
                )

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self._headers(content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _unauthorized(self) -> None:
                body = b"Authentication required"
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    'Basic realm="Shorts Chill Gemini Analytics", charset="UTF-8"',
                )
                self._headers("text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if not dashboard._authorized(
                    self.headers.get("Authorization"),
                    dashboard.username,
                    dashboard.password,
                ):
                    self._unauthorized()
                    return

                parsed = urlsplit(self.path)
                params = parse_qs(parsed.query)
                period = (params.get("period") or ["30"])[0]
                if period not in _PERIODS:
                    period = "30"
                since = analytics.period_start(period)

                try:
                    if parsed.path == "/":
                        self._send(200, _HTML.encode("utf-8"), "text/html; charset=utf-8")
                    elif parsed.path == "/api/usage":
                        result = asyncio.run(dashboard.database.get_gemini_usage_stats(since))
                        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
                        self._send(200, payload, "application/json; charset=utf-8")
                    elif parsed.path == "/api/requests":
                        rows = asyncio.run(
                            dashboard.database.list_gemini_usage_requests(since, limit=50)
                        )
                        payload = json.dumps(
                            {"requests": rows}, ensure_ascii=False
                        ).encode("utf-8")
                        self._send(200, payload, "application/json; charset=utf-8")
                    else:
                        self._send(404, b"Not found", "text/plain; charset=utf-8")
                except Exception:
                    logger.exception("Gemini analytics dashboard request failed")
                    self._send(500, b"Internal server error", "text/plain; charset=utf-8")

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = Server((self.host, self.port), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="gemini-analytics-dashboard",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
