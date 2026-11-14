"""Live audio protocol tests. ASR responses in these unit tests are mocks."""
import asyncio
import json
import re
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app
from audio_transcription import AudioError
from live_audio import LiveCapture, reconcile_overlap, resample_mono, unpack_packet
from meeting import Meeting


def packet(position, count=2000, epoch=0):
    return struct.pack("<Id", epoch, position) + np.zeros(count, dtype="<f4").tobytes()


def word(text, start, end):
    return {"text": text, "start_s": start, "end_s": end}


class SignalTests(unittest.TestCase):
    def test_resampling_preserves_duration_and_pitch(self):
        for rate in (22050, 44100, 48000, 96000):
            t = np.arange(rate * 3) / rate
            result = resample_mono((0.4 * np.sin(2 * np.pi * 440 * t)).astype("float32"), rate)
            self.assertEqual(len(result), 48000)
            frequencies = np.fft.rfftfreq(len(result), 1 / 16000)
            peak = frequencies[np.argmax(np.abs(np.fft.rfft(result)))]
            self.assertAlmostEqual(peak, 440, delta=0.4)
            self.assertGreater(np.sqrt(np.mean(result ** 2)), 0.27)

    def test_invalid_packets_and_samples_rejected(self):
        for data in (b"123", packet(0, 2001), packet(float("nan")), packet(-1), packet(0)[:-1]):
            with self.assertRaises(AudioError):
                unpack_packet(data, 8000)
        for pcm in (np.array([np.nan]), np.array([2.0]), np.zeros((2, 5)), np.zeros(8000 * 9)):
            with self.assertRaises(AudioError):
                resample_mono(pcm, 8000)

    def test_overlap_requires_multiple_matching_words_and_matching_time(self):
        prior = [word(" the", 7.1, 7.3), word(" report", 7.35, 7.7)]
        rows = [{"text": "the report is ready", "start_s": 0.1, "end_s": 1.4,
                 "words": [word(" the", .1, .3), word(" report", .35, .7), word(" is", .8, 1), word(" ready", 1.1, 1.4)]}]
        kept, _, count = reconcile_overlap(rows, prior, 7, 8, 8)
        self.assertEqual(count, 2)
        self.assertEqual(kept[0]["text"], "is ready")
        self.assertAlmostEqual(kept[0]["start_s"], 7.8)
        # Same repeated phrase spoken again at a different time must remain.
        prior = [word(" the", 6.1, 6.3), word(" report", 6.35, 6.7)]
        kept, _, count = reconcile_overlap(rows, prior, 7, 8, 8)
        self.assertEqual(count, 0)
        self.assertEqual(kept[0]["text"], "the report is ready")
        self.assertIn("retained", kept[0]["overlap_note"])
        # A single repeated word is intentionally retained, even at similar offsets.
        rows = [{"text": "very very good", "start_s": 0.1, "end_s": 1,
                 "words": [word(" very", .1, .3), word(" very", .35, .55), word(" good", .6, 1)]}]
        kept, _, count = reconcile_overlap(rows, [word(" very", 7.1, 7.3)], 7, 8, 8)
        self.assertEqual(count, 0)
        self.assertEqual(kept[0]["text"], "very very good")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def create(self, worker=None, current=lambda: True):
        meeting = Meeting()
        worker = worker or SimpleNamespace(run_pcm=AsyncMock())
        return LiveCapture(meeting, 8000, "live audio", Path("mock"), worker, current, AsyncMock())

    def feed_seconds(self, capture, seconds, start=0, epoch=0):
        for position in range(start * 8000, (start + seconds) * 8000, 2000):
            capture.feed(packet(position, epoch=epoch))

    async def test_queue_bounded_and_drops_are_visible(self):
        capture = self.create()
        self.feed_seconds(capture, 30)
        self.assertEqual(capture.queue.qsize(), 2)
        self.assertEqual(capture.report["dropped_windows"], 2)
        self.assertTrue(capture.report["intervals"])
        self.assertLessEqual(len(capture.buffer), 8000 * 8)
        capture.stop()
        self.assertEqual(capture.report["queue_windows"], 0)
        self.assertEqual(len(capture.buffer), 0)

    async def test_stop_invalidates_inflight_results_and_new_session(self):
        release = asyncio.Event()
        async def asr(*args):
            await release.wait()
            return {"segments": [{"text": "Mock late content", "start_s": 1, "end_s": 2, "words": []}],
                    "audio_duration_s": 8, "transcription_duration_s": 1, "model_initializations": 1}
        capture = self.create(SimpleNamespace(run_pcm=asr))
        self.feed_seconds(capture, 8)
        consumer = asyncio.create_task(capture.consume())
        await asyncio.sleep(0)
        self.assertTrue(capture.report["asr_processing"])
        capture.stop("New session")
        new_meeting = Meeting()
        release.set()
        await consumer
        self.assertEqual(capture.meeting.segments, [])
        self.assertEqual(new_meeting.segments, [])
        self.assertTrue(any(t["step"] == "discarded late result" for t in capture.report["trace"]))

    async def test_pause_for_speech_marks_gap_and_rejects_old_generation(self):
        capture = self.create()
        self.feed_seconds(capture, 7)
        capture.pause(56000, "speech output")
        self.assertTrue(capture.report["paused"])
        capture.feed(packet(56000, epoch=0))
        self.assertEqual(len(capture.buffer), 0)
        capture.resume(80000)
        self.assertEqual(capture.epoch, 2)
        capture.feed(packet(80000, epoch=1))
        self.assertEqual(len(capture.buffer), 0)
        capture.feed(packet(80000, epoch=2))
        self.assertEqual(len(capture.buffer), 2000)
        self.assertTrue(any(g["start_s"] <= 7 and g["end_s"] == 10 and "speech output" in g["reason"] for g in capture.report["intervals"]))
        capture.stop()

    async def test_missing_browser_audio_marks_gap_without_compressing_time(self):
        capture = self.create()
        capture.feed(packet(0))
        capture.feed(packet(8000))
        self.assertEqual(capture.buffer_start, 8000)
        self.assertEqual(capture.report["dropped_seconds"], .75)
        self.assertTrue(any(g["start_s"] == .25 and g["end_s"] == 1 for g in capture.report["intervals"]))
        capture.stop()


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.context = TestClient(app.app, base_url=app.ORIGIN)
        self.client = self.context.__enter__()
        page = self.client.get("/")
        self.token = re.search(r'name="aick-session-token" content="([^"]+)"', page.text)[1]
        self.headers = {"X-Session-Token": self.token, "Origin": app.ORIGIN}
        self.meeting = self.client.get("/api/meeting", headers=self.headers).json()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def hello(self, **fields):
        return dict(type="start", token=self.token, revision=self.meeting["revision"], meeting_id=self.meeting["id"], sample_rate=48000, mode="sample replay") | fields

    def test_host_origin_cookie_and_token_required(self):
        for headers in ({"Origin": "https://bad.invalid"}, {"Origin": app.ORIGIN, "Host": "bad.invalid"}, {}):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect("ws://127.0.0.1:8765/api/meeting/live", headers=headers):
                    self.fail("Untrusted handshake accepted")
        with self.client.websocket_connect("ws://127.0.0.1:8765/api/meeting/live", headers={"Origin": app.ORIGIN}) as socket:
            socket.send_json(self.hello(token="x" * 32))
            self.assertEqual(socket.receive_json()["type"], "error")

    def test_capture_stop_and_replacement(self):
        with self.client.websocket_connect("ws://127.0.0.1:8765/api/meeting/live", headers={"Origin": app.ORIGIN}) as socket:
            socket.send_json(self.hello())
            ready = socket.receive_json()
            self.assertEqual(ready["type"], "ready")
            self.assertEqual(ready["meeting"]["capture"]["source_label"], "Sample replay (test PCM)")
            # An authenticated new session invalidates the old capture before replacing it.
            response = self.client.post("/api/meeting/new", headers=self.headers, json={"revision": 0})
            self.assertEqual(response.status_code, 200)
            socket.send_bytes(packet(0, 4800))
            error = socket.receive_json()
            self.assertEqual(error["type"], "error")
            self.assertEqual(response.json()["segments"], [])
            self.assertIsNone(response.json()["capture"])


if __name__ == "__main__":
    unittest.main()

