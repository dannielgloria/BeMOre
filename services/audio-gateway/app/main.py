import os
import time
import asyncio
from typing import Any, Dict

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

import uvicorn


def setup_tracing() -> None:
    service_name = os.getenv("OTEL_SERVICE_NAME", "bmo-audio-gateway")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


setup_tracing()
tracer = trace.get_tracer("bmo.audio_gateway")

app = FastAPI(title="BMO Audio Gateway", version="0.1.0")
FastAPIInstrumentor.instrument_app(app)

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8080")

# Backpressure knobs (data-plane sanity)
FRAME_MIN_INTERVAL_MS = int(os.getenv("FRAME_MIN_INTERVAL_MS", "10"))  # allow <=100fps
MAX_WARNINGS = int(os.getenv("MAX_WARNINGS", "5"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "audio-gateway"}


@app.post("/internal/register/{session_id}")
async def register(session_id: str):
    """Manual trigger, mostly for debugging."""
    await _register_session_with_orchestrator(session_id)
    return {"registered": True, "session_id": session_id}


async def _register_session_with_orchestrator(session_id: str) -> None:
    with tracer.start_as_current_span("audio.session.register") as span:
        span.set_attribute("bmo.session_id", session_id)
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(f"{ORCHESTRATOR_URL}/audio/session/register", json={"session_id": session_id})
            r.raise_for_status()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    conn_id = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
    warnings = 0

    session_id = None
    last_frame_ts_ms = None
    last_seq = None

    with tracer.start_as_current_span("audio.ws.connection") as span:
        span.set_attribute("bmo.conn_id", conn_id)

        try:
            while True:
                msg: Dict[str, Any] = await websocket.receive_json()
                msg_type = msg.get("type")

                # control: start_stream
                if msg_type == "start_stream":
                    session_id = msg.get("session_id")
                    if not session_id:
                        await websocket.send_json({"type": "error", "reason": "missing session_id"})
                        continue

                    span.set_attribute("bmo.session_id", session_id)
                    await _register_session_with_orchestrator(session_id)

                    await websocket.send_json({"type": "ack", "session_id": session_id})
                    continue

                # frames
                if msg_type == "audio_frame":
                    if not session_id:
                        await websocket.send_json({"type": "error", "reason": "stream not started"})
                        continue

                    seq = int(msg.get("seq", -1))
                    ts_ms = int(msg.get("timestamp_ms", -1))

                    with tracer.start_as_current_span("audio.frame.recv") as fspan:
                        fspan.set_attribute("bmo.session_id", session_id)
                        fspan.set_attribute("bmo.seq", seq)
                        fspan.set_attribute("bmo.timestamp_ms", ts_ms)

                        # ordering sanity
                        if last_seq is not None and seq <= last_seq:
                            warnings += 1
                            fspan.set_attribute("bmo.warning", "non_monotonic_seq")
                            await websocket.send_json({"type": "warning", "kind": "non_monotonic_seq", "last_seq": last_seq, "seq": seq})

                        # backpressure sanity: frames too fast
                        now = int(time.time() * 1000)
                        if last_frame_ts_ms is not None:
                            delta = now - last_frame_ts_ms
                            if delta < FRAME_MIN_INTERVAL_MS:
                                warnings += 1
                                fspan.set_attribute("bmo.warning", "backpressure")
                                await websocket.send_json({"type": "warning", "kind": "backpressure", "delta_ms": delta})

                        last_frame_ts_ms = now
                        last_seq = seq

                        # echo back minimal receipt to validate duplex
                        await websocket.send_json({"type": "frame_ok", "seq": seq})

                    if warnings >= MAX_WARNINGS:
                        await websocket.send_json({"type": "error", "reason": "too_many_warnings"})
                        await websocket.close(code=1011)
                        break

                    continue

                # control: end_stream
                if msg_type == "end_stream":
                    await websocket.send_json({"type": "end_ack", "session_id": session_id})
                    await websocket.close(code=1000)
                    break

                # unknown
                await websocket.send_json({"type": "error", "reason": f"unknown type: {msg_type}"})

        except WebSocketDisconnect:
            # client disconnected
            pass
        except Exception as e:
            # ensure we don't spin silently
            try:
                await websocket.send_json({"type": "error", "reason": str(e)})
            except Exception:
                pass
            try:
                await websocket.close(code=1011)
            except Exception:
                pass


if __name__ == "__main__":
    port = int(os.getenv("AUDIO_PORT", "8081"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")
