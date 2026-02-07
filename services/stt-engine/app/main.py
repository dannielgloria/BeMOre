from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import uuid

app = FastAPI(title="bmo-stt-engine")

class SegmentReady(BaseModel):
    session_id: str
    start_ms: int
    end_ms: int
    audio_format: str
    sample_rate: int

class STTResult(BaseModel):
    session_id: str
    segment_id: str
    text: str
    confidence: float
    language: Optional[str] = None

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/segment")
def process_segment(payload: SegmentReady):
    # Placeholder: no Whisper yet
    segment_id = str(uuid.uuid4())

    return {
        "event": "stt.final",
        "data": STTResult(
            session_id=payload.session_id,
            segment_id=segment_id,
            text="(stub)",
            confidence=0.0,
            language="und"
        )
    }
