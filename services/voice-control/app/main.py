import os
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

import uvicorn

from app.state_machine import VoiceStateMachine


def setup_tracing() -> None:
    service_name = os.getenv("OTEL_SERVICE_NAME", "bmo-voice-control")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


setup_tracing()
tracer = trace.get_tracer("bmo.voice_control")

app = FastAPI(title="BMO Voice Control", version="0.1.0")
FastAPIInstrumentor.instrument_app(app)

sm = VoiceStateMachine()


@app.get("/health")
def health():
    return {"status": "ok", "service": "voice-control"}


@app.get("/state/{session_id}")
def get_state(session_id: str):
    return {"session_id": session_id, "state": sm.get_state(session_id)}


@app.post("/event")
def post_event(payload: Dict[str, Any]):
    # Minimal validation for now; contracts exist in repo
    event_type = payload.get("type")
    session_id = payload.get("session_id")
    timestamp_ms = payload.get("timestamp_ms")

    if not event_type or not session_id or timestamp_ms is None:
        return JSONResponse(
            {"error": "missing required fields: type, session_id, timestamp_ms"},
            status_code=400,
        )

    with tracer.start_as_current_span("voice.event") as span:
        span.set_attribute("bmo.session_id", session_id)
        span.set_attribute("bmo.event_type", event_type)

        before, after, actions = sm.apply(session_id=session_id, event_type=event_type)

        span.set_attribute("bmo.state_before", before)
        span.set_attribute("bmo.state_after", after)
        span.set_attribute("bmo.actions_count", len(actions))

        # Return deterministic control decisions
        return {
            "session_id": session_id,
            "event": event_type,
            "state_before": before,
            "state_after": after,
            "actions": list(actions),
            "processed_at_ms": int(time.time() * 1000),
        }


if __name__ == "__main__":
    port = int(os.getenv("VOICE_PORT", "8085"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")
