"""AICK Local Agent, stage 6: presenter review and local reports with audit history."""

import asyncio
import html
import json
import os
import secrets
import subprocess
import time
import math
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import Field, ValidationError
from typing import Literal
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from meeting import Meeting, TypedInput, SampleInput, AskInput, RevisionInput, SAMPLES, parse_sample
from agent_workflow import run_workflow, WorkflowError
from audio_transcription import (LocalTranscriber, AudioError, MAX_AUDIO_BYTES,
                                 UPLOAD_TIMEOUT_SECONDS, cleanup_upload)
from live_audio import LiveCapture, MAX_CAPTURE_SECONDS
from meeting import StrictModel
from speech_output import SpeechOutput, SpeechError, selected_text
from meeting_exports import (ReviewInput, ExportInput, ReviewError, capacity, log, register_analysis,
                             review_action, report_data, runtime_identity, export_report, download_path)


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1:8765"
ORIGIN = f"http://{HOST}"
COOKIE = "aick_session"
SESSION_SECONDS = 8 * 60 * 60
STATES = ("not connected", "ready", "listening", "processing", "error")
SESSIONS: dict[str, float] = {}
MEETINGS: dict[str, Meeting] = {}
MODEL_LOCK = asyncio.Lock()
ASR_LOCK = asyncio.Lock()
TRANSCRIBER = LocalTranscriber()
AUDIO_TASKS: set[asyncio.Task] = set()
CAPTURES: dict[str, LiveCapture] = {}
SPEECH = SpeechOutput(ROOT, CAPTURES)
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; font-src 'self'; connect-src 'self' ws://127.0.0.1:8765; "
        "media-src 'self' blob:; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), display-capture=(self)",
    "Cache-Control": "no-store",
}

@asynccontextmanager
async def lifespan(application):
    reaper = asyncio.create_task(SPEECH.reap())
    yield
    reaper.cancel()
    with suppress(asyncio.CancelledError):
        await reaper
    await SPEECH.shutdown()
    for capture in list(CAPTURES.values()):
        capture.stop("Application shutdown")
    # Drain accepted work before shutdown; never delete a WAV while its worker reads it.
    if AUDIO_TASKS:
        await asyncio.gather(*AUDIO_TASKS, return_exceptions=True)
    await asyncio.to_thread(TRANSCRIBER.shutdown)


app = FastAPI(title="AICK Local Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
_health_lock = asyncio.Lock()
_health_cache: dict | None = None
_health_time = 0.0


def session_valid(token: str) -> bool:
    return bool(token) and SESSIONS.get(token, 0) > time.monotonic()


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    """Check the boundary before routing, including unsupported mutation routes."""
    hosts = request.headers.getlist("host")
    origins = request.headers.getlist("origin")
    unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
    # A link/bookmark may open the dashboard. Its token remains unreadable to
    # the referring page; framing, cross-origin API reads and mutations stay blocked.
    dashboard_navigation = (
        request.method == "GET" and request.url.path == "/"
        and request.headers.get("sec-fetch-mode") == "navigate"
        and request.headers.get("sec-fetch-dest") == "document"
    )
    problem = None
    if hosts != [HOST]:
        problem = "Unexpected Host. Use http://127.0.0.1:8765."
    elif origins and origins != [ORIGIN]:
        problem = "Unexpected Origin."
    elif unsafe and origins != [ORIGIN]:
        problem = "A same-origin Origin header is required."
    elif (request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}
          and not dashboard_navigation):
        problem = "Cross-site requests are not allowed."
    elif request.url.path.startswith("/api/") or unsafe:
        tokens = request.headers.getlist("x-session-token")
        token = tokens[0] if len(tokens) == 1 else ""
        cookie = request.cookies.get(COOKIE, "")
        if not session_valid(token) or not secrets.compare_digest(token.encode(), cookie.encode()):
            problem = "Session expired or missing. Reload the dashboard."

    response = (
        JSONResponse({"detail": problem}, status_code=403)
        if problem else await call_next(request)
    )
    response.headers.update(SECURITY_HEADERS)
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    now = time.monotonic()
    for old in [key for key, expiry in SESSIONS.items() if expiry <= now]:
        SESSIONS.pop(old, None)
        MEETINGS.pop(old, None)
    token = request.cookies.get(COOKIE, "")
    if not session_valid(token):
        if len(SESSIONS) >= 64:
            return HTMLResponse("Too many active sessions. Try again later.", status_code=503)
        token = secrets.token_urlsafe(32)
    SESSIONS[token] = now + SESSION_SECONDS
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    response = HTMLResponse(page.replace("__SESSION_TOKEN__", html.escape(token, quote=True)))
    response.set_cookie(
        COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/"
    )
    return response


