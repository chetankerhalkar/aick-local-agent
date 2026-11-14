"""Stage 4 PCM framing, bounded windows, resampling and conservative overlap checks."""
import asyncio
import math
import re
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from audio_transcription import AudioError
from meeting import MAX_SEGMENTS, MAX_SESSION_CHARS, Segment

WINDOW_SECONDS = 8
OVERLAP_SECONDS = 1
QUEUE_WINDOWS = 2
MAX_CAPTURE_SECONDS = 8 * 60 * 60


def resample_mono(pcm, sample_rate):
    from scipy.signal import resample_poly
    if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 96000:
        raise AudioError("Unsupported AudioContext sample rate.")
    data = np.asarray(pcm, dtype=np.float32)
    if data.ndim != 1 or not 0 < len(data) <= sample_rate * WINDOW_SECONDS:
        raise AudioError("PCM window must be mono and at most eight seconds.")
    if not np.isfinite(data).all() or np.max(np.abs(data)) > 1.01:
        raise AudioError("PCM samples must be finite and in [-1, 1].")
    divisor = math.gcd(sample_rate, 16000)
    result = data.copy() if sample_rate == 16000 else resample_poly(data, 16000 // divisor, sample_rate // divisor)
    return np.ascontiguousarray(result, dtype=np.float32)


def unpack_packet(packet, sample_rate):
    # uint32 generation, float64 first sample index, float32 mono PCM; little endian.
    if len(packet) <= 12 or (len(packet) - 12) % 4 or len(packet) > 12 + math.ceil(sample_rate / 4) * 4:
        raise AudioError("PCM packet must contain at most 250 ms of mono float32 audio.")
    epoch, position = struct.unpack_from("<Id", packet)
    if not math.isfinite(position) or position != int(position) or not 0 <= position < sample_rate * MAX_CAPTURE_SECONDS:
        raise AudioError("Invalid PCM sample position.")
    pcm = np.frombuffer(packet, dtype="<f4", offset=12).copy()
    if not np.isfinite(pcm).all() or np.max(np.abs(pcm)) > 1.01:
        raise AudioError("Invalid PCM values.")
    return epoch, int(position), pcm


def word_match(first, second):
    normalize = lambda word: re.sub(r"[^\w']", "", word.lower())
    if not normalize(first["text"]) or normalize(first["text"]) != normalize(second["text"]):
        return False
    overlap = min(first["end_s"], second["end_s"]) - max(first["start_s"], second["start_s"])
    longest = max(first["end_s"] - first["start_s"], second["end_s"] - second["start_s"], 0.02)
    return overlap / longest >= 0.65 and abs(first["start_s"] - second["start_s"]) <= 0.12


def reconcile_overlap(rows, previous_words, window_start, previous_end, duration):
    """Remove only a >=2 word matching suffix/prefix at the SAME audio times."""
    absolute = []
    for row in rows:
        for item in [row, *row.get("words", [])]:
            start, end = item["start_s"], item["end_s"]
            if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= duration + 0.1):
                raise AudioError("Whisper returned invalid live offsets.")
        words = [{**w, "start_s": w["start_s"] + window_start, "end_s": w["end_s"] + window_start}
                 for w in row.get("words", [])]
        absolute.append({**row, "start_s": row["start_s"] + window_start,
                         "end_s": row["end_s"] + window_start, "words": words})
    prefix = [w for row in absolute for w in row["words"]]
    duplicate_count = 0
    for count in range(min(len(prefix), len(previous_words)), 1, -1):
        if all(word_match(a, b) and b["end_s"] <= previous_end + 0.12
               for a, b in zip(previous_words[-count:], prefix[:count])):
            duplicate_count = count
            break
    remaining = duplicate_count
    kept = []
    for row in absolute:
        words = row["words"]
        if remaining and words:
            trim = min(remaining, len(words))
            remaining -= trim
            words = words[trim:]
            if not words:
                continue
            row = {**row, "words": words, "start_s": words[0]["start_s"],
                   "text": "".join(word["text"] for word in words).strip()}
        note = None
        if row["start_s"] < previous_end - 0.05:
            note = "Overlap uncertain; words retained for review."
        kept.append({**row, "overlap_note": note})
    return kept, prefix, duplicate_count


@dataclass
class Window:
    start: int
    pcm: np.ndarray
    epoch: int
    queued_at: float


