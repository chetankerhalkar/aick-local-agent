"""Presenter review, append-only audit events, and bounded local text exports."""
import copy
import csv
import html
import io
import json
import os
import platform
import re
import secrets
import unicodedata
from datetime import date, datetime, timezone
from importlib.metadata import version
from pathlib import Path

from pydantic import Field, model_validator
from typing import Literal
from meeting import RevisionInput, StrictModel

MAX_EVENTS = 1500
MAX_AUDIT_BYTES = 8 * 1024 * 1024
MAX_EXPORTS = 20
FIELDS = ("task", "owner", "due_date", "due_text")


class ReviewError(ValueError):
    pass


class ReviewInput(RevisionInput):
    review_version: int = Field(ge=0)
    run_id: str = Field(min_length=1, max_length=80)
    action_id: str = Field(pattern=r"^A[1-9][0-9]?$")
    operation: Literal["approve", "edit", "reject"]
    task: str | None = Field(default=None, min_length=1, max_length=600)
    owner: str | None = Field(default=None, max_length=80)
    due_date: str | None = Field(default=None, max_length=10)
    due_text: str | None = Field(default=None, max_length=120)
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def explicit_edit(self):
        if self.operation == "edit":
            if not self.task or not set(FIELDS).issubset(self.model_fields_set):
                raise ValueError("An edit must supply task, owner, due_date and due_text; use null for unresolved fields.")
            if self.due_date:
                if date.fromisoformat(self.due_date).isoformat() != self.due_date:
                    raise ValueError("Use an explicit YYYY-MM-DD date or leave it unresolved.")
        elif set(FIELDS).intersection(self.model_fields_set):
            raise ValueError("Save edits separately before approving or rejecting.")
        return self


class ExportInput(RevisionInput):
    review_version: int = Field(ge=0)
    title: str = Field(default="Meeting report", min_length=1, max_length=80)


def now():
    return datetime.now(timezone.utc).isoformat()


def source_modes(meeting):
    return sorted({s.source_type for s in meeting.segments}) or ["empty"]


def runtime_identity(config, result=None):
    return {"application_stage": 6, "python": platform.python_version(), "platform": platform.system(),
            "packages": {name: version(name) for name in ("fastapi", "faster-whisper", "ctranslate2", "av")},
            "configured_llm_endpoint": config.get("llm_base_url"), "configured_model_alias": config.get("llm_model_alias"),
            "configured_model_file": Path(config.get("llm_model_path", "")).name,
            "server_reported_models": sorted({str(e["reported_model"]) for e in (result or {}).get("trace", []) if e.get("reported_model")}),
            "asr_model_directory": Path(config.get("asr_model_path", "")).name,
            "asr_device": "cpu", "asr_compute_type": "int8", "raw_audio_persisted": False}


def event(meeting, kind, data):
    return {"sequence": len(meeting.action_log) + 1, "at": now(), "event": kind,
            "meeting_id": meeting.id, "transcript_revision": meeting.revision,
            "source_modes": source_modes(meeting), "runtime": copy.deepcopy(meeting.runtime_identity),
            "data": copy.deepcopy(data)}


def capacity(meeting, count=1, extra_bytes=0):
    if len(meeting.action_log) + count > MAX_EVENTS or len(json.dumps(meeting.action_log).encode()) + extra_bytes > MAX_AUDIT_BYTES:
        raise ReviewError("Audit capacity reached. Export this session before starting a new one; history is never silently dropped.")


def log(meeting, kind, data):
    entry = event(meeting, kind, data)
    capacity(meeting, extra_bytes=len(json.dumps(entry).encode()))
    meeting.action_log.append(entry)
    return entry