@app.get("/static/styles.css")
async def stylesheet():
    return FileResponse(ROOT / "static" / "styles.css", media_type="text/css")


@app.get("/static/app.js")
async def javascript():
    return FileResponse(ROOT / "static" / "app.js", media_type="text/javascript")


@app.get("/static/favicon.svg")
async def favicon():
    return FileResponse(ROOT / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/static/capture.js")
async def capture_javascript():
    return FileResponse(ROOT / "static" / "capture.js", media_type="text/javascript")


@app.get("/static/pcm-worklet.js")
async def pcm_worklet():
    return FileResponse(ROOT / "static" / "pcm-worklet.js", media_type="text/javascript")


@app.get("/static/speech.js")
async def speech_javascript():
    return FileResponse(ROOT / "static" / "speech.js", media_type="text/javascript")


@app.get("/static/review.js")
async def review_javascript():
    return FileResponse(ROOT / "static" / "review.js", media_type="text/javascript")


def read_config() -> tuple[dict, Path, str | None]:
    configured = os.environ.get("MEETING_PREP_CONFIG")
    path = Path(configured).expanduser() if configured else ROOT.parent / "prepared-config.json"
    if not path.is_absolute():
        path = ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Prepared configuration must be a JSON object.")
        required = ("python", "llm_base_url", "asr_model_path")
        if any(not isinstance(data.get(key), str) or not data[key].strip() for key in required):
            raise ValueError("Prepared configuration needs python, llm_base_url and asr_model_path.")
        if data.get("app_host", "127.0.0.1") != "127.0.0.1" or data.get("app_port", 8765) != 8765:
            raise ValueError("The application address must be 127.0.0.1:8765.")
        if (data.get("asr_device", "cpu"), data.get("asr_compute_type", "int8"),
                data.get("asr_language", "en")) != ("cpu", "int8", "en"):
            raise ValueError("This build requires Whisper CPU / int8 / English.")
        if "speech_voice" in data and not isinstance(data["speech_voice"], str):
            raise ValueError("speech_voice must be an installed Windows voice name.")
        return data, path, None
    except FileNotFoundError:
        return {}, path, "Prepared configuration is missing. Set MEETING_PREP_CONFIG or add ../prepared-config.json."
    except (OSError, UnicodeError, ValueError) as exc:
        return {}, path, f"Cannot read prepared configuration: {exc}"


def status(state: str, detail: str, **extra) -> dict:
    if state not in STATES:
        raise ValueError("Unknown state")
    return {"state": state, "detail": detail, **extra}


def health_url(base: str) -> str:
    parsed = urlsplit(base)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") not in {"", "/v1"}):
        raise ValueError("llm_base_url must be a loopback HTTP URL, optionally ending in /v1.")
    _ = parsed.port  # Validate the port before making any connection.
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


async def check_llama(config: dict) -> dict:
    if not config:
        return status("not connected", "No configured local llama.cpp endpoint.")
    try:
        endpoint = health_url(config["llm_base_url"])
    except ValueError as exc:
        return status("error", str(exc))
    started = time.perf_counter()
    try:
        # Never consult proxy environment settings, redirect, or call a completion endpoint.
        async with httpx.AsyncClient(timeout=3.0, trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", endpoint) as response:
                code = response.status_code
                if code != 200:
                    return status("error", f"Health endpoint returned HTTP {code}.", endpoint=endpoint)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 8192:
                        return status("error", "Health response exceeded 8 KiB.", endpoint=endpoint)
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return status("error", 'Health response did not report status "ok".', endpoint=endpoint)
        elapsed = round((time.perf_counter() - started) * 1000)
        return status("ready", f"/health returned ok · {elapsed} ms", endpoint=endpoint,
                      model_alias=config.get("llm_model_alias", "Not configured"), latency_ms=elapsed)
    except (httpx.ConnectError, httpx.TimeoutException):
        return status("not connected", "Local server is unreachable or timed out. Recheck after starting llama.cpp.", endpoint=endpoint)
    except (httpx.HTTPError, ValueError, UnicodeError):
        return status("error", "Local server returned an invalid health response.", endpoint=endpoint)


def check_whisper(config: dict, config_path: Path) -> dict:
    if not config:
        return status("not connected", "No configured local Whisper directory.")
    path = Path(config["asr_model_path"])
    if not path.is_absolute():
        path = config_path.parent / path
    required = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")
    try:
        if not path.is_dir():
            return status("error", "Configured Whisper directory does not exist.", path=str(path))
        for name in required:
            with (path / name).open("rb") as handle:
                if not handle.read(1):
                    return status("error", f"Local model file is empty: {name}", path=str(path))
        loaded = TRANSCRIBER.loaded and TRANSCRIBER.model_path == path.resolve()
        return status("ready", "Local Whisper initialized; ready for uploaded WAVs." if loaded else
                      "4 model files readable. Model loads once on the first audio job.",
                      path=str(path), device="cpu", compute_type="int8", cpu_threads=4, loaded=loaded)
    except OSError:
        return status("error", "A required model file is missing or unreadable.", path=str(path))


def check_speech(config: dict) -> dict:
    if os.name != "nt":
        return status("error", "Windows System.Speech is required.")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    environment = os.environ.copy()
    environment["AICK_SPEECH_VOICE"] = config.get("speech_voice", "")
    try:
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "check_voice.ps1")],
            shell=False, capture_output=True, text=True, encoding="utf-8", timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW, env=environment,
        )
        if result.returncode:
            return status("error", "System.Speech could not select an enabled installed voice.")
        voice = json.loads(result.stdout.lstrip("\ufeff"))
        if not isinstance(voice, dict) or not isinstance(voice.get("name"), str) or not voice["name"]:
            raise ValueError("No voice")
        return status("ready", "Installed voice selected successfully. No speech generated.",
                      voice=voice["name"], culture=voice["culture"])
    except subprocess.TimeoutExpired:
        return status("error", "Windows voice check timed out.")
    except (OSError, ValueError, KeyError, UnicodeError):
        return status("error", "Windows speech probe could not return a usable installed voice.")


