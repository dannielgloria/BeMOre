import os
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor


def setup_tracing() -> None:
    service_name = os.getenv("OTEL_SERVICE_NAME", "bmo-orchestrator")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


setup_tracing()
tracer = trace.get_tracer("bmo.orchestrator")

app = FastAPI(title="BMO Orchestrator", version="0.1.0")
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
def health():
    return {"status": "ok", "service": "orchestrator"}


@app.post("/session/new")
def new_session():
    with tracer.start_as_current_span("session.new") as span:
        session_id = str(uuid4())
        span.set_attribute("bmo.session_id", session_id)
        return JSONResponse(
            {
                "session_id": session_id,
                "created_at": int(time.time()),
            }
        )