class LiveCapture:
    def __init__(self, meeting, sample_rate, mode, model_path, transcriber, still_current, send):
        self.meeting, self.rate, self.mode = meeting, sample_rate, mode
        self.model_path, self.transcriber = model_path, transcriber
        self.still_current, self.send = still_current, send
        self.id = "C-" + secrets.token_hex(8)
        self.active, self.paused, self.epoch = True, False, 0
        self.speech_hold = False
        self.queue = asyncio.Queue(maxsize=QUEUE_WINDOWS)
        self.buffer = np.empty(0, dtype=np.float32)
        self.buffer_start = self.position = 0
        self.base = max((s.end_s or s.start_s for s in meeting.segments), default=0)
        self.previous_words, self.previous_end = [], 0.0
        self.pause_start = None
        self.in_flight = None
        self.started = time.monotonic()
        self.arrivals = deque(maxlen=200)
        self.report = {"id": self.id, "active": True, "paused": False, "state": "listening", "epoch": 0,
                       "source_label": "Live meeting tab audio" if mode == "live audio" else "Sample replay (test PCM)",
                       "sample_rate": sample_rate, "target_sample_rate": 16000, "base_s": self.base,
                       "queue_windows": 0, "queue_limit": QUEUE_WINDOWS, "asr_processing": False,
                       "dropped_windows": 0, "dropped_seconds": 0.0, "received_seconds": 0.0,
                       "asr_latency_s": None, "transcription_duration_s": None, "intervals": [], "trace": [],
                       "message": "Listening to audio only. Ask agent and Update action board remain manual."}
        meeting.capture = self.report

    def trace(self, step, **data):
        self.report["trace"] = (self.report["trace"] + [{"step": step, **data}])[-40:]

    def gap(self, start, end, reason):
        if end <= start:
            return
        entry = {"start_s": self.base + start, "end_s": self.base + end, "reason": reason, "label": "Not transcribed"}
        intervals = self.report["intervals"]
        if intervals and intervals[-1]["reason"] == reason and abs(intervals[-1]["end_s"] - entry["start_s"]) < 0.05:
            intervals[-1]["end_s"] = entry["end_s"]
        else:
            intervals.append(entry)
        if len(intervals) > 100:
            intervals.pop(0)
            self.report["older_intervals_omitted"] = self.report.get("older_intervals_omitted", 0) + 1
        self.report["message"] = f"{reason}: audio was not transcribed. See the marked intervals."

    def discard_pending(self, reason):
        if len(self.buffer):
            self.gap(self.buffer_start / self.rate, self.position / self.rate, reason)
        self.buffer = np.empty(0, dtype=np.float32)
        while not self.queue.empty():
            window = self.queue.get_nowait()
            if window is not None:
                self.gap(window.start / self.rate, (window.start + len(window.pcm)) / self.rate, reason)
        self.report["queue_windows"] = 0
        if self.in_flight:
            window = self.in_flight
            self.gap(window.start / self.rate, (window.start + len(window.pcm)) / self.rate, reason)
        self.previous_words, self.previous_end = [], 0.0

    def stop(self, reason="Stopped by user"):
        if not self.active:
            return
        self.active = False
        self.epoch += 1
        self.discard_pending(reason)
        if self.pause_start is not None:
            self.gap(self.pause_start, self.position / self.rate, "Paused; not transcribed")
        self.report.update(active=False, paused=False, epoch=self.epoch, state="not connected", message=reason + ". Pending audio cleared; late results discarded.")
        self.queue.put_nowait(None)

    def pause(self, position, reason):
        self.position = max(self.position, position)
        if self.paused:
            return
        self.paused = True
        self.epoch += 1
        self.discard_pending("Paused for " + reason)
        self.pause_start = self.position / self.rate
        self.pause_reason = reason
        self.report.update(paused=True, epoch=self.epoch, state="ready", message=f"Paused for {reason}. This interval is not transcribed.")

    def resume(self, position):
        if self.speech_hold:
            self.report["message"] = "Listening paused for speech output. End playback or click Stop speaking to resume."
            return
        if not self.paused:
            return
        self.position = max(self.position, position)
        self.gap(self.pause_start, self.position / self.rate, "Paused for " + self.pause_reason)
        self.pause_start = None
        self.paused = False
        self.epoch += 1
        self.report.update(paused=False, epoch=self.epoch, state="listening", message="Listening resumed. The paused interval remains marked as not transcribed.")

    def feed(self, packet):
        epoch, position, pcm = unpack_packet(packet, self.rate)
        now = time.monotonic()
        self.arrivals.append(now)
        if len(self.arrivals) == self.arrivals.maxlen and now - self.arrivals[0] < 1:
            raise AudioError("PCM packet rate exceeds the bounded live protocol.")
        if not self.active or self.paused or epoch != self.epoch:
            return
        if position < self.position:
            raise AudioError("PCM packet positions must be ordered; replayed packets are rejected.")
        if position > self.position:
            self.gap(self.position / self.rate, position / self.rate, "Dropped browser audio")
            self.report["dropped_seconds"] += (position - self.position) / self.rate
            if len(self.buffer):
                self.gap(self.buffer_start / self.rate, self.position / self.rate, "Incomplete window before dropped browser audio")
            self.buffer = np.empty(0, dtype=np.float32)
        if not len(self.buffer):
            self.buffer_start = position
        self.position = position + len(pcm)
        self.report["received_seconds"] = self.position / self.rate
        self.buffer = np.concatenate((self.buffer, pcm))
        count = self.rate * WINDOW_SECONDS
        hop = self.rate * (WINDOW_SECONDS - OVERLAP_SECONDS)
        if len(self.buffer) >= count:
            window = Window(self.buffer_start, self.buffer[:count].copy(), self.epoch, now)
            if self.queue.full():
                old = self.queue.get_nowait()
                self.report["dropped_windows"] += 1
                self.report["dropped_seconds"] += len(old.pcm) / self.rate
                self.gap(old.start / self.rate, (old.start + len(old.pcm)) / self.rate, "ASR backlog: dropped window (overlap may be covered)")
            self.queue.put_nowait(window)
            self.buffer = self.buffer[hop:].copy()
            self.buffer_start += hop
            self.report["queue_windows"] = self.queue.qsize()

    async def consume(self):
        while self.active:
            window = await self.queue.get()
            if window is None or not self.active:
                break
            self.in_flight = window
            self.report.update(queue_windows=self.queue.qsize(), asr_processing=True)
            try:
                result = await self.transcriber.run_pcm(window.pcm, self.rate, self.model_path)
                if not self.active or self.paused or window.epoch != self.epoch or not self.still_current():
                    self.trace("discarded late result", epoch=window.epoch)
                    continue
                start = window.start / self.rate
                rows, words, trimmed = reconcile_overlap(result["segments"], self.previous_words,
                    start, self.previous_end, len(window.pcm) / self.rate)
                if len(self.meeting.segments) + len(rows) > MAX_SEGMENTS or sum(len(s.text) for s in self.meeting.segments) + sum(len(r["text"]) for r in rows) > MAX_SESSION_CHARS:
                    raise AudioError("Session transcript limit reached. Stop and start a new session.")
                latency = time.monotonic() - window.queued_at
                batch = [Segment(id=f"S{self.meeting.next_segment + i:04d}",
                    start_s=self.base + row["start_s"], end_s=self.base + row["end_s"],
                    source_type=self.mode, source_label=self.report["source_label"], speaker=None,
                    text=row["text"], capture_id=self.id, overlap_note=row["overlap_note"], asr_latency_s=latency)
                    for i, row in enumerate(rows)]
                self.meeting.segments.extend(batch)
                self.meeting.next_segment += len(batch)
                self.meeting.revision += len(batch)
                self.previous_words, self.previous_end = words[-40:], start + len(window.pcm) / self.rate
                self.report.update(asr_latency_s=latency, transcription_duration_s=result["transcription_duration_s"])
                self.trace("transcribed", start_s=self.base + start, audio_duration_s=result["audio_duration_s"],
                    segments_added=len(batch), overlap_words_removed=trimmed, asr_latency_s=latency,
                    model_initializations=result["model_initializations"], input_sample_rate=self.rate, output_sample_rate=16000)
            except Exception as exc:
                self.trace("error", detail=str(exc) if isinstance(exc, AudioError) else "Local ASR failed")
                self.stop(str(exc) if isinstance(exc, AudioError) else "Local ASR failed")
                self.report["state"] = "error"
            finally:
                self.in_flight = None
                self.report["asr_processing"] = False
            await self.send({"type": "update", "capture_id": self.id, "meeting": self.meeting.view()})
