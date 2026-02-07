from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


VoiceState = str

# Canonical states
IDLE: VoiceState = "IDLE"
LISTENING: VoiceState = "LISTENING"
THINKING: VoiceState = "THINKING"
SPEAKING: VoiceState = "SPEAKING"
INTERRUPTED: VoiceState = "INTERRUPTED"

# Event types
AUDIO_STREAM_STARTED = "audio_stream_started"
AUDIO_STREAM_ENDED = "audio_stream_ended"
SPEECH_DETECTED = "speech_detected"
SPEECH_FINALIZED = "speech_finalized"
LLM_RESPONSE_READY = "llm_response_ready"
TTS_STARTED = "tts_started"
TTS_FINISHED = "tts_finished"
BARGE_IN = "barge_in"
CANCEL = "cancel"


@dataclass(frozen=True)
class Transition:
    from_state: VoiceState
    event: str
    to_state: VoiceState
    # optional "actions" emitted by control plane (commands)
    actions: Tuple[str, ...] = ()


# Deterministic transition table
TRANSITIONS: List[Transition] = [
    Transition(IDLE, AUDIO_STREAM_STARTED, LISTENING),
    Transition(LISTENING, SPEECH_DETECTED, LISTENING),
    Transition(LISTENING, SPEECH_FINALIZED, THINKING, actions=("request_llm",)),
    Transition(THINKING, LLM_RESPONSE_READY, SPEAKING, actions=("start_tts",)),
    Transition(SPEAKING, TTS_STARTED, SPEAKING),
    Transition(SPEAKING, TTS_FINISHED, IDLE),
    Transition(SPEAKING, BARGE_IN, INTERRUPTED, actions=("cancel_tts",)),
    Transition(INTERRUPTED, SPEECH_FINALIZED, THINKING, actions=("request_llm",)),
    Transition(LISTENING, AUDIO_STREAM_ENDED, IDLE),
    Transition(THINKING, CANCEL, IDLE, actions=("cancel_llm",)),
    Transition(SPEAKING, CANCEL, IDLE, actions=("cancel_tts",)),
    Transition(INTERRUPTED, CANCEL, IDLE, actions=("cancel_tts", "cancel_llm")),
]


class VoiceStateMachine:
    def __init__(self) -> None:
        self._states: Dict[str, VoiceState] = {}

    def get_state(self, session_id: str) -> VoiceState:
        return self._states.get(session_id, IDLE)

    def apply(self, session_id: str, event_type: str) -> Tuple[VoiceState, VoiceState, Tuple[str, ...]]:
        current = self.get_state(session_id)

        # Find first matching transition
        for t in TRANSITIONS:
            if t.from_state == current and t.event == event_type:
                self._states[session_id] = t.to_state
                return current, t.to_state, t.actions

        # Default: no transition (stay put), no actions
        return current, current, ()