def register_analysis(meeting, result, config):
    meeting.runtime_identity = runtime_identity(config, result)
    meeting.task_reviews = {}
    meeting.review_version += 1
    log(meeting, "analysis_proposals", {"run_id": result["run_id"], "revision": result["revision"],
        "budget": result["budget"], "previous_approvals": "Not carried forward; this run requires fresh review.",
        "original_results": {key: result.get(key, []) for key in ("actions", "confirmed_decisions", "review_decisions", "unresolved_questions", "answer")}})
    for action in result["actions"]:
        meeting.task_reviews[action["id"]] = {"run_id": result["run_id"], "status": "pending",
            "current": {key: action.get(key) for key in FIELDS}, "edited": False, "note": ""}
    for item in result.get("actions", []) + result.get("review_decisions", []) + result.get("answer", []) + result.get("unresolved_questions", []):
        if item.get("issues"):
            log(meeting, "validation_failure_or_unresolved_field", {"run_id": result["run_id"], "id": item["id"],
                "citation_check": item.get("citation_check"), "semantic_check": item.get("semantic_check"), "issues": item["issues"]})
    for trace in result.get("trace", []):
        if trace["step"] in {"format_validation_failed", "semantic_review_unavailable", "reconcile_correction"}:
            log(meeting, "correction" if trace["step"] == "reconcile_correction" else "validation_failure", {"run_id": result["run_id"], "trace": trace})


def current_result(meeting):
    if not meeting.result or meeting.result["revision"] != meeting.revision:
        raise ReviewError("Run Ask agent or Update action board for the current transcript before reviewing or exporting.")
    return meeting.result


def valid_refs(meeting, refs):
    segments = {s.id: s.text for s in meeting.segments}
    return bool(refs) and all(r.get("segment_id") in segments and isinstance(r.get("quote"), str)
        and r["quote"].strip() and r["quote"] in segments[r["segment_id"]] for r in refs)


def review_action(meeting, body):
    result = current_result(meeting)
    if body.run_id != result["run_id"] or body.review_version != meeting.review_version:
        raise ReviewError("Review changed in another tab or analysis. Refresh before retrying.")
    original = next((item for item in result["actions"] if item["id"] == body.action_id), None)
    previous = meeting.task_reviews.get(body.action_id)
    if not original or not previous or previous["run_id"] != body.run_id:
        raise ReviewError("Action is not a proposal in the current analysis.")
    updated = copy.deepcopy(previous)
    if body.operation == "approve":
        if original["status"] == "superseded" or not valid_refs(meeting, original["evidence"]):
            log(meeting, "validation_failure", {"action_id": body.action_id, "operation": "approve", "reason": "Superseded action or invalid original citations."})
            raise ReviewError("Cannot approve a superseded action or one with invalid source citations.")
        updated["status"] = "approved"
    elif body.operation == "reject":
        updated["status"] = "rejected"
    else:
        updated.update(current={key: getattr(body, key) or None for key in FIELDS}, edited=True, status="pending")
    updated["note"] = body.note
    log(meeting, {"approve": "approval", "reject": "rejection", "edit": "presenter_correction"}[body.operation],
        {"run_id": body.run_id, "action_id": body.action_id, "original_proposal": original,
         "before": previous, "after": updated, "provenance": "Presenter edits are not transcript quotations or inferred commitments."})
    meeting.task_reviews[body.action_id] = updated
    meeting.review_version += 1


def report_data(meeting):
    result = current_result(meeting)
    actions = []
    for original in result["actions"]:
        review = meeting.task_reviews.get(original["id"], {})
        if review.get("status") != "approved" or review.get("run_id") != result["run_id"]:
            continue
        if original["status"] == "superseded" or not valid_refs(meeting, original["evidence"]):
            raise ReviewError("An approved task has invalid citations or is superseded. Review it again before exporting.")
        actions.append({"id": original["id"], **review["current"], "status": "presenter approved", "edited": review["edited"],
                        "evidence": copy.deepcopy(original["evidence"]), "original_status": original["status"],
                        "support": original.get("semantic_check", "not checked"), "issues": original.get("issues", [])})
    decisions = [copy.deepcopy(item) for item in result["confirmed_decisions"]
        if item.get("citation_check") == "matched" and item.get("semantic_check") == "supported"
        and not item.get("issues") and valid_refs(meeting, item["evidence"])]
    questions = [copy.deepcopy(item) for item in result["unresolved_questions"] if valid_refs(meeting, item["evidence"])]
    return {"approved_tasks": actions, "confirmed_decisions": decisions, "open_questions": questions,
            "excluded_decisions": len(result["confirmed_decisions"]) + len(result.get("review_decisions", [])) - len(decisions),
            "excluded_questions": len(result["unresolved_questions"]) - len(questions), "budget": result["budget"],
            "draft_follow_up": draft_follow_up(actions, decisions)}


