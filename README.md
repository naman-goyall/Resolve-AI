## Resolve AI - Logs Collector/Distributor (Python)

This repo contains a complete, Dockerized logs pipeline using Redis Streams and FastAPI:

- `collector` (FastAPI): HTTP ingest endpoint `/ingest` that writes events to a Redis Stream with bounded length.
- `distributor` (FastAPI): Consumes the stream via Redis consumer groups and broadcasts to connected clients using SSE at `/subscribe`.
- `ui` (Node/Express): Minimal UI to send test events and view the live stream.

### Requirements

- Docker and Docker Compose

### Quick start

```bash
docker compose build
docker compose up -d

# Open UI
open http://localhost:8080

# Health
curl -s localhost:8081/healthz | jq
curl -s localhost:8082/healthz | jq

### Smoke test

Run a full end-to-end check from the host shell:

```bash
# subscribe in background and send a test event
curl -sN http://localhost:8082/subscribe > /tmp/resolve_sse.txt &
SSE_PID=$!
sleep 1
curl -s -X POST http://localhost:8081/ingest \
  -H 'content-type: application/json' \
  -d '{"source":"readme","level":"INFO","message":"hello from smoke test"}'
sleep 1
echo "SSE bytes captured:" $(wc -c < /tmp/resolve_sse.txt)
tail -n +1 /tmp/resolve_sse.txt | sed -n '1,10p'
kill $SSE_PID || true
```

### Scaling distributors

- Compose already starts a second consumer `distributor2` in the same consumer group.
- To scale further:

```bash
docker compose up -d --scale distributor=3
```

Each replica must have a distinct `CONSUMER_NAME` if not using Docker’s replica suffixing; for demo scale, defaults are fine.

### Configuration

All tunables are environment variables (see `docker-compose.yml`). Key ones:
- `TRIM_MAXLEN` caps stream memory; tune upward for longer history.
- `BATCH_COUNT` and `BLOCK_MS` control consumer latency vs throughput.
- `PENDING_IDLE_MS` controls stale-claim timing for resiliency.

### Operational notes

- Containers include healthchecks and `restart: unless-stopped`.
- Health-gated dependencies: Redis must be healthy; services retry Redis at startup.
- SSE heartbeat (every 10s) keeps connections alive behind proxies.
```

In the UI:
- Click Connect to subscribe to the live stream
- Send events via the left panel; they should appear in the right panel

### Services

- `services/collector`: FastAPI app exposing `/ingest` (POST). Body schema:

```json
{
  "source": "service-A",
  "level": "INFO",
  "message": "started",
  "ts_unix_ms": 1710000000000,
  "metadata": {"region": "us-east-1"}
}
```

Env vars:
- `REDIS_ADDR` (default `redis://redis:6379/0`)
- `STREAM_NAME` (default `logs`)
- `MAX_MESSAGE_BYTES` (default `65536`)
- `TRIM_MAXLEN` (stream cap, default `50000`)

- `services/distributor`: FastAPI app exposing `/subscribe` (SSE) and `/healthz`.

Env vars:
- `REDIS_ADDR` (default `redis://redis:6379/0`)
- `STREAM_NAME` (default `logs`)
- `CONSUMER_GROUP` (default `distributors`)
- `CONSUMER_NAME` (default `dist-1`)
- `PENDING_IDLE_MS` (claim stale pending threshold)
- `BATCH_COUNT` (read batch size)
- `BLOCK_MS` (xreadgroup block milliseconds)
- `SUBSCRIBER_QUEUE_LEN` (bounded per-connection queue)
- `CORS_ALLOW_ORIGINS` (default `*`)

### Concurrency and reliability

- Collector validates payload size and uses `XADD MAXLEN ~` to cap memory.
- Distributor runs a background consumer loop using Redis consumer groups to ensure at-least-once delivery and acknowledges after fanout.
- Stale messages are reclaimed via `XPENDING` + `XCLAIM` after `PENDING_IDLE_MS`.
- SSE fanout uses per-subscriber bounded `deque` to avoid unbounded memory growth; no blocking while holding locks.

### Failure handling

- Redis connectivity is checked at startup; health endpoints expose status.
- Consumer loop uses small backoff on errors and keeps running.
- UI handles SSE disconnects gracefully.

### Scaling

- Run multiple distributor replicas by scaling the `distributor` service and varying `CONSUMER_NAME`. Redis consumer groups will shard messages across consumers.
- Add independent topic partitioning by using multiple streams (`STREAM_NAME` values) if needed.

### Local development

To run a single service locally against Docker Redis:

```bash
uvicorn services/collector/main:app --reload --port 8081
uvicorn services/distributor/main:app --reload --port 8082
node services/ui/server.js
```


