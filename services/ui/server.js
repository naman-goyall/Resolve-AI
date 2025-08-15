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
    <script type="text/javascript">
      (function(){
        var distributor = 'http://localhost:8082';
        // Use absolute URLs to avoid mixed-origin surprises
        var collector = 'http://localhost:8081';

        var sendBtn = document.getElementById('send');
        var connectBtn = document.getElementById('connect');
        var sendStatus = document.getElementById('send-status');

        sendBtn.onclick = function(){
          var payload = {
            source: document.getElementById('source').value,
            level: document.getElementById('level').value,
            message: document.getElementById('message').value,
            ts_unix_ms: Date.now()
          };
          fetch(collector + '/ingest', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(payload),
            mode: 'cors'
          }).then(function(r){
            sendStatus.textContent = r.ok ? ' ok' : ' error';
            sendStatus.className = r.ok ? 'ok' : 'bad';
          }).catch(function(err){
            console.error('ingest error', err);
            sendStatus.textContent = ' error';
            sendStatus.className = 'bad';
          });
        };

        connectBtn.onclick = function(){
          var statusEl = document.getElementById('sse-status');
          statusEl.textContent = ' connecting...';
          try {
            var ev = new EventSource(distributor + '/subscribe');
            ev.onopen = function(){ statusEl.textContent = ' connected'; statusEl.className = 'ok'; };
            ev.onerror = function(){ statusEl.textContent = ' disconnected'; statusEl.className = 'bad'; };
            ev.onmessage = function(m){
              try {
                var data = JSON.parse(m.data);
                var el = document.createElement('div');
                el.textContent = '[' + (data.level||'') + '] ' + (data.source||'') + ': ' + (data.message||'');
                var logs = document.getElementById('logs');
                logs.appendChild(el);
                logs.scrollTop = logs.scrollHeight;
              } catch(e) { console.error('parse error', e); }
            };
          } catch(e) {
            console.error('EventSource error', e);
            statusEl.textContent = ' error';
            statusEl.className = 'bad';
          }
        };
      })();
    </script>
  </body>
 </html>`);
});

app.listen(PORT, () => {
  console.log(`UI listening on :${PORT} -> distributor ${DISTRIBUTOR_HTTP}`);
});