async def runtime_state():
    global _health_cache, _health_time
    async with _health_lock:
        if _health_cache is not None and time.monotonic() - _health_time < 4:
            return _health_cache
        config, config_path, config_error = read_config()
        llama, whisper, speech = await asyncio.gather(
            check_llama(config), asyncio.to_thread(check_whisper, config, config_path),
            asyncio.to_thread(check_speech, config),
        )
        resources = {"llama": llama, "whisper": whisper, "speech": speech}
        resource_states = [item["state"] for item in resources.values()]
        overall = ("error" if config_error or "error" in resource_states else
                   "ready" if all(item == "ready" for item in resource_states) else "not connected")
        _health_cache = {
            "stage": 6,
            "state": overall,
            "meeting_state": "not connected",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "configuration": {"state": "error" if config_error else "ready",
                              "detail": config_error or "Prepared configuration loaded.", "path": str(config_path)},
            "services": resources,
            "transcript": [],
            "actions": [],
            "decisions": [],
        }
        _health_time = time.monotonic()
        return _health_cache


def current_meeting(request: Request) -> Meeting:
    token = request.headers["x-session-token"]
    if token not in MEETINGS:
        MEETINGS[token] = Meeting()
    return MEETINGS[token]


def writable(meeting: Meeting, revision: int, allow_live=False):
    if meeting.speech and meeting.speech["active"]:
        raise HTTPException(409, "Finish or stop the selected speech reply before changing this session.")
    if meeting.busy:
        raise HTTPException(409, "A local workflow is processing. Wait before changing this session.")
    if meeting.revision != revision:
        raise HTTPException(409, "Session changed in another tab. Refresh before retrying.")
    if not allow_live and meeting.capture and meeting.capture["active"]:
        raise HTTPException(409, "Stop live capture before changing transcript sources or the session.")


