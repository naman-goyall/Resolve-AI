import asyncio
import os
from collections import defaultdict, deque
from typing import AsyncIterator, Deque, Dict, List, Optional, Tuple

import orjson
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis


STREAM_NAME = os.getenv("STREAM_NAME", "logs")
REDIS_ADDR = os.getenv("REDIS_ADDR", "redis://localhost:6379/0")
CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "distributors")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", "dist-1")
PENDING_IDLE_MS = int(os.getenv("PENDING_IDLE_MS", "60000"))
BATCH_COUNT = int(os.getenv("BATCH_COUNT", "128"))
BLOCK_MS = int(os.getenv("BLOCK_MS", "2000"))
SUBSCRIBER_QUEUE_LEN = int(os.getenv("SUBSCRIBER_QUEUE_LEN", "1000"))

ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")


app = FastAPI(title="Logs Distributor", version="1.0")
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
                # fanout to all active subscribers without blocking
                for sid, q in list(subscriber_queues.items()):
                    try:
                        q.append(raw)
                    except Exception:
                        # queue is bounded; drop oldest by deque behavior
                        pass
                ids_to_ack.append(msg_id)

            if ids_to_ack:
                await redis_client.xack(STREAM_NAME, CONSUMER_GROUP, *ids_to_ack)
            last_exception = None
        except Exception as exc:  # noqa: BLE001
            # backoff on error
            last_exception = exc
            await asyncio.sleep(0.5)


@app.on_event("startup")
async def on_startup() -> None:
    global redis_client, bg_task
    redis_client = Redis.from_url(REDIS_ADDR, decode_responses=False)
    await redis_client.ping()
    bg_task = asyncio.create_task(consume_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global redis_client, bg_task
    if bg_task:
        bg_task.cancel()
        try:
            await bg_task
        except Exception:  # noqa: BLE001
            pass
    if redis_client:
        await redis_client.aclose()


@app.get("/healthz")
async def healthz() -> dict:
    assert redis_client is not None
    try:
        pong = await redis_client.ping()
        return {"status": "ok", "redis": pong, "subscribers": len(subscriber_queues)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "error": str(exc)}


async def sse_event_stream(sid: str) -> AsyncIterator[bytes]:
    try:
        while True:
            q = subscriber_queues[sid]
            if q:
                # drain queue quickly without blocking
                while q:
                    raw = q.popleft()
                    try:
                        data = raw.decode()
                    except Exception:
                        data = orjson.dumps({"error": "decode", "raw_len": len(raw)}).decode()
                    yield f"data: {data}\n\n".encode()
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(0.1)
    finally:
        # cleanup on disconnect
        try:
            del subscriber_queues[sid]
        except KeyError:
            pass


@app.get("/subscribe")
async def subscribe() -> StreamingResponse:
    # create a unique subscriber id
    sid = f"s-{os.urandom(6).hex()}"
    # Initialize queue implicitly via defaultdict
    _ = subscriber_queues[sid]
    headers = {"Cache-Control": "no-cache", "Content-Type": "text/event-stream", "Connection": "keep-alive"}
    return StreamingResponse(sse_event_stream(sid), headers=headers, media_type="text/event-stream")


