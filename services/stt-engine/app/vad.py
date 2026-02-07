import webrtcvad
from collections import deque

SAMPLE_RATE = 16000
FRAME_DURATION_MS = 20
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000) * 2  # bytes

class VoiceActivityDetector:
    def __init__(self, mode: int = 2, padding_ms: int = 300):
        self.vad = webrtcvad.Vad(mode)
        self.ring_buffer = deque(maxlen=padding_ms // FRAME_DURATION_MS)
        self.triggered = False

    def process(self, frame: bytes) -> str | None:
        if len(frame) != FRAME_SIZE:
            return None

        is_speech = self.vad.is_speech(frame, SAMPLE_RATE)

        if not self.triggered:
            self.ring_buffer.append(is_speech)
            if sum(self.ring_buffer) > 0.8 * self.ring_buffer.maxlen:
                self.triggered = True
                self.ring_buffer.clear()
                return "speech_start"
        else:
            self.ring_buffer.append(is_speech)
            if sum(self.ring_buffer) < 0.2 * self.ring_buffer.maxlen:
                self.triggered = False
                self.ring_buffer.clear()
                return "speech_end"

        return None