@app.get("/api/state")
async def state(request: Request):
    health = await runtime_state()
    meeting = current_meeting(request)
    result = meeting.result or {}
    return {**health, "meeting": meeting.view(), "transcript": [s.model_dump() for s in meeting.segments],
            "actions": result.get("actions", []), "decisions": result.get("confirmed_decisions", [])}


@app.get("/api/meeting")
async def get_meeting(request: Request):
    return current_meeting(request).view()


@app.post("/api/meeting/review")
async def review_task(body: ReviewInput, request: Request):
    meeting = current_meeting(request)
    writable(meeting, body.revision, allow_live=True)
    try:
        review_action(meeting, body)
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    return meeting.view()


@app.get("/api/meeting/report-preview")
async def preview_report(request: Request):
    try:
        return report_data(current_meeting(request))
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/meeting/exports")
async def create_export(body: ExportInput, request: Request):
    meeting = current_meeting(request)
    writable(meeting, body.revision)  # stable export snapshot: stop capture first
    try:
        capacity(meeting, count=2, extra_bytes=10000)
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        export_report(ROOT, meeting, body)
    except (ReviewError, OSError) as exc:
        detail = str(exc) if isinstance(exc, ReviewError) else "Export failed. Check permissions on the fixed local exports directory."
        log(meeting, "export_failure", {"reason": detail})
        raise HTTPException(409 if isinstance(exc, ReviewError) else 500, detail) from exc
    return meeting.view()


@app.get("/api/meeting/exports/{export_id}/{kind}")
async def get_export(export_id: str, kind: str, request: Request):
    try:
        path = download_path(ROOT, current_meeting(request), export_id, kind)
        if not path.is_file(): raise ReviewError("Export file is no longer present.")
    except ReviewError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="text/markdown" if kind == "md" else "text/csv", filename=path.name)


class SpeechStart(RevisionInput):
    run_id: str = Field(min_length=1, max_length=80)
    answer_id: str = Field(min_length=1, max_length=40)
    approved: Literal[True]


class SpeechControl(StrictModel):
    job_id: str = Field(pattern=r"^T-[a-f0-9]{24}$")
    reason: Literal["ended", "stopped"]


@app.post("/api/meeting/speech", status_code=202)
async def start_speech(body: SpeechStart, request: Request):
    meeting = current_meeting(request)
    writable(meeting, body.revision, allow_live=True)
    config, _, error = read_config()
    if error:
        raise HTTPException(503, error)
    try:
        text = selected_text(meeting, body.run_id, body.answer_id)
        await SPEECH.start(request.headers["x-session-token"], meeting, text,
                           body.answer_id, body.run_id, config.get("speech_voice"))
    except SpeechError as exc:
        raise HTTPException(409, str(exc)) from exc
    return meeting.view()


@app.get("/api/meeting/speech/{job_id}/wav")
async def speech_wav(job_id: str, request: Request):
    try:
        job = SPEECH.owned(request.headers["x-session-token"], job_id)
        if job["cancelled"] or job["report"]["phase"] not in {"ready", "playing"}:
            raise SpeechError("Speech WAV is not ready or was stopped.")
        # Read and close before returning; cleanup never races an open Windows file handle.
        data = (job["directory"] / "reply.wav").read_bytes()
    except (SpeechError, OSError) as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, SpeechError) else "Speech WAV unavailable.") from exc
    return Response(data, media_type="audio/wav")


