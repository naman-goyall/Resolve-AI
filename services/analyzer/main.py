import asyncio
import logging
import os
from typing import Optional, List

import orjson
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from redis.asyncio import Redis


REDIS_ADDR = os.getenv("REDIS_ADDR", "redis://localhost:6379/0")
ANALYZER_NAME = os.getenv("ANALYZER_NAME", "an1")
STREAM_NAME = f"analyzer:{ANALYZER_NAME}"
GROUP = os.getenv("ANALYZER_GROUP", "analyzers")
CONSUMER = os.getenv("CONSUMER_NAME", f"{ANALYZER_NAME}-c1")
BATCH_COUNT = int(os.getenv("BATCH_COUNT", "128"))
BLOCK_MS = int(os.getenv("BLOCK_MS", "2000"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(f"analyzer-{ANALYZER_NAME}")

app = FastAPI(title=f"Analyzer {ANALYZER_NAME}", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_client: Optional[Redis] = None
worker_task: Optional[asyncio.Task] = None

# Metrics
PROC_TOTAL = Counter("analyzer_processed_total", "Total analyzer messages processed", ["analyzer"]) \
    .labels(analyzer=ANALYZER_NAME)
PROC_ERRORS = Counter("analyzer_errors_total", "Analyzer errors", ["analyzer"]).labels(analyzer=ANALYZER_NAME)
PROC_LATENCY = Histogram("analyzer_latency_seconds", "Analyzer processing latency seconds", ["analyzer"]) \
    .labels(analyzer=ANALYZER_NAME)
QUEUE_GAUGE = Gauge("analyzer_pending_gauge", "Pending messages in analyzer stream", ["analyzer"]).labels(analyzer=ANALYZER_NAME)


async def ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(name=STREAM_NAME, groupname=GROUP, id="$", mkstream=True)
    except Exception as exc:  # noqa: BLE001
        if "BUSYGROUP" in str(exc):
            return
        raise


async def analyze(payload: bytes) -> None:
    # Simulate analyzer logic; in real life this would do CPU/IO work
    await asyncio.sleep(0.001)


async def run_worker() -> None:
    assert redis_client is not None
    await ensure_group(redis_client)
    while True:
        try:
            resp = await redis_client.xreadgroup(
                groupname=GROUP,
                consumername=CONSUMER,
                streams={STREAM_NAME: ">"},
                count=BATCH_COUNT,
                block=BLOCK_MS,
            )
            if not resp:
                await asyncio.sleep(0)
                continue
            _, messages = resp[0]
            ids: List[str] = []
            for msg_id, fields in messages:
                raw = fields.get(b"json")
                if raw is None:
                    ids.append(msg_id)
                    continue
                with PROC_LATENCY.time():  # type: ignore[attr-defined]
                    try:
                        await analyze(raw)
                        PROC_TOTAL.inc()
                    except Exception:  # noqa: BLE001
                        PROC_ERRORS.inc()
                ids.append(msg_id)
            if ids:
                await redis_client.xack(STREAM_NAME, GROUP, *ids)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Analyzer %s: worker error: %s", ANALYZER_NAME, exc)
            await asyncio.sleep(0.5)


@app.on_event("startup")
async def on_startup() -> None:
    global redis_client, worker_task
    redis_client = Redis.from_url(REDIS_ADDR, decode_responses=False)
    # ping with retry
    backoff = 0.5
    for _ in range(10):
        try:
            await redis_client.ping()
            break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(backoff)
            backoff = min(2.0, backoff * 1.5)
    else:
        raise RuntimeError(f"Analyzer {ANALYZER_NAME}: cannot connect to Redis {REDIS_ADDR}")
    worker_task = asyncio.create_task(run_worker())
    logger.info("Analyzer %s: connected to Redis and started worker on %s", ANALYZER_NAME, STREAM_NAME)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global worker_task, redis_client
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except Exception:  # noqa: BLE001
            pass
    if redis_client:
        await redis_client.aclose()


@app.get("/healthz")
async def healthz() -> dict:
    try:
        assert redis_client is not None
        pong = await redis_client.ping()
        return {"status": "ok", "redis": pong, "stream": STREAM_NAME, "consumer": CONSUMER}
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "error": str(exc)}


@app.get("/metrics")
async def metrics() -> Response:
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


@app.get("/stats")
async def stats() -> JSONResponse:
    return JSONResponse({"analyzer": ANALYZER_NAME, "stream": STREAM_NAME, "consumer": CONSUMER})


