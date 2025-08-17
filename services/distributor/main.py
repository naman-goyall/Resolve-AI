import asyncio
import os
import logging
from collections import defaultdict, deque
from typing import AsyncIterator, Deque, Dict, List, Optional, Tuple

import orjson
import contextlib
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from redis.asyncio import Redis
import json
import zlib


STREAM_NAME = os.getenv("STREAM_NAME", "logs")
REDIS_ADDR = os.getenv("REDIS_ADDR", "redis://localhost:6379/0")
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "distributors")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", "dist-1")
PENDING_IDLE_MS = int(os.getenv("PENDING_IDLE_MS", "60000"))
BATCH_COUNT = int(os.getenv("BATCH_COUNT", "128"))
BLOCK_MS = int(os.getenv("BLOCK_MS", "2000"))
SUBSCRIBER_QUEUE_LEN = int(os.getenv("SUBSCRIBER_QUEUE_LEN", "1000"))
BROADCAST_CHANNEL = os.getenv("BROADCAST_CHANNEL", "logs_broadcast")
ANALYZERS_JSON = os.getenv("ANALYZERS", "[{'name':'an1','weight':2},{'name':'an2','weight':1}]")

ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("distributor")

app = FastAPI(title="Logs Distributor", version="1.3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


redis_client: Optional[Redis] = None
subscriber_queues: Dict[str, Deque[bytes]] = defaultdict(lambda: deque(maxlen=SUBSCRIBER_QUEUE_LEN))
subscriber_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
bg_task: Optional[asyncio.Task] = None
pubsub_task: Optional[asyncio.Task] = None
pubsub_obj = None

# Metrics
PUB_TOTAL = Counter("distributor_published_total", "Total events published to subscribers")
PUB_ERRORS = Counter("distributor_errors_total", "Distributor errors", ["where"])
SSE_CONNECTIONS = Gauge("distributor_sse_connections", "Active SSE connections")

# Weighted routing config (deterministic hashing)
try:
    # Support both JSON and Python-like single quotes for convenience
    ANALYZERS = json.loads(ANALYZERS_JSON.replace("'", '"'))
except Exception:
    ANALYZERS = [{"name": "an1", "weight": 2}, {"name": "an2", "weight": 1}]
_weights: List[Tuple[int, str]] = []  # (upper_bound, name)
_total_weight = 0
for a in ANALYZERS:
    w = int(a.get("weight", 1))
    if w <= 0:
        continue
    _total_weight += w
    _weights.append((_total_weight, a["name"]))


def choose_analyzer(raw_json: bytes) -> str:
    if not _weights:
        return "an1"
    h = zlib.crc32(raw_json) % _total_weight
    for upper, name in _weights:
        if h < upper:
            return name
    return _weights[-1][1]


async def ensure_group(redis: Redis) -> None:
    try:
        # Create stream and group if not exist
        await redis.xgroup_create(name=STREAM_NAME, groupname=CONSUMER_GROUP, id="$", mkstream=True)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "BUSYGROUP" in msg:
            return
        # On other errors, bubble up
        raise


async def claim_stale(redis: Redis) -> None:
    try:
        pending = await redis.xpending_range(
            STREAM_NAME, CONSUMER_GROUP, min="-", max="+", count=BATCH_COUNT, consumername=CONSUMER_NAME
        )
        for item in pending:
            # item may be tuple-like or object-like depending on redis-py
            try:
                message_id = getattr(item, "message_id", None) or item[0]
                idle_ms = getattr(item, "idle", None)
                if idle_ms is None:
                    idle_ms = item[2]
            except Exception:
                continue
            if idle_ms >= PENDING_IDLE_MS:
                await redis.xclaim(
                    STREAM_NAME,
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    min_idle_time=PENDING_IDLE_MS,
                    message_ids=[message_id],
                )
    except Exception:
        # best-effort; do not crash
        return


async def consume_loop() -> None:
    assert redis_client is not None
    await ensure_group(redis_client)
    last_exception: Optional[Exception] = None
    while True:
        try:
            await claim_stale(redis_client)
            resp = await redis_client.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={STREAM_NAME: ">"},
                count=BATCH_COUNT,
                block=BLOCK_MS,
            )
            if not resp:
                await asyncio.sleep(0)
                continue

            # xreadgroup return shape: List[Tuple[stream, List[Tuple[id, Dict[field, value]]]]]
            (_, messages) = resp[0]
            ids_to_ack: List[str] = []
            for msg_id, fields in messages:
                raw = fields.get(b"json")
                if raw is None:
                    ids_to_ack.append(msg_id)
                    continue
                # publish to Redis channel for cross-instance fanout
                try:
                    await redis_client.publish(BROADCAST_CHANNEL, raw)
                    PUB_TOTAL.inc()
                except Exception as exc:
                    logger.exception("Distributor: publish failed: %s", exc)
                    PUB_ERRORS.labels(where="publish").inc()
                # Route deterministically to analyzer stream by weight
                try:
                    target = choose_analyzer(raw)
                    stream_name = f"analyzer:{target}"
                    await redis_client.xadd(name=stream_name, fields={b"json": raw})
                except Exception as exc:
                    logger.exception("Distributor: route to analyzer failed: %s", exc)
                    PUB_ERRORS.labels(where="route").inc()
                ids_to_ack.append(msg_id)

            if ids_to_ack:
                await redis_client.xack(STREAM_NAME, CONSUMER_GROUP, *ids_to_ack)
                logger.debug("Distributor: acked %d messages", len(ids_to_ack))
            last_exception = None
        except Exception as exc:  # noqa: BLE001
            # backoff on error
            last_exception = exc
            logger.exception("Distributor: consume loop error: %s", exc)
            await asyncio.sleep(0.5)