@app.post("/api/meeting/speech/{job_id}/play")
async def speech_play(job_id: str, request: Request):
    try:
        job = SPEECH.owned(request.headers["x-session-token"], job_id)
        if job["cancelled"] or job["report"]["phase"] not in {"ready", "playing"}:
            raise SpeechError("Speech reply is not ready or has expired. Select and approve it again.")
        job["report"].update(phase="playing", message="Browser playback requested. Listening is paused; this interval is not transcribed.")
    except SpeechError as exc:
        raise HTTPException(409, str(exc)) from exc
    return current_meeting(request).view()


@app.post("/api/meeting/speech/finish")
async def finish_speech(body: SpeechControl, request: Request):
    meeting = current_meeting(request)
    if meeting.speech and meeting.speech["id"] == body.job_id and not meeting.speech["active"]:
        return meeting.view()  # idempotent end/pagehide/Stop race
    try:
        await SPEECH.finish(request.headers["x-session-token"], body.job_id, body.reason)
    except SpeechError as exc:
        raise HTTPException(409, str(exc)) from exc
    return meeting.view()


@app.post("/api/meeting/new")
async def new_meeting(body: RevisionInput, request: Request):
    current = current_meeting(request)
    writable(current, body.revision, allow_live=True)
    capture = CAPTURES.get(request.headers["x-session-token"])
    if capture:
        capture.stop("Session replaced")
    meeting = Meeting()
    # Revision never goes backwards within a browser session, including replacements.
    meeting.revision = body.revision + 1
    MEETINGS[request.headers["x-session-token"]] = meeting
    return meeting.view()


@app.post("/api/meeting/segments")
async def add_segment(body: TypedInput, request: Request):
    meeting = current_meeting(request)
    writable(meeting, body.revision)
    try:
        meeting.add(text=body.text, start_s=body.start_s, end_s=body.end_s,
                    speaker=body.speaker or None, source_type="typed", source_label="Typed transcript")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return meeting.view()


@app.post("/api/meeting/sample")
async def import_sample(body: SampleInput, request: Request):
    meeting = current_meeting(request)
    writable(meeting, body.revision)
    _, config_path, error = read_config()
    if error:
        raise HTTPException(503, error)
    sample_dir = ROOT.parent / "samples"
    if not sample_dir.is_dir():
        sample_dir = config_path.parent / "samples"
    try:
        path = sample_dir / body.sample
        if path.resolve().parent != sample_dir.resolve() or path.stat().st_size > 64_000:
            raise ValueError("Sample must be an allowlisted file in the prepared samples directory under 64 KB.")
        replacement = parse_sample(path.read_text(encoding="utf-8-sig"), body.sample)
        replacement.revision = meeting.revision + 1
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(422, f"Sample import failed: {exc}") from exc
    MEETINGS[request.headers["x-session-token"]] = replacement
    return replacement.view()


