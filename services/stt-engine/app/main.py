import base64
import os
import time
import uuid
from typing import Any, Dict, Optional
from fastapi import FastAPI
from pydantic import BaseModel
from app.vad import VoiceActivityDetector
import base64


from fastapi import FastAPI
from pydantic import BaseModel

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.vad_segmenter import VadSegmenter

# ---- tracing ----
def setup_tracing() -> None:
    service_name = os.getenv("OTEL_SERVICE_NAME", "bmo-stt-engine")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

setup_tracing()
tracer = trace.get_tracer("bmo.stt_engine")

# ---- app ----
app = FastAPI(title="bmo-stt-engine", version="0.2.0")
vad = VoiceActivityDetector()
FastAPIInstrumentor.instrument_app(app)

segmenter = VadSegmenter(
    sample_rate=16000,
    frame_ms=20,
    vad_aggressiveness=2,
    pre_roll_ms=200,
    hangover_ms=400,
    min_segment_ms=300,
    max_segment_ms=10000,
)
class AudioFrame(BaseModel):
    pcm_b64: str


# in-memory per session (for this phase)
_session_segmenters: Dict[str, VadSegmenter] = {}
# NOTE: We keep one global template but clone settings via new instance per session


def _get_seg(session_id: str) -> VadSegmenter:
    if session_id not in _session_segmenters:
        _session_segmenters[session_id] = VadSegmenter(
            sample_rate=16000,
            frame_ms=20,
            vad_aggressiveness=2,
            pre_roll_ms=200,
            hangover_ms=400,
            min_segment_ms=300,
            max_segment_ms=10000,
        )
    return _session_segmenters[session_id]


class AudioFrameIn(BaseModel):
    session_id: str
    timestamp_ms: int
    audio_format: str = "pcm16"
    sample_rate: int = 16000
    frame_ms: int = 20
    pcm16_b64: str  # base64 of raw PCM16 mono 16k


class EndStreamIn(BaseModel):
    session_id: str


@app.get("/health")
def health():
    return {"status": "ok", "service": "stt-engine"}


@app.post("/frame")
def ingest_frame(payload: AudioFrameIn):
    if payload.audio_format != "pcm16" or payload.sample_rate != 16000 or payload.frame_ms != 20:
        return {"error": "unsupported format in this step (need pcm16 mono 16k, 20ms)"}

    pcm = base64.b64decode(payload.pcm16_b64)

    seg = _get_seg(payload.session_id)

    with tracer.start_as_current_span("stt.vad.frame") as span:
        span.set_attribute("bmo.session_id", payload.session_id)
        span.set_attribute("bmo.timestamp_ms", payload.timestamp_ms)

        segments = seg.push_frame(payload.timestamp_ms, pcm)

        if not segments:
            return {"status": "frame_ok", "segments": 0}

        # Return first segment (keep API simple in this step)
        s = segments[0]
        segment_id = str(uuid.uuid4())

        span.set_attribute("bmo.segment_ready", True)
        span.set_attribute("bmo.segment_start_ms", s.start_ms)
        span.set_attribute("bmo.segment_end_ms", s.end_ms)

        return {
            "status": "segment_ready",
            "event": "audio.segment.ready",
            "data": {
                "session_id": payload.session_id,
                "segment_id": segment_id,
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "audio_format": "pcm16",
                "sample_rate": 16000,
                "pcm16_b64": base64.b64encode(s.pcm16).decode("ascii"),
            },
        }


@app.post("/end")
def end_stream(payload: EndStreamIn):
    seg = _get_seg(payload.session_id)

    with tracer.start_as_current_span("stt.vad.flush") as span:
        span.set_attribute("bmo.session_id", payload.session_id)

        segments = seg.flush()
        if not segments:
            return {"status": "ok", "segments": 0}

        s = segments[0]
        segment_id = str(uuid.uuid4())

        return {
            "status": "segment_ready",
            "event": "audio.segment.ready",
            "data": {
                "session_id": payload.session_id,
                "segment_id": segment_id,
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "audio_format": "pcm16",
                "sample_rate": 16000,
                "pcm16_b64": base64.b64encode(s.pcm16).decode("ascii"),
            },
        }
        
@app.post("/vad/frame")
def vad_frame(payload: AudioFrame):
    pcm = base64.b64decode(payload.pcm_b64)
    event = vad.process(pcm)
    return {
        "vad": event,
        "bytes": len(pcm)
    }

