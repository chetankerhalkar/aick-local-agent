"""Stage 3 boundary/worker tests. Model output in this file is explicitly mocked."""
import asyncio
import io
import os
import re
import struct
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import app
import audio_transcription as audio
from meeting import Meeting

WORK = Path(__file__).resolve().parent / "work"
WORK.mkdir(exist_ok=True)


def wav_bytes(seconds=1, rate=16000, channels=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0\0" * int(seconds * rate) * channels)
    return buffer.getvalue()


class WavTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="wav-unit-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_pcm_metadata_and_invalid_or_truncated_audio(self):
        path = self.root / "input.wav"
        path.write_bytes(wav_bytes(1.25))
        self.assertEqual(audio.validate_wav(path)["audio_duration_s"], 1.25)
        for data in (b"not audio", wav_bytes()[:-1], wav_bytes(0), wav_bytes(channels=3)):
            path.write_bytes(data)
            with self.assertRaises(audio.AudioError):
                audio.validate_wav(path)
        data = bytearray(wav_bytes()[:-4])
        data[4:8] = struct.pack("<I", len(data) - 8)  # Consistent RIFF, truncated data chunk.
        path.write_bytes(data)
        with self.assertRaises(audio.AudioError):
            audio.validate_wav(path)

    def test_duration_and_byte_limits(self):
        path = self.root / "input.wav"
        path.write_bytes(wav_bytes(181, 8000))
        with self.assertRaisesRegex(audio.AudioError, "180 seconds"):
            audio.validate_wav(path)
        path.write_bytes(wav_bytes())
        with patch.object(audio, "MAX_AUDIO_BYTES", 10):
            with self.assertRaisesRegex(audio.AudioError, "25 MiB"):
                audio.validate_wav(path)

    def test_model_loaded_once_offline_and_files_removed(self):
        import numpy as np
        model_dir = self.root / "model"
        model_dir.mkdir()
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
            (model_dir / name).touch()
        transcriber = audio.LocalTranscriber()
        with patch("faster_whisper.WhisperModel") as model, patch("faster_whisper.audio.decode_audio", return_value=np.ones(16000, dtype="float32")):
            def response(*args, **kwargs):
                return iter([SimpleNamespace(start=0.24, end=0.88, text=" Mock recognition. ")]), SimpleNamespace(duration_after_vad=0.8)
            model.return_value.transcribe.side_effect = response
            for number in range(2):
                path = self.root / "session" / f"job{number}" / "upload.wav"
                path.parent.mkdir(parents=True)
                path.write_bytes(wav_bytes())
                result = transcriber.transcribe(path, model_dir)
                self.assertFalse(path.exists())
                self.assertEqual(result["segments"][0]["start_s"], 0.24)
                self.assertGreaterEqual(result["transcription_duration_s"], 0)
            model.assert_called_once_with(str(model_dir.resolve()), local_files_only=True, device="cpu", compute_type="int8", cpu_threads=4, num_workers=1)
            self.assertEqual(transcriber.initializations, 1)
            self.assertEqual(model.return_value.transcribe.call_args.kwargs, dict(language="en", beam_size=5, vad_filter=True, condition_on_previous_text=False, temperature=0))
            with patch("faster_whisper.audio.decode_audio", return_value=np.zeros(16000, dtype="float32")):
                path = self.root / "session" / "silence" / "upload.wav"
                path.parent.mkdir(parents=True)
                path.write_bytes(wav_bytes())
                self.assertEqual(transcriber.transcribe(path, model_dir)["segments"], [])
                self.assertEqual(model.return_value.transcribe.call_count, 2)
        self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
        self.assertEqual(os.environ["HF_HUB_DISABLE_TELEMETRY"], "1")

    def test_worker_failure_also_deletes_audio(self):
        path = self.root / "session" / "job" / "upload.wav"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"invalid")
        with self.assertRaises(audio.AudioError):
            audio.LocalTranscriber().transcribe(path, self.root / "missing-model")
        self.assertFalse(path.exists())

    def test_batch_offsets_no_speaker_stable_ids_and_atomic_limit(self):
        meeting = Meeting()
        meeting.add(text="Typed first.", start_s=0, end_s=None, speaker=None, source_type="typed", source_label="Typed transcript")
        rows = [{"start_s": 0.24, "end_s": 1.42, "text": "Mock recognition."}]
        meeting.append_audio(rows, "U-test", 60)
        segment = meeting.segments[-1]
        self.assertEqual((segment.id, segment.start_s, segment.end_s), ("S0002", 60.24, 61.42))
        self.assertEqual((segment.audio_start_s, segment.audio_end_s), (0.24, 1.42))
        self.assertIsNone(segment.speaker)
        self.assertEqual(segment.source_label, "Uploaded audio")
        before = meeting.view()
        with patch("meeting.MAX_SEGMENTS", 2):
            with self.assertRaises(ValueError):
                meeting.append_audio(rows, "U-next", 0)
        self.assertEqual(before, meeting.view())
        meeting.append_audio([], "U-silent", 0)
        self.assertEqual(before, meeting.view())


class UploadApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url=app.ORIGIN)
        page = await self.client.get("/")
        self.token = re.search(r'name="aick-session-token" content="([^"]+)"', page.text)[1]
        self.client.headers.update({"X-Session-Token": self.token, "Origin": app.ORIGIN})
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="upload-unit-")
        self.root = Path(self.temp.name)
        self.transcriber = audio.LocalTranscriber()
        self.patches = [patch.object(app, "ROOT", self.root), patch.object(app, "ASR_LOCK", asyncio.Lock()),
                        patch.object(app, "TRANSCRIBER", self.transcriber),
                        patch.object(app, "read_config", return_value=({"asr_model_path": str(self.root / "model")}, self.root / "prepared-config.json", None))]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        if app.AUDIO_TASKS:
            await asyncio.gather(*app.AUDIO_TASKS, return_exceptions=True)
        await asyncio.to_thread(self.transcriber.shutdown)
        for item in reversed(self.patches):
            item.stop()
        await self.client.aclose()
        self.temp.cleanup()

    async def upload(self, data=None, **kwargs):
        return await self.client.post("/api/meeting/audio?revision=0", content=wav_bytes() if data is None else data, headers={"Content-Type": "audio/wav"}, **kwargs)

    async def finish(self):
        await asyncio.gather(*app.AUDIO_TASKS)
        return (await self.client.get("/api/meeting")).json()

    async def test_bounded_worker_responsive_reads_and_locked_edits(self):
        release = threading.Event()
        def blocked_worker(*args):
            release.wait(3)
            return {"segments": [{"start_s": 0.2, "end_s": 0.8, "text": "Mock recognition."}], "audio_duration_s": 1, "transcription_duration_s": 0.1, "message": "Mock result"}
        with patch.object(self.transcriber, "transcribe", side_effect=blocked_worker):
            try:
                response = await self.upload()
                self.assertEqual(response.status_code, 202)
                started = time.perf_counter()
                state = (await self.client.get("/api/meeting")).json()
                self.assertLess(time.perf_counter() - started, 0.5)
                self.assertEqual(state["state"], "processing")
                self.assertEqual((await self.upload()).status_code, 409)
                self.assertEqual((await self.client.post("/api/meeting/new", json={"revision": 0})).status_code, 409)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url=app.ORIGIN) as other:
                    # Bootstrap a second session using the actual static path during this one request.
                    with patch.object(app, "ROOT", Path(__file__).resolve().parent):
                        page = await other.get("/")
                    token = re.search(r'name="aick-session-token" content="([^"]+)"', page.text)[1]
                    response = await other.post("/api/meeting/audio?revision=0", content=wav_bytes(), headers={"Origin": app.ORIGIN, "X-Session-Token": token, "Content-Type": "audio/wav"})
                    self.assertEqual(response.status_code, 409)
            finally:
                release.set()
            state = await self.finish()
            self.assertEqual(state["segments"][0]["source_type"], "uploaded audio")
            self.assertTrue(state["audio_jobs"][-1]["audio_deleted"])
            self.assertFalse(list(self.root.rglob("*.wav")))

    async def test_empty_silence_and_worker_failure_append_nothing(self):
        self.assertEqual((await self.upload(b"")).status_code, 422)
        with patch.object(self.transcriber, "transcribe", return_value={"segments": [], "message": "Mock silence"}):
            self.assertEqual((await self.upload()).status_code, 202)
            state = await self.finish()
            self.assertEqual(state["segments"], [])
            self.assertEqual(state["revision"], 0)
        with patch.object(self.transcriber, "transcribe", side_effect=audio.AudioError("Mock decoder failure")):
            await self.upload()
            state = await self.finish()
            self.assertEqual(state["state"], "error")
            self.assertEqual(state["segments"], [])
            self.assertTrue(state["audio_jobs"][-1]["audio_deleted"])
        self.assertFalse(list(self.root.rglob("*.wav")))

    async def test_oversized_header_stream_origin_and_offsets_rejected(self):
        with patch.object(app, "MAX_AUDIO_BYTES", 8):
            self.assertEqual((await self.upload()).status_code, 413)
            async def chunks():
                yield b"12345"
                yield b"67890"
            response = await self.client.post("/api/meeting/audio?revision=0", content=chunks(), headers={"Content-Type": "audio/wav"})
            self.assertEqual(response.status_code, 413)
        for query in ("revision=0&offset_s=-1", "revision=0&offset_s=nan", "revision=-1"):
            self.assertEqual((await self.client.post("/api/meeting/audio?" + query, content=b"bad", headers={"Content-Type": "audio/wav"})).status_code, 422)
        self.assertEqual((await self.client.post("/api/meeting/audio?revision=0", content=b"bad", headers={"Content-Type": "audio/wav", "Origin": "https://bad.invalid"})).status_code, 403)
        self.assertFalse(list(self.root.rglob("*.wav")))


if __name__ == "__main__":
    unittest.main()
