"""Speech safety/lifecycle unit tests. Synthesized WAVs here are explicit mocks."""
import asyncio
import json
import re
import struct
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import app
from meeting import Meeting
from live_audio import LiveCapture
from speech_output import SpeechError, SpeechOutput, cleanup, selected_text, synthesize


def wav(path):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(22050)
        out.writeframes(struct.pack("<h", 100) * 22050)


def fake_synthesize(root, directory, text, voice):
    directory.mkdir(parents=True, exist_ok=False)
    wav(directory / "reply.wav")
    return {"voice": "Unit test mock", "culture": "en-US", "audio_duration_s": 1.0}


def answer_meeting():
    meeting = Meeting()
    meeting.result = {"run_id": "R-unit", "revision": 0, "answer": [{"id": "C1", "text": "The notebook is blue."}]}
    return meeting


class SelectionAndCommandTests(unittest.TestCase):
    def test_current_selected_answer_only_and_bounded_unicode(self):
        meeting = answer_meeting()
        self.assertEqual(selected_text(meeting, "R-unit", "C1"), "The notebook is blue.")
        for run, claim in (("wrong", "C1"), ("R-unit", "../../other")):
            with self.assertRaises(SpeechError): selected_text(meeting, run, claim)
        for invalid in ("x" * 1001, "\x00", "😀" * 501):
            meeting.result["answer"][0]["text"] = invalid
            with self.assertRaises(SpeechError): selected_text(meeting, "R-unit", "C1")
        meeting.revision += 1
        with self.assertRaises(SpeechError): selected_text(meeting, "R-unit", "C1")

    def test_utf8_text_is_never_executable_or_a_command_argument(self):
        dangerous = 'Hello “reader”. $(Write-Output injected); `"; <speak>literal</speak>\nCafé'
        with tempfile.TemporaryDirectory(dir=app.ROOT / "work") as base:
            directory = Path(base) / "M-unit" / "T-unit"
            def execute(args, **kwargs):
                self.assertFalse(kwargs["shell"])
                self.assertEqual(kwargs["timeout"], 30)
                self.assertNotIn(dangerous, args)
                self.assertNotIn("-Command", args)
                self.assertEqual(Path(args[args.index("-TextPath")+1]).read_text(encoding="utf-8"), dangerous)
                self.assertEqual(Path(args[args.index("-File")+1]), app.ROOT / "synthesize_speech.ps1")
                wav(Path(args[args.index("-WavPath")+1]))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"name": "Mock", "culture": "en-US"}))
            with patch("speech_output.subprocess.run", side_effect=execute):
                result = synthesize(app.ROOT, directory, dangerous, None)
            self.assertEqual(result["audio_duration_s"], 1)
            self.assertFalse((directory / "input.txt").exists())
            cleanup(directory)
            self.assertFalse(directory.exists())


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=app.ROOT / "work")
        self.meeting = answer_meeting()
        self.capture = LiveCapture(self.meeting, 48000, "live audio", Path("unused"), None, lambda: True, AsyncMock())
        self.output = SpeechOutput(Path(self.temp.name), {"token": self.capture})

    async def asyncTearDown(self):
        await self.output.shutdown()
        self.temp.cleanup()

    async def start(self):
        return await self.output.start("token", self.meeting, "The notebook is blue.", "C1", "R-unit", None)

    async def generated(self):
        await asyncio.gather(*self.output.tasks)

    async def test_pause_blocks_resume_and_stale_audio_then_cleanup_and_resume(self):
        self.capture.position = 48000
        with patch("speech_output.synthesize", side_effect=fake_synthesize):
            report = await self.start()
            self.assertTrue(self.capture.paused)
            self.capture.resume(96000)
            self.assertTrue(self.capture.paused)
            self.capture.feed(struct.pack("<Id", 0, 48000) + b"\0" * 400)
            self.assertEqual(len(self.capture.buffer), 0)
            await self.generated()
        self.assertEqual(report["phase"], "ready")
        self.assertTrue(self.capture.paused)  # ready/autoplay waiting must not resume
        with self.assertRaises(SpeechError): self.output.owned("other-session", report["id"])
        with self.assertRaises(SpeechError): await self.start()
        self.capture.position = 480000
        directory = self.output.active["directory"]
        await self.output.finish("token", report["id"], "ended")
        self.assertFalse(self.capture.paused)
        self.assertEqual(self.capture.report["intervals"][-1]["reason"], "Paused for speech output")
        self.assertEqual(self.capture.report["intervals"][-1]["end_s"], 10)
        self.assertTrue(report["files_deleted"])
        self.assertFalse(directory.exists())

    async def test_manual_pause_is_preserved(self):
        self.capture.pause(0, "user pause")
        with patch("speech_output.synthesize", side_effect=fake_synthesize):
            report = await self.start(); await self.generated()
        await self.output.finish("token", report["id"], "stopped")
        self.assertTrue(self.capture.paused)
        self.assertFalse(self.capture.speech_hold)

    async def test_bounded_worker_cancel_never_publishes_late_ready(self):
        release = threading.Event()
        def slow(*args):
            release.wait(3)
            return fake_synthesize(*args)
        with patch("speech_output.synthesize", side_effect=slow):
            report = await asyncio.wait_for(self.start(), 0.3)
            await self.output.finish("token", report["id"], "stopped")
            self.assertTrue(report["active"])
            self.assertEqual(report["phase"], "stopping")
            self.assertTrue(self.capture.paused)
            release.set(); await self.generated()
        self.assertFalse(report["active"])
        self.assertEqual(report["phase"], "stopped")
        self.assertTrue(report["files_deleted"])

    async def test_abandoned_playback_stops_capture_instead_of_resuming(self):
        with patch("speech_output.synthesize", side_effect=fake_synthesize):
            report = await self.start(); await self.generated()
        await self.output.finish("token", report["id"], "expired")
        self.assertFalse(self.capture.active)
        self.assertTrue(report["files_deleted"])


class SpeechBoundaryTests(unittest.TestCase):
    def test_approval_selected_ids_and_security_required(self):
        with TestClient(app.app, base_url=app.ORIGIN) as client:
            token = re.search(r'name="aick-session-token" content="([^"]+)"', client.get("/").text)[1]
            headers = {"X-Session-Token": token, "Origin": app.ORIGIN}
            body = {"revision": 0, "run_id": "R-unit", "answer_id": "C1", "approved": True}
            app.MEETINGS[token] = answer_meeting()
            self.assertEqual(client.post("/api/meeting/speech", json=body).status_code, 403)
            self.assertEqual(client.post("/api/meeting/speech", json=body, headers={**headers, "Origin": "https://evil.invalid"}).status_code, 403)
            for extra in ({"approved": False}, {"path": "C:/arbitrary.wav"}, {"text": "arbitrary speech"}):
                self.assertEqual(client.post("/api/meeting/speech", json={**body, **extra}, headers=headers).status_code, 422)
            self.assertEqual(client.get("/api/meeting/speech/T-missing/wav", headers=headers).status_code, 409)


if __name__ == "__main__": unittest.main()