async def pubsub_loop() -> None:
    assert redis_client is not None
    global pubsub_obj
    pubsub_obj = redis_client.pubsub()
    await pubsub_obj.subscribe(BROADCAST_CHANNEL)
    logger.info("Distributor: subscribed to channel=%s", BROADCAST_CHANNEL)
    try:
        async for message in pubsub_obj.listen():
            if message is None:
                await asyncio.sleep(0)
                continue
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if not isinstance(data, (bytes, bytearray)):
                try:
                    data = str(data).encode()
                except Exception:
                    continue
            for sid, q in list(subscriber_queues.items()):
                lock = subscriber_locks[sid]
                async with lock:
                    try:
                        q.append(data)
                    except Exception:
                        pass
    finally:
        with contextlib.suppress(Exception):
            await pubsub_obj.unsubscribe(BROADCAST_CHANNEL)
            await pubsub_obj.close()


@app.on_event("startup")
async def on_startup() -> None:
    global redis_client, bg_task
    redis_client = Redis.from_url(REDIS_ADDR, decode_responses=False)
    # Retry ping
    backoff = 0.5
    for _ in range(10):
        try:
            await redis_client.ping()
            break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 2.0)
    else:
        raise RuntimeError(f"Cannot connect to Redis at {REDIS_ADDR}")
    logger.info("Distributor: connected to Redis at %s, group=%s consumer=%s stream=%s", REDIS_ADDR, CONSUMER_GROUP, CONSUMER_NAME, STREAM_NAME)
    bg_task = asyncio.create_task(consume_loop())
    global pubsub_task
    pubsub_task = asyncio.create_task(pubsub_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global redis_client, bg_task, pubsub_task, pubsub_obj
    if bg_task:
        bg_task.cancel()
        try:
            await bg_task
        except Exception:  # noqa: BLE001
            pass
    if pubsub_task:
        pubsub_task.cancel()
        try:
            await pubsub_task
        except Exception:
            pass
    if redis_client:
        await redis_client.aclose()


@app.get("/healthz")
async def healthz() -> dict:
    assert redis_client is not None
    try:
        pong = await redis_client.ping()
        return {"status": "ok", "redis": pong, "subscribers": len(subscriber_queues), "group": CONSUMER_GROUP, "consumer": CONSUMER_NAME}
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "error": str(exc)}


async def sse_event_stream(sid: str) -> AsyncIterator[bytes]:
    try:
        SSE_CONNECTIONS.inc()
        while True:
            q = subscriber_queues[sid]
            if q:
                # drain queue quickly without blocking
                while q:
                    lock = subscriber_locks[sid]
                    async with lock:
                        raw = q.popleft()
                    try:
                        data = raw.decode()
                    except Exception:
                        data = orjson.dumps({"error": "decode", "raw_len": len(raw)}).decode()
                    yield f"data: {data}\n\n".encode()
                await asyncio.sleep(0)
            else:
                # heartbeat to keep connection alive through proxies
                yield b": keep-alive\n\n"
                await asyncio.sleep(10.0)
    finally:
        # cleanup on disconnect
        try:
            del subscriber_queues[sid]
        except KeyError:
            pass
        try:
            del subscriber_locks[sid]
        except KeyError:
            pass
        SSE_CONNECTIONS.dec()


@app.get("/subscribe")
async def subscribe() -> StreamingResponse:
    # create a unique subscriber id
    sid = f"s-{os.urandom(6).hex()}"
    # Initialize queue implicitly via defaultdict
    _ = subscriber_queues[sid]
    headers = {"Cache-Control": "no-cache", "Content-Type": "text/event-stream", "Connection": "keep-alive"}
    logger.info("Distributor: subscriber connected sid=%s", sid)
    return StreamingResponse(sse_event_stream(sid), headers=headers, media_type="text/event-stream")


@app.get("/stats")
async def stats() -> JSONResponse:
    """Lightweight metrics endpoint for demo validation."""
    return JSONResponse({
        "subscribers": len(subscriber_queues),
        "group": CONSUMER_GROUP,
        "consumer": CONSUMER_NAME,
        "stream": STREAM_NAME,
    })


@app.get("/readyz")
async def readyz() -> dict:
    return await healthz()


@app.get("/livez")
async def livez() -> dict:
    return {"status": "alive"}


@app.get("/metrics")
async def metrics() -> Response:
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


