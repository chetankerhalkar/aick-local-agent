"""Bounded local WAV generation; no default audio device or executable text."""
import asyncio
import json
import os
import secrets
import subprocess
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

MAX_SPEECH_CHARS = 1000
MAX_WAV_BYTES = 8 * 1024 * 1024
SYNTHESIS_TIMEOUT = 30
LEASE_SECONDS = 300  # abandoned playback expires by stopping capture, never resuming it


class SpeechError(ValueError):
    pass


def selected_text(meeting, run_id, answer_id):
    result = meeting.result
    if not result or result.get("run_id") != run_id or result["revision"] != meeting.revision:
        raise SpeechError("Answer changed or is stale. Run Ask agent and select a current answer.")
    matches = [item for item in result.get("answer", []) if item["id"] == answer_id]
    if len(matches) != 1:
        raise SpeechError("Select one visible answer claim.")
    text = matches[0]["text"]
    if not text.strip() or len(text.encode("utf-16-le")) // 2 > MAX_SPEECH_CHARS:
        raise SpeechError("Selected answer must contain 1-1,000 characters. Nothing was truncated.")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise SpeechError("Selected answer contains unsupported control characters.")
    return text


def synthesize(root, directory, text, voice):
    directory.mkdir(parents=True, exist_ok=False)
    text_path, wav_path = directory / "input.txt", directory / "reply.wav"
    text_path.write_text(text, encoding="utf-8")
    env = os.environ.copy()
    env["AICK_SPEECH_VOICE"] = voice or ""
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
             str(root / "synthesize_speech.ps1"), "-TextPath", str(text_path), "-WavPath", str(wav_path)],
            shell=False, capture_output=True, text=True, encoding="utf-8", timeout=SYNTHESIS_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env,
        )
        if result.returncode:
            raise SpeechError("Windows speech synthesis failed. Recheck the installed voice.")
        metadata = json.loads(result.stdout)
        if not 44 < wav_path.stat().st_size <= MAX_WAV_BYTES:
            raise SpeechError("Generated WAV exceeded the size limit or is empty.")
        with wave.open(str(wav_path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or not 0 < duration <= 90:
                raise SpeechError("Generated WAV format or duration was rejected (90 seconds maximum).")
        return {"voice": metadata["name"], "culture": metadata["culture"], "audio_duration_s": round(duration, 3)}
    except subprocess.TimeoutExpired as exc:
        raise SpeechError("Windows speech synthesis timed out after 30 seconds.") from exc
    finally:
        text_path.unlink(missing_ok=True)


def cleanup(directory):
    # Only generated filenames in a server-created session directory; no recursive deletion.
    for name in ("input.txt", "reply.wav"):
        (directory / name).unlink(missing_ok=True)
    if directory.exists():
        directory.rmdir()
    try:
        directory.parent.rmdir()
    except (FileNotFoundError, OSError):
        pass


class SpeechOutput:
    def __init__(self, root, captures):
        self.root, self.captures = root, captures
        self.active = None
        self.tasks = set()

    async def notify(self, capture, control):
        await capture.send({"type": "control", "control": control, "capture_id": capture.id,
                            "epoch": capture.epoch, "meeting": capture.meeting.view()})

    async def start(self, token, meeting, text, answer_id, run_id, voice):
        if self.active:
            raise SpeechError("A speech reply is already active. Finish or stop it before starting another.")
        job_id = "T-" + secrets.token_hex(12)
        report = {"id": job_id, "active": True, "phase": "preparing", "state": "processing",
                  "answer_id": answer_id, "run_id": run_id, "text": text,
                  "message": "Preparing local WAV. Listening paused; this interval is not transcribed.",
                  "started_at": datetime.now(timezone.utc).isoformat(), "ended_at": None,
                  "files_deleted": False, "paused_capture_ids": []}
        job = {"token": token, "meeting": meeting, "report": report, "cancelled": False,
               "directory": self.root / "work/speech-sessions" / meeting.id / job_id,
               "started": time.monotonic(), "held": [], "generating": True, "finished": False}
        self.active = job
        meeting.speech = report
        # Hold every active local capture, including capture running in another dashboard tab.
        for capture in list(self.captures.values()):
            if capture.active:
                owned = not capture.paused
                capture.speech_hold = True
                capture.pause(capture.position, "speech output")
                job["held"].append((capture, owned))
                report["paused_capture_ids"].append(capture.id)
        task = asyncio.create_task(self.generate(job, text, voice))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        for capture, _ in job["held"]:
            await self.notify(capture, "speech_pause")
        return report

    async def generate(self, job, text, voice):
        try:
            metadata = await asyncio.to_thread(synthesize, self.root, job["directory"], text, voice)
            job["report"].update(metadata, synthesis_duration_s=round(time.monotonic() - job["started"], 3))
            if not job["cancelled"]:
                job["report"].update(phase="ready", state="ready", message="WAV ready. Listening remains paused until playback ends or you click Stop speaking.")
        except Exception as exc:
            job["cancelled"] = True
            job["reason"] = "error"
            job["report"].update(state="error", message=str(exc) if isinstance(exc, SpeechError) else "Local speech generation failed.")
        finally:
            job["generating"] = False
            if job["cancelled"]:
                await self.finalize(job, job.get("reason", "stopped"))

    def owned(self, token, job_id):
        job = self.active
        if not job or job["token"] != token or job["report"]["id"] != job_id:
            raise SpeechError("Speech reply is no longer active in this session.")
        return job

    async def finish(self, token, job_id, reason):
        job = self.owned(token, job_id)
        job["cancelled"], job["reason"] = True, reason
        if job["generating"]:
            job["report"].update(phase="stopping", message="Stopping. Waiting for the bounded synthesis worker to release its files.")
        else:
            await self.finalize(job, reason)
        return job["report"]

    async def finalize(self, job, reason):
        if job["finished"]:
            return
        job["finished"] = True
        report = job["report"]
        try:
            cleanup(job["directory"])
            report["files_deleted"] = True
        except OSError:
            report.update(state="error", message="Playback stopped, but temporary file cleanup failed. Check the controlled speech session directory.")
        report.update(active=False, phase="error" if report["state"] == "error" else reason,
                      ended_at=datetime.now(timezone.utc).isoformat(), paused_duration_s=round(time.monotonic()-job["started"], 3))
        if report["state"] != "error":
            report.update(state="ready", message="Speech finished; temporary files deleted." if reason == "ended" else "Speech stopped; temporary files deleted.")
        for capture, owned in job["held"]:
            capture.speech_hold = False
            if capture.active:
                if reason not in {"ended", "stopped"}:
                    capture.stop("Speech interrupted or expired. Restart Listen manually")
                elif owned:
                    capture.resume(capture.position)
                await self.notify(capture, "speech_release")
        if self.active is job:
            self.active = None

    async def reap(self):
        while True:
            await asyncio.sleep(5)
            job = self.active
            if job and time.monotonic() - job["started"] > LEASE_SECONDS:
                await self.finish(job["token"], job["report"]["id"], "expired")

    async def shutdown(self):
        if self.active:
            await self.finish(self.active["token"], self.active["report"]["id"], "shutdown")
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
