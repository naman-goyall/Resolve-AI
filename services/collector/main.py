import asyncio
import os
import logging
from typing import Optional

import orjson
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from redis.asyncio import Redis


STREAM_NAME = os.getenv("STREAM_NAME", "logs")
REDIS_ADDR = os.getenv("REDIS_ADDR", "redis://localhost:6379/0")
MAX_MESSAGE_BYTES = int(os.getenv("MAX_MESSAGE_BYTES", "65536"))
TRIM_MAXLEN = int(os.getenv("TRIM_MAXLEN", "50000"))


class LogEvent(BaseModel):
    source: str = Field(..., description="Source identifier of the log/event")
    level: str = Field(..., description="Log level", pattern=r"^(DEBUG|INFO|WARN|ERROR)$")
    message: str = Field(..., description="Message content")
    ts_unix_ms: Optional[int] = Field(None, description="Client-supplied timestamp in ms")
    metadata: Optional[dict] = Field(default=None, description="Additional structured fields")

    def to_redis_fields(self) -> dict:
        payload = self.model_dump(mode="python")
        data = orjson.dumps(payload)
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("payload too large")
        return {"json": data}


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("collector")

app = FastAPI(title="Logs Collector", version="1.1")

# CORS: default to wildcard without credentials for browser compatibility
ALLOWED_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


redis_client: Optional[Redis] = None


@app.on_event("startup")
async def on_startup() -> None:
    global redis_client
    redis_client = Redis.from_url(REDIS_ADDR, decode_responses=False)
    # Retry ping for up to ~15s to tolerate container start order
    backoff = 0.5
    for _ in range(10):
        try:
            await redis_client.ping()
            break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 2.0)
    else:
        logger.error("Collector: unable to connect to Redis at %s", REDIS_ADDR)
        raise RuntimeError(f"Cannot connect to Redis at {REDIS_ADDR}")
    logger.info("Collector: connected to Redis at %s, stream=%s", REDIS_ADDR, STREAM_NAME)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global redis_client
    if redis_client:
        await redis_client.aclose()


@app.post("/ingest")
async def ingest(event: LogEvent) -> dict:
    assert redis_client is not None
    try:
        fields = event.to_redis_fields()
    except ValueError as e:
        logger.warning("Collector: payload too large from source=%s", event.source)
        raise HTTPException(status_code=413, detail=str(e)) from e

    # XADD with MAXLEN to cap stream length for demo stability
    try:
        msg_id = await redis_client.xadd(name=STREAM_NAME, fields=fields, maxlen=TRIM_MAXLEN, approximate=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Collector: failed to XADD: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to write to stream: {exc}") from exc
    msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
    logger.info("Collector: ingested id=%s level=%s source=%s", msg_id_str, event.level, event.source)
    return {"status": "ok", "id": msg_id_str}


@app.post("/ingest/bulk")
async def ingest_bulk(events: list[LogEvent]) -> dict:
    assert redis_client is not None
    if not events:
        return {"status": "ok", "count": 0}
    pairs = []
    try:
        for ev in events:
            pairs.append((STREAM_NAME, ev.to_redis_fields()))
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e)) from e
    # Use pipeline for efficiency
    pipe = redis_client.pipeline()
    for name, fields in pairs:
        pipe.xadd(name=name, fields=fields, maxlen=TRIM_MAXLEN, approximate=True)
    try:
        ids = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Collector: bulk XADD failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"failed to write to stream: {exc}") from exc
    logger.info("Collector: bulk ingested %d events", len(ids))
    return {"status": "ok", "count": len(ids)}


@app.get("/healthz")
async def healthz() -> dict:
    assert redis_client is not None
    try:
        pong = await redis_client.ping()
        return {"status": "ok", "redis": pong}
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "error": str(exc)}


