"""Bounded, offline WAV transcription. No browser audio or speaker identification."""
import os

# Hub reads these at import time. Set them before any faster-whisper/Hub import.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["DO_NOT_TRACK"] = "1"

import asyncio
import math
import struct
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_SECONDS = 180
UPLOAD_TIMEOUT_SECONDS = 30


class AudioError(ValueError):
    pass


def validate_wav(path: Path) -> dict:
    """Validate PCM header, duration and actual frame bytes before any model work."""
    size = path.stat().st_size
    if size > MAX_AUDIO_BYTES:
        raise AudioError("WAV exceeds the 25 MiB file limit.")
    with path.open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise AudioError("Upload must be a valid RIFF PCM WAV file.")
    if struct.unpack("<I", header[4:8])[0] + 8 != size:
        raise AudioError("WAV is truncated or has an invalid RIFF length.")
    try:
        with wave.open(str(path), "rb") as audio:
            channels, width, rate, frames, compression, _ = audio.getparams()
            if compression != "NONE" or channels not in (1, 2) or width not in (1, 2, 3, 4) or not 8000 <= rate <= 96000:
                raise AudioError("Use PCM WAV: mono/stereo, 8–32 bit, 8–96 kHz.")
            duration = frames / rate
            if not 0 < duration <= MAX_AUDIO_SECONDS:
                raise AudioError("WAV duration must be greater than zero and at most 180 seconds.")
            remaining = frames * channels * width
            while remaining:
                block = audio.readframes(min(16384, (remaining + channels * width - 1) // (channels * width)))
                if not block or len(block) > remaining:
                    raise AudioError("WAV audio frames are truncated or invalid.")
                remaining -= len(block)
            if audio.readframes(1):
                raise AudioError("WAV contains an incomplete PCM frame.")
            return {"audio_duration_s": duration, "sample_rate": rate, "channels": channels,
                    "sample_width_bytes": width, "bytes": size}
    except (wave.Error, EOFError, struct.error) as exc:
        raise AudioError("WAV header or audio frames are invalid.") from exc


def cleanup_upload(path: Path) -> bool:
    """Only remove the exact server-created file and its now-empty directories."""
    try:
        path.unlink(missing_ok=True)
    finally:
        for directory in (path.parent, path.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass
    return not path.exists()


class LocalTranscriber:
    """One worker, one retained model. Admission is guarded before submission by the app."""
    def __init__(self):
        self._executor = None
        self._model = None
        self._model_path = None
        self.initializations = 0

    @property
    def loaded(self):
        return self._model is not None

    @property
    def model_path(self):
        return self._model_path

    async def run(self, path: Path, model_path: Path):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aick-whisper")
        return await asyncio.get_running_loop().run_in_executor(self._executor, self.transcribe, path, model_path)

    async def run_pcm(self, pcm, sample_rate, model_path):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aick-whisper")
        return await asyncio.get_running_loop().run_in_executor(self._executor, self.transcribe_pcm, pcm, sample_rate, model_path)

    def ensure_model(self, model_path):
        model_path = model_path.resolve()
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
            if not (model_path / name).is_file():
                raise AudioError("Configured local Whisper model is incomplete. No downloads are allowed.")
        if self.loaded and self._model_path != model_path:
            raise AudioError("Configured Whisper path changed. Restart the application before using another model.")
        started = time.perf_counter()
        if not self.loaded:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(str(model_path), local_files_only=True, device="cpu",
                                       compute_type="int8", cpu_threads=4, num_workers=1)
            self._model_path = model_path
            self.initializations += 1
            return time.perf_counter() - started
        return 0.0

    def transcribe_pcm(self, pcm, sample_rate, model_path):
        # Resample whole overlapping windows, never independent tiny packets.
        from live_audio import resample_mono
        import numpy as np
        started = time.perf_counter()
        samples = resample_mono(pcm, sample_rate)
        resample_seconds = time.perf_counter() - started
        load_seconds = self.ensure_model(model_path)
        asr_started = time.perf_counter()
        rows = []
        if np.any(samples):
            segments, info = self._model.transcribe(samples, language="en", beam_size=5,
                vad_filter=True, condition_on_previous_text=False, temperature=0, word_timestamps=True)
            for segment in segments:
                if segment.text.strip():
                    rows.append({"start_s": float(segment.start), "end_s": float(segment.end),
                                 "text": segment.text.strip(), "words": [
                                     {"start_s": float(w.start), "end_s": float(w.end), "text": w.word}
                                     for w in (segment.words or [])]})
                if len(rows) > 300:
                    raise AudioError("Live window returned too many segments.")
        return {"segments": rows, "audio_duration_s": len(pcm) / sample_rate,
                "transcription_duration_s": time.perf_counter() - asr_started,
                "resample_duration_s": resample_seconds, "model_load_duration_s": load_seconds,
                "model_initializations": self.initializations}

    def shutdown(self):
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None

    def transcribe(self, path: Path, model_path: Path) -> dict:
        started = time.perf_counter()
        try:
            metadata = validate_wav(path)
            model_path = model_path.resolve()
            for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
                if not (model_path / name).is_file():
                    raise AudioError("Configured local Whisper model is incomplete. No downloads are allowed.")
            if self.loaded and self._model_path != model_path:
                raise AudioError("Configured Whisper path changed. Restart the application before using another model.")
            from faster_whisper import WhisperModel
            from faster_whisper.audio import decode_audio
            import numpy as np

            decode_started = time.perf_counter()
            try:
                audio = decode_audio(str(path), sampling_rate=16000)
            except Exception as exc:
                raise AudioError("The uploaded WAV could not be decoded locally.") from exc
            if not len(audio) or not np.isfinite(audio).all():
                raise AudioError("Decoded WAV is empty or contains invalid samples.")
            decode_seconds = time.perf_counter() - decode_started
            load_seconds = self.ensure_model(model_path)
            transcription_started = time.perf_counter()
            rows = []
            speech_seconds = 0.0
            digital_silence = not np.any(audio)
            if not digital_silence:
                segments, info = self._model.transcribe(audio, language="en", beam_size=5,
                    vad_filter=True, condition_on_previous_text=False, temperature=0)
                for segment in segments:
                    text = segment.text.strip()
                    if not text:
                        continue
                    start, end = float(segment.start), float(segment.end)
                    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end
                            and end <= metadata["audio_duration_s"] + 0.1):
                        raise AudioError("Whisper returned invalid offsets. No segments were appended.")
                    rows.append({"start_s": start, "end_s": end, "text": text})
                    if len(rows) > 300 or sum(len(row["text"]) for row in rows) > 120_000:
                        raise AudioError("Transcription exceeds the session limit. No segments appended.")
                speech_seconds = float(info.duration_after_vad)
            transcription_seconds = time.perf_counter() - transcription_started
            return {**metadata, "segments": rows, "speech_duration_s": speech_seconds,
                    "decode_duration_s": decode_seconds, "model_load_duration_s": load_seconds,
                    "transcription_duration_s": transcription_seconds,
                    "processing_duration_s": time.perf_counter() - started,
                    "model_initializations": self.initializations,
                    "message": ("Digital silence detected; no text was generated." if digital_silence else
                                "No speech detected; no segments added." if not rows else
                                "Uploaded audio transcribed locally. Review recognition accuracy before relying on it.")}
        finally:
            cleanup_upload(path)
