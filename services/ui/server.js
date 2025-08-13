import express from 'express';

const app = express();
const PORT = process.env.PORT || 8080;
const DISTRIBUTOR_HTTP = process.env.DISTRIBUTOR_HTTP || 'http://localhost:8082';

app.get('/', (_req, res) => {
  res.set('Content-Type', 'text/html');
  res.send(`<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Resolve Logs Demo</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px; background: #0b1220; color: #eef2ff; }
      .row { display:flex; gap:16px; }
      textarea { width: 100%; min-height: 100px; }
      .card { background:#111827; border:1px solid #1f2937; border-radius:10px; padding:16px; }
      .logs { height: 420px; overflow:auto; background:#0f172a; border:1px solid #1f2937; border-radius:8px; padding:12px; }
      .ok { color: #10b981 }
      .bad { color: #ef4444 }
      .btn { background:#2563eb; border:none; color:white; padding:8px 12px; border-radius:8px; cursor:pointer; }
      .btn:disabled { opacity: 0.6; cursor: not-allowed; }
      input, select, textarea { background:#0f172a; color:#e5e7eb; border:1px solid #1f2937; border-radius:6px; padding:8px; }
      label { display:block; margin-bottom:6px; color:#9ca3af }
    </style>
  </head>
  <body>
    <h2>Resolve Logs - Demo</h2>
    <div class="row">
      <div class="card" style="flex:1">
        <h3>Ingest</h3>
        <label>Source</label>
        <input id="source" value="demo-ui"/>
        <label>Level</label>
        <select id="level">
          <option>DEBUG</option>
          <option selected>INFO</option>
          <option>WARN</option>
          <option>ERROR</option>
        </select>
        <label>Message</label>
        <textarea id="message">Hello from UI</textarea>
        <br/>
        <button id="send" class="btn">Send</button>
        <span id="send-status"></span>
      </div>
      <div class="card" style="flex:1">
        <h3>Live Stream</h3>
        <button id="connect" class="btn">Connect</button>
        <span id="sse-status"></span>
        <div id="logs" class="logs"></div>
      </div>
    </div>
    <script>
      const distributor = '${DISTRIBUTOR_HTTP}'.replace(/\/$/, '');
      const collector = location.origin.replace(':8080', ':8081');

      document.getElementById('send').onclick = async () => {
        const payload = {
          source: document.getElementById('source').value,
          level: document.getElementById('level').value,
          message: document.getElementById('message').value,
          ts_unix_ms: Date.now(),
        };
        const el = document.getElementById('send-status');
        try {
          const r = await fetch(collector + '/ingest', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
          const j = await r.json();
          el.textContent = r.ok ? ' ok' : ' error';
          el.className = r.ok ? 'ok' : 'bad';
        } catch (e) {
          el.textContent = ' error';
          el.className = 'bad';
        }
      };

      document.getElementById('connect').onclick = () => {
        const status = document.getElementById('sse-status');
        status.textContent = ' connecting...';
        const ev = new EventSource(distributor + '/subscribe');
        ev.onopen = () => { status.textContent = ' connected'; status.className = 'ok'; };
        ev.onerror = () => { status.textContent = ' disconnected'; status.className = 'bad'; };
        ev.onmessage = (m) => {
          try {
            const data = JSON.parse(m.data);
            const el = document.createElement('div');
            el.textContent = '[' + (data.level||'') + '] ' + (data.source||'') + ': ' + (data.message||'');
            const logs = document.getElementById('logs');
            logs.appendChild(el);
            logs.scrollTop = logs.scrollHeight;
          } catch {}
        };
      };
    </script>
  </body>
 </html>`);
});

app.listen(PORT, () => {
  console.log(`UI listening on :${PORT} -> distributor ${DISTRIBUTOR_HTTP}`);
});