async def analyze(request: Request, revision: int, question: str, mode: str):
    meeting = current_meeting(request)
    writable(meeting, revision, allow_live=True)
    if not meeting.segments:
        raise HTTPException(409, "Add typed text, import a sample, or transcribe an uploaded WAV before analysis.")
    if MODEL_LOCK.locked():
        raise HTTPException(409, "The local model is processing another request. Try again when it finishes.")
    config, _, error = read_config()
    if error:
        raise HTTPException(503, error)
    try:
        capacity(meeting, count=100, extra_bytes=512000)
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    meeting.runtime_identity = runtime_identity(config)
    log(meeting, "analysis_requested", {"mode": mode, "question": question})
    try:
        endpoint = urlsplit(health_url(config["llm_base_url"]))
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    root = urlunsplit((endpoint.scheme, endpoint.netloc, "", "", ""))
    meeting.busy = True
    meeting.last_error = None
    # A button analyzes a stable snapshot while live segments may continue arriving.
    snapshot = Meeting(id=meeting.id, revision=meeting.revision, segments=list(meeting.segments))
    try:
        async with MODEL_LOCK:
            result = await run_workflow(snapshot, question, mode, config, root)
        if meeting.result:
            meeting.history = (meeting.history + [meeting.result])[-5:]
        register_analysis(meeting, result, config)
        meeting.result = result
    except (TimeoutError, httpx.TimeoutException):
        meeting.last_error = "Local model workflow timed out. No new result was accepted. Previous results, if any, are unchanged."
        snapshot.trace.append({"step": "error", "detail": meeting.last_error})
        raise HTTPException(504, meeting.last_error)
    except (httpx.HTTPError, WorkflowError) as exc:
        meeting.last_error = str(exc) if isinstance(exc, WorkflowError) else "Could not reach the configured local model."
        snapshot.trace.append({"step": "error", "detail": meeting.last_error})
        raise HTTPException(502, meeting.last_error) from exc
    finally:
        if meeting.last_error:
            log(meeting, "validation_failure", {"reason": meeting.last_error,
                "validation_trace": [e for e in snapshot.trace if e["step"] in {"format_validation_failed", "semantic_review_unavailable"}]})
        meeting.trace = snapshot.trace
        meeting.busy = False
    return meeting.view()


@app.post("/api/meeting/ask")
async def ask_agent(body: AskInput, request: Request):
    return await analyze(request, body.revision, body.question, "ask")


@app.post("/api/meeting/board")
async def update_board(body: RevisionInput, request: Request):
    return await analyze(request, body.revision,
                         "What are the current confirmed decisions, proposed or confirmed actions, superseded commitments, and unresolved questions? Identify missing owners/deadlines and unresolved contradictions.", "board")


async def process_audio(token, meeting, revision, job, path, model_path):
    worker = asyncio.create_task(TRANSCRIBER.run(path, model_path))
    try:
        result = await asyncio.shield(worker)
        if MEETINGS.get(token) is not meeting or meeting.revision != revision:
            raise AudioError("Session changed before transcription completed. No segments appended.")
        rows = result.pop("segments")
        meeting.append_audio(rows, job["id"], job["offset_s"])
        job.update(result, state="ready", segments_added=len(rows))
    except asyncio.CancelledError:
        # A Python thread cannot be cancelled safely. Retain admission until it exits.
        try:
            await worker
        except Exception:
            pass
        job.update(state="error", message="Transcription interrupted. No segments appended.")
        meeting.last_error = job["message"]
        raise
    except Exception as exc:
        message = str(exc) if isinstance(exc, (AudioError, ValueError)) else "Local transcription failed. No segments appended. Check the prepared Whisper runtime."
        job.update(state="error", message=message)
        meeting.last_error = message
    finally:
        try:
            job["audio_deleted"] = await asyncio.to_thread(cleanup_upload, path)
        except OSError:
            job.update(state="error", audio_deleted=False, message="Temporary audio cleanup failed. Restart after checking local file permissions.")
            meeting.last_error = job["message"]
        job["completed_at"] = datetime.now(timezone.utc).isoformat()
        meeting.busy = False
        ASR_LOCK.release()


