from __future__ import annotations
import collections
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple
import webrtcvad


@dataclass
class Segment:
    start_ms: int
    end_ms: int
    pcm16: bytes  # raw PCM16 mono @ 16k


class VadSegmenter:
    """
    Streaming VAD segmenter for PCM16 mono @16kHz.

    Frame size must be 10/20/30ms. We'll use 20ms default.
    """
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        vad_aggressiveness: int = 2,
        pre_roll_ms: int = 200,
        hangover_ms: int = 400,
        min_segment_ms: int = 300,
        max_segment_ms: int = 10000,
    ) -> None:
        if sample_rate != 16000:
            raise ValueError("Only 16kHz supported in this step.")
        if frame_ms not in (10, 20, 30):
            raise ValueError("frame_ms must be 10/20/30")
        if not (0 <= vad_aggressiveness <= 3):
            raise ValueError("vad_aggressiveness must be 0..3")

        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.bytes_per_frame = int(sample_rate * (frame_ms / 1000.0) * 2)  # 16-bit mono

        self.vad = webrtcvad.Vad(vad_aggressiveness)

        self.pre_roll_frames = max(0, pre_roll_ms // frame_ms)
        self.hangover_frames = max(0, hangover_ms // frame_ms)
        self.min_segment_frames = max(1, min_segment_ms // frame_ms)
        self.max_segment_frames = max(1, max_segment_ms // frame_ms)

        self._ring: Deque[Tuple[int, bytes, bool]] = collections.deque(maxlen=self.pre_roll_frames)
        self._in_speech = False
        self._speech_frames: List[Tuple[int, bytes]] = []
        self._silence_run = 0

        self._current_start_ms: Optional[int] = None

    def push_frame(self, timestamp_ms: int, pcm16_frame: bytes) -> List[Segment]:
        if len(pcm16_frame) != self.bytes_per_frame:
            raise ValueError(f"Bad frame size. Expected {self.bytes_per_frame} bytes, got {len(pcm16_frame)}")

        is_speech = self.vad.is_speech(pcm16_frame, self.sample_rate)
        segments: List[Segment] = []

        # Always fill pre-roll ring
        self._ring.append((timestamp_ms, pcm16_frame, is_speech))

        if not self._in_speech:
            if is_speech:
                # Start speech: include pre-roll
                self._in_speech = True
                self._silence_run = 0

                pre = list(self._ring)
                # Find earliest timestamp in pre-roll
                self._current_start_ms = pre[0][0] if pre else timestamp_ms

                self._speech_frames = [(t, b) for (t, b, _) in pre]
            return segments

        # in speech
        self._speech_frames.append((timestamp_ms, pcm16_frame))

        if is_speech:
            self._silence_run = 0
        else:
            self._silence_run += 1

        total_frames = len(self._speech_frames)

        # flush on max duration
        if total_frames >= self.max_segment_frames:
            segments.append(self._finalize())
            return segments

        # finalize if enough trailing silence
        if self._silence_run >= self.hangover_frames:
            # ensure min duration
            if total_frames >= self.min_segment_frames:
                segments.append(self._finalize())
            else:
                # too short -> discard
                self._reset()
            return segments

        return segments

    def flush(self) -> List[Segment]:
        segments: List[Segment] = []
        if self._in_speech and len(self._speech_frames) >= self.min_segment_frames:
            segments.append(self._finalize())
        else:
            self._reset()
        return segments

    def _finalize(self) -> Segment:
        assert self._current_start_ms is not None
        start_ms = self._current_start_ms
        end_ms = self._speech_frames[-1][0] + self.frame_ms
        pcm = b"".join([b for (_, b) in self._speech_frames])
        seg = Segment(start_ms=start_ms, end_ms=end_ms, pcm16=pcm)
        self._reset()
        return seg

    def _reset(self) -> None:
        self._in_speech = False
        self._speech_frames = []
        self._silence_run = 0
        self._current_start_ms = None