def draft_follow_up(actions, decisions):
    # A deterministic draft cannot introduce extra model-generated facts or recipients.
    lines = ["Draft follow-up — review before use. Nothing has been sent."]
    if not actions and not decisions:
        return lines + ["No approved actions or supported decisions are available."]
    for decision in decisions:
        lines.append(f"Decision [{decision['id']}]: {decision['text']}")
    for task in actions:
        due = " / ".join(value for value in (task["due_date"], task["due_text"]) if value) or "unresolved"
        lines.append(f"Approved action [{task['id']}]: {task['task']} Owner: {task['owner'] or 'unresolved'}. Due: {due}."
                     + (" Presenter-edited; original transcript evidence is retained separately." if task["edited"] else ""))
    return lines


def safe_slug(title):
    name = re.sub(r"[^a-zA-Z0-9]+", "-", unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()).strip("-").lower()[:48]
    return "meeting-" + (name or "report")  # prefix also prevents Windows device names


def csv_cell(value):
    value = "" if value is None else str(value)
    probe = unicodedata.normalize("NFKC", value)
    probe = "".join(c for c in probe if unicodedata.category(c) != "Cf").lstrip()
    while probe and (probe[0].isspace() or unicodedata.category(probe[0]).startswith("C")):
        probe = probe[1:]
    if value.startswith(("\t", "\r", "\n")) or probe.startswith(("=", "+", "-", "@")):
        return "Text: " + value
    return value


def md(value):
    value = html.escape(str(value), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value).replace("\r", "").replace("\n", "  \n")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def render_report(meeting, title, data, audit, created):
    lines = [f"# {md(title)}", "", f"Session: {meeting.id} · revision {meeting.revision} · review {meeting.review_version}",
             f"Exported: {created}", f"Source modes: {md(', '.join(source_modes(meeting)))}", "",
             "Sample transcript, sample replay, uploaded audio and typed sources are not live meeting capture.",
             "This is a local reviewable report. No email, calendar event or external task was created.",
             "Presenter approval is separate from model confirmation. Matching quotes and model support can still be wrong.", "",
             f"Context: {md(data['budget']['notice'])}", f"Omitted segment IDs: {md(', '.join(data['budget'].get('omitted_ids', [])) or 'none')}", "",
             "## Model and runtime identity", "", md(json_text(meeting.runtime_identity)), ""]
    rows = []
    def row(kind, item_id="", text="", owner="", due="", status="", refs=None, provenance=""):
        rows.append([kind, meeting.id, meeting.revision, created, ", ".join(source_modes(meeting)), item_id, text,
                     owner, due, status, json_text(refs or []), provenance])
    row("runtime_identity", text=json_text(meeting.runtime_identity))
    row("context_scope", text=json_text(data["budget"]))
    for heading, key, kind in (("Confirmed decisions", "confirmed_decisions", "confirmed_decision"),
                                ("Approved tasks", "approved_tasks", "approved_task"), ("Open questions", "open_questions", "open_question")):
        lines += [f"## {heading}", ""]
        if not data[key]: lines += ["None.", ""]
        for item in data[key]:
            text = item.get("task", item.get("text", ""))
            due = " / ".join(v for v in (item.get("due_date"), item.get("due_text")) if v) or "unresolved"
            provenance = "Presenter edit; citations support the original proposal, not necessarily the edited task." if item.get("edited") else "Transcript-based proposal; presenter approval required for actions."
            lines += [f"### {item['id']}", "", md(text)]
            if kind == "approved_task":
                lines += [f"Owner: {md(item.get('owner') or 'unresolved')} · Due: {md(due)}", md(provenance),
                          f"Original support: {md(item['support'])}. Review flags: {md('; '.join(item['issues']) or 'none')}."]
            for ref in item["evidence"]:
                lines += [f"- [{ref['segment_id']}](#transcript-{ref['segment_id'].lower()}): “{md(ref['quote'])}”"]
            lines += [""]
            row(kind, item["id"], text, item.get("owner") or ("unresolved" if kind == "approved_task" else ""),
                due if kind == "approved_task" else "", item.get("status", "supported" if kind == "confirmed_decision" else "unresolved"), item["evidence"], provenance)
    lines += [f"Excluded unsupported/review decisions: {data['excluded_decisions']}. Questions with invalid citations: {data['excluded_questions']}. See audit history.", "",
              "## Draft follow-up", ""]
    for line in data["draft_follow_up"]:
        lines += [md(line), ""]
        row("draft_follow_up", text=line)
    lines += ["## Transcript and citation targets", "", "Original source statements follow; these are not the approved-task list.", ""]
    for segment in meeting.segments:
        lines += [f"### Transcript {segment.id}", "", f"{md(segment.source_label)} · {segment.start_s}–{segment.end_s if segment.end_s is not None else 'unknown'} seconds · Speaker: {md(segment.speaker or 'unassigned')}", md(segment.text), ""]
        row("transcript", segment.id, segment.text, provenance=json_text(segment.model_dump()))
    lines += ["## Audit history — not approved tasks", "", "Rejected, pending, superseded and prior-run proposals are retained only as history. Original evidence and every presenter edit remain here.", ""]
    for entry in audit:
        lines += [f"### Audit {entry['sequence']} · {md(entry['event'])}", "", md(json_text(entry)), ""]
        row("audit_history", str(entry["sequence"]), json_text(entry), status=entry["event"], provenance="HISTORY ONLY; not an approved task")
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(["record_type", "meeting_id", "transcript_revision", "exported_at", "source_modes", "item_id", "text", "owner", "due", "status", "citations_json", "provenance"])
    for values in rows: writer.writerow([csv_cell(value) for value in values])
    return "\n".join(lines), output.getvalue()