@app.post("/api/meeting/audio", status_code=202)
async def upload_audio(request: Request, revision: int = Query(ge=0),
                       offset_s: float = Query(default=0, ge=0, le=86400, allow_inf_nan=False)):
    meeting = current_meeting(request)
    writable(meeting, revision)
    if ASR_LOCK.locked():
        raise HTTPException(409, "One WAV is already uploading or transcribing. Wait for it to finish; uploads are not queued.")
    if request.headers.get("content-type", "").split(";", 1)[0].lower() not in {"audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"}:
        raise HTTPException(415, "Upload a WAV file as an audio/wav request body.")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError as exc:
            raise HTTPException(400, "Invalid upload length.") from exc
        if size < 0 or size > MAX_AUDIO_BYTES:
            raise HTTPException(413, "WAV exceeds the 25 MiB file limit.")
    config, config_path, error = read_config()
    if error:
        raise HTTPException(503, error)
    model_path = Path(config["asr_model_path"])
    if not model_path.is_absolute():
        model_path = config_path.parent / model_path
    await ASR_LOCK.acquire()
    meeting.busy = True
    meeting.last_error = None
    job = {"id": "U-" + secrets.token_hex(8), "state": "processing", "source_label": "Uploaded audio",
           "offset_s": offset_s, "segments_added": 0, "audio_deleted": False, "message": "Receiving WAV upload."}
    meeting.audio_jobs = (meeting.audio_jobs + [job])[-10:]
    base = ROOT / "work" / "audio-sessions"
    path = base / meeting.id / job["id"] / "upload.wav"
    transferred = False
    try:
        if not path.resolve().is_relative_to(base.resolve()):
            raise HTTPException(400, "Invalid controlled upload directory.")
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=False)
        handle = await asyncio.to_thread(path.open, "xb")
        received = 0
        try:
            async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > MAX_AUDIO_BYTES:
                        raise HTTPException(413, "WAV exceeds the 25 MiB file limit.")
                    await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)
        if not received:
            raise HTTPException(422, "WAV upload is empty.")
        job.update(bytes=received, message="Processing uploaded WAV locally; this may include first-time model initialization.")
        task = asyncio.create_task(process_audio(request.headers["x-session-token"], meeting, revision, job, path, model_path))
        AUDIO_TASKS.add(task)
        task.add_done_callback(AUDIO_TASKS.discard)
        transferred = True
        return meeting.view()
    except TimeoutError as exc:
        raise HTTPException(408, "WAV upload exceeded the 30 second upload limit.") from exc
    finally:
        if not transferred:
            try:
                job["audio_deleted"] = await asyncio.to_thread(cleanup_upload, path)
            finally:
                job.update(state="error", message="Upload was rejected or interrupted. No segments appended.")
                meeting.last_error = job["message"]
                meeting.busy = False
                ASR_LOCK.release()


class CaptureStart(StrictModel):
    type: Literal["start"]
    token: str = Field(min_length=20, max_length=100)
    revision: int = Field(ge=0)
    meeting_id: str = Field(pattern=r"^M-[a-f0-9]{10}$")
    sample_rate: int = Field(ge=8000, le=96000, strict=True)
    mode: Literal["live audio", "sample replay"] = "live audio"


class CaptureStop(StrictModel):
    capture_id: str = Field(pattern=r"^C-[a-f0-9]{16}$")


@app.post("/api/meeting/capture/stop")
async def stop_capture(body: CaptureStop, request: Request):
    capture = CAPTURES.get(request.headers["x-session-token"])
    if capture and capture.id == body.capture_id:
        capture.stop("Stopped by user")
    return current_meeting(request).view()


@app.get("/api/samples/speech-smoke.wav")
async def replay_sample():
    _, config_path, error = read_config()
    if error:
        raise HTTPException(503, error)
    directory = ROOT.parent / "samples"
    if not directory.is_dir():
        directory = config_path.parent / "samples"
    path = directory / "speech_smoke_test.wav"
    if not path.is_file() or path.stat().st_size > MAX_AUDIO_BYTES:
        raise HTTPException(404, "Prepared speech smoke WAV unavailable.")
    return FileResponse(path, media_type="audio/wav")


async def consume_capture(token, capture):
    try:
        await capture.consume()
    finally:
        capture.stop("Capture ended")
        if CAPTURES.get(token) is capture:
            CAPTURES.pop(token, None)
        ASR_LOCK.release()


