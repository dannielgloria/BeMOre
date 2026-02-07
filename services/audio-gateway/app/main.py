import os
import time
import asyncio
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

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
    processor = BatchSpanProcessor(
        OTLPSpanExporter(endpoint=endpoint, insecure=True)
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


setup_tracing()
tracer = trace.get_tracer("bmo.audio_gateway")

app = FastAPI(title="BMO Audio Gateway", version="0.2.0")
FastAPIInstrumentor.instrument_app(app)

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8080")
VOICE_CONTROL_URL = os.getenv("VOICE_CONTROL_URL", "http://voice-control:8085")

FRAME_MIN_INTERVAL_MS = int(os.getenv("FRAME_MIN_INTERVAL_MS", "10"))
MAX_WARNINGS = int(os.getenv("MAX_WARNINGS", "5"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "audio-gateway"}


async def send_voice_event(session_id: str, event_type: str) -> Dict[str, Any]:
    payload = {
        "type": event_type,
        "session_id": session_id,
        "timestamp_ms": int(time.time() * 1000),
    }

    with tracer.start_as_current_span("voice.event.emit") as span:
        span.set_attribute("bmo.session_id", session_id)
        span.set_attribute("bmo.event_type", event_type)

        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(f"{VOICE_CONTROL_URL}/event", json=payload)
            r.raise_for_status()
            return r.json()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()

    session_id: Optional[str] = None
    last_frame_ts_ms: Optional[int] = None
    last_seq: Optional[int] = None
    warnings = 0

    try:
        while True:
            msg: Dict[str, Any] = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == "start_stream":
                session_id = msg.get("session_id")
                if not session_id:
                    await websocket.send_json({"type": "error", "reason": "missing session_id"})
                    continue

                await send_voice_event(session_id, "audio_stream_started")
                await websocket.send_json({"type": "ack", "session_id": session_id})
                continue

            if msg_type == "audio_frame":
                if not session_id:
                    await websocket.send_json({"type": "error", "reason": "stream not started"})
                    continue

                seq = int(msg.get("seq", -1))
                now = int(time.time() * 1000)

                # Simulated speech detection on first frame
                if last_seq is None:
                    await send_voice_event(session_id, "speech_detected")

                # Backpressure check
                if last_frame_ts_ms is not None:
                    delta = now - last_frame_ts_ms
                    if delta < FRAME_MIN_INTERVAL_MS:
                        warnings += 1
                        await websocket.send_json(
                            {"type": "warning", "kind": "backpressure", "delta_ms": delta}
                        )

                last_frame_ts_ms = now
                last_seq = seq

                await websocket.send_json({"type": "frame_ok", "seq": seq})

                if warnings >= MAX_WARNINGS:
                    await websocket.send_json({"type": "error", "reason": "too_many_warnings"})
                    await websocket.close(code=1011)
                    break

                continue

            if msg_type == "end_stream":
                if session_id:
                    await send_voice_event(session_id, "speech_finalized")

                    # TEMPORAL: simulamos que el LLM respondió
                    await send_voice_event(session_id, "llm_response_ready")

                    await send_voice_event(session_id, "audio_stream_ended")

                await websocket.send_json({"type": "end_ack", "session_id": session_id})
                await websocket.close(code=1000)
                break

            await websocket.send_json({"type": "error", "reason": f"unknown type: {msg_type}"})

    except WebSocketDisconnect:
        if session_id:
            await send_voice_event(session_id, "audio_stream_ended")
    except Exception as e:
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