def export_directory(root, meeting_id):
    if not re.fullmatch(r"M-[0-9a-f]{10}", meeting_id): raise ReviewError("Invalid server session ID.")
    root = root.resolve()
    base, directory = root / "exports", root / "exports" / meeting_id
    # Reject redirected export roots/session directories (including Windows junctions).
    for path in (base, directory):
        if path.resolve() != path or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ReviewError("Export directory must not be a symbolic link or junction.")
    return directory


def export_report(root, meeting, body):
    if body.review_version != meeting.review_version: raise ReviewError("Review changed. Refresh the report preview before exporting.")
    if len(meeting.exports) >= MAX_EXPORTS: raise ReviewError("Session export limit reached (20). Start a new session for more exports.")
    data = report_data(meeting)
    directory = export_directory(root, meeting.id)
    export_id = "E-" + secrets.token_hex(12)
    created = now()
    basename = safe_slug(body.title) + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + export_id
    filenames = {kind: basename + "." + kind for kind in ("md", "csv")}
    entry = event(meeting, "export", {"export_id": export_id, "filenames": filenames, "review_version": meeting.review_version,
        "approved_action_ids": [item["id"] for item in data["approved_tasks"]], "confirmed_decision_ids": [item["id"] for item in data["confirmed_decisions"]]})
    capacity(meeting, extra_bytes=len(json.dumps(entry).encode()))
    markdown, csv_text = render_report(meeting, body.title, data, meeting.action_log + [entry], created)
    directory.mkdir(parents=True, exist_ok=True)
    created_paths = []
    try:
        for kind, content in (("md", markdown), ("csv", csv_text)):
            checked = export_directory(root, meeting.id)
            path = checked / filenames[kind]
            if path.resolve().parent != checked: raise ReviewError("Export target escaped the session directory.")
            with path.open("x", encoding="utf-8-sig" if kind == "csv" else "utf-8", newline="") as stream:
                created_paths.append(path)
                stream.write(content)
    except Exception:
        for path in created_paths:
            if path.resolve().parent == directory: path.unlink(missing_ok=True)
        raise
    meeting.action_log.append(entry)
    metadata = {"id": export_id, "created_at": created, "title": body.title, "directory": str(directory),
                "filenames": filenames, "revision": meeting.revision, "review_version": meeting.review_version,
                "approved_count": len(data["approved_tasks"]), "decision_count": len(data["confirmed_decisions"])}
    meeting.exports.append(metadata)
    return metadata


def download_path(root, meeting, export_id, kind):
    if kind not in {"md", "csv"}: raise ReviewError("Only Markdown and CSV exports are available.")
    exported = next((entry for entry in meeting.exports if entry["id"] == export_id), None)
    if not exported: raise ReviewError("Export does not belong to the current meeting session.")
    directory = export_directory(root, meeting.id)
    path = directory / exported["filenames"][kind]
    if path.resolve().parent != directory or path.is_symlink(): raise ReviewError("Invalid export file location.")
    return path