@app.websocket("/api/meeting/live")
async def live_socket(socket: WebSocket):
    # HTTP middleware does not cover WebSockets. Check the complete boundary here.
    if (socket.headers.getlist("host") != [HOST] or socket.headers.getlist("origin") != [ORIGIN]
            or socket.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}
            or not session_valid(socket.cookies.get(COOKIE, ""))):
        await socket.close(code=1008)
        return
    await socket.accept()
    capture = None
    send_lock = asyncio.Lock()

    async def send(payload):
        try:
            async with send_lock:
                async with asyncio.timeout(3):
                    await socket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect, TimeoutError):
            if capture:
                capture.stop("Local connection ended")

    try:
        first = await asyncio.wait_for(socket.receive_text(), timeout=5)
        if len(first) > 4096:
            raise AudioError("Capture authentication message is too large.")
        hello = CaptureStart.model_validate_json(first)
        token = hello.token
        if not session_valid(token) or not secrets.compare_digest(token.encode(), socket.cookies.get(COOKIE, "").encode()):
            raise AudioError("Capture session token is missing, expired or invalid.")
        meeting = MEETINGS.get(token)
        if not meeting or meeting.id != hello.meeting_id:
            raise AudioError("Meeting session changed before capture began.")
        writable(meeting, hello.revision)
        if SPEECH.active:
            raise AudioError("Speech output is active. Finish or stop speaking before starting capture.")
        if ASR_LOCK.locked():
            raise AudioError("The single ASR worker is busy. Wait for the previous upload or capture to finish.")
        config, config_path, error = read_config()
        if error:
            raise AudioError(error)
        model_path = Path(config["asr_model_path"])
        if not model_path.is_absolute():
            model_path = config_path.parent / model_path
        await ASR_LOCK.acquire()
        capture = LiveCapture(meeting, hello.sample_rate, hello.mode, model_path, TRANSCRIBER,
                              lambda: MEETINGS.get(token) is meeting and session_valid(token), send)
        CAPTURES[token] = capture
        worker = asyncio.create_task(consume_capture(token, capture))
        AUDIO_TASKS.add(worker)
        worker.add_done_callback(AUDIO_TASKS.discard)
        await send({"type": "ready", "capture_id": capture.id, "epoch": capture.epoch, "meeting": meeting.view()})
        last_report = time.monotonic()
        while capture.active:
            message = await asyncio.wait_for(socket.receive(), timeout=20)
            if message["type"] == "websocket.disconnect":
                break
            if not session_valid(token) or MEETINGS.get(token) is not meeting:
                raise AudioError("Meeting session expired or changed.")
            if message.get("bytes") is not None:
                capture.feed(message["bytes"])
            else:
                raw = message.get("text", "")
                if len(raw) > 1024:
                    raise AudioError("Capture control message is too large.")
                control = json.loads(raw)
                if not isinstance(control, dict) or set(control) - {"type", "position", "reason"}:
                    raise AudioError("Invalid capture control.")
                kind = control.get("type")
                position = control.get("position", capture.position)
                if not isinstance(position, int) or isinstance(position, bool) or not 0 <= position <= capture.rate * MAX_CAPTURE_SECONDS:
                    raise AudioError("Invalid capture position.")
                if kind == "stop":
                    capture.position = max(capture.position, position)
                    capture.stop("Stopped by user")
                elif kind == "pause":
                    reason = control.get("reason", "user pause")
                    if reason not in {"user pause", "speech output", "muted capture"}:
                        raise AudioError("Unknown pause reason.")
                    capture.pause(position, reason)
                elif kind == "resume":
                    capture.resume(position)
                elif kind == "ping":
                    if capture.paused:
                        capture.position = max(capture.position, position)
                else:
                    raise AudioError("Unsupported capture control.")
                await send({"type": "control", "control": kind, "capture_id": capture.id,
                            "epoch": capture.epoch, "meeting": meeting.view()})
            if time.monotonic() - last_report >= 1:
                await send({"type": "status", "capture_id": capture.id, "meeting": meeting.view()})
                last_report = time.monotonic()
    except (AudioError, ValidationError, ValueError, HTTPException, TimeoutError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc) if isinstance(exc, AudioError) else "Invalid or timed-out local capture connection. Stop and retry."
        if capture:
            capture.stop(detail)
            capture.report["state"] = "error"
        await send({"type": "error", "message": detail})
    except WebSocketDisconnect:
        pass
    finally:
        if capture:
            capture.stop("Capture connection ended")
        try:
            await socket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
