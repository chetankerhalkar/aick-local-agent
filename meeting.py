"""Stage 2 meeting data and conservative evidence validation. No I/O tools."""
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAMPLES = ("01_launch_meeting.txt", "02_conflicts_and_injection.txt", "03_no_actions.txt")
MAX_SEGMENTS = 300
MAX_SESSION_CHARS = 120_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class TypedInput(StrictModel):
    revision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=2000)
    start_s: float = Field(ge=0, le=86400)
    end_s: float | None = Field(default=None, ge=0, le=86400)
    speaker: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_s is not None and self.end_s < self.start_s:
            raise ValueError("End must be at or after start.")
        return self


class RevisionInput(StrictModel):
    revision: int = Field(ge=0)


class SampleInput(RevisionInput):
    sample: Literal["01_launch_meeting.txt", "02_conflicts_and_injection.txt", "03_no_actions.txt"]


class AskInput(RevisionInput):
    question: str = Field(min_length=1, max_length=1500)


class Segment(StrictModel):
    id: str
    start_s: float
    end_s: float | None
    end_inferred: bool = False
    source_type: Literal["typed", "sample file", "live audio", "uploaded audio", "sample replay"]
    source_label: str
    source_segment_id: str | None = None
    upload_id: str | None = None
    audio_start_s: float | None = None
    audio_end_s: float | None = None
    capture_id: str | None = None
    overlap_note: str | None = None
    asr_latency_s: float | None = None
    speaker: str | None
    text: str


@dataclass
class Meeting:
    id: str = field(default_factory=lambda: "M-" + secrets.token_hex(5))
    revision: int = 0
    segments: list[Segment] = field(default_factory=list)
    next_segment: int = 1
    busy: bool = False
    last_error: str | None = None
    result: dict | None = None
    history: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    audio_jobs: list[dict] = field(default_factory=list)
    capture: dict | None = None
    speech: dict | None = None
    task_reviews: dict = field(default_factory=dict)
    review_version: int = 0
    action_log: list[dict] = field(default_factory=list)
    runtime_identity: dict = field(default_factory=dict)
    exports: list[dict] = field(default_factory=list)

    def add(self, **fields):
        if len(self.segments) >= MAX_SEGMENTS or sum(len(s.text) for s in self.segments) + len(fields["text"]) > MAX_SESSION_CHARS:
            raise ValueError("Session limit reached (300 segments / 120,000 characters). Start a new session.")
        segment = Segment(id=f"S{self.next_segment:04d}", **fields)
        self.next_segment += 1
        self.segments.append(segment)
        self.revision += 1
        self.last_error = None
        return segment

    def view(self):
        return {
            "id": self.id, "revision": self.revision,
            "state": "processing" if self.busy else "error" if self.last_error else "listening" if self.capture and self.capture["active"] and not self.capture["paused"] else "ready" if self.segments else "not connected",
            "segments": [s.model_dump() for s in self.segments],
            "result": self.result, "history": self.history,
            "result_stale": bool(self.result and self.result["revision"] != self.revision),
            "error": self.last_error, "trace": self.trace, "audio_jobs": self.audio_jobs, "capture": self.capture,
            "speech": self.speech,
            "task_reviews": self.task_reviews, "review_version": self.review_version,
            "action_log": self.action_log, "runtime_identity": self.runtime_identity, "exports": self.exports,
        }

    def append_audio(self, rows: list[dict], upload_id: str, offset_s: float):
        # Prepare the entire batch before mutation so a rejected upload adds nothing.
        if len(self.segments) + len(rows) > MAX_SEGMENTS or sum(len(s.text) for s in self.segments) + sum(len(r["text"]) for r in rows) > MAX_SESSION_CHARS:
            raise ValueError("Session limit reached (300 segments / 120,000 characters). No audio segments were appended.")
        batch = [Segment(id=f"S{self.next_segment + i:04d}",
                         start_s=row["start_s"] + offset_s, end_s=row["end_s"] + offset_s,
                         audio_start_s=row["start_s"], audio_end_s=row["end_s"], upload_id=upload_id,
                         text=row["text"], speaker=None, source_type="uploaded audio", source_label="Uploaded audio")
                 for i, row in enumerate(rows)]
        self.segments.extend(batch)
        self.next_segment += len(batch)
        self.revision += len(batch)


def parse_sample(text: str, filename: str) -> Meeting:
    """Preserve supplied starts. Infer ends from the next start; final end is unknown."""
    meeting = Meeting()
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\[(S\d+) (\d{2,3}):(\d{2})\] ([^:]{1,80}): (.+)", line.strip())
        if not match or int(match[3]) >= 60:
            raise ValueError("Sample format must be [S01 MM:SS] Speaker: text.")
        start = int(match[2]) * 60 + int(match[3])
        if rows and start < rows[-1][1]:
            raise ValueError("Sample timestamps must be ordered.")
        rows.append((match[1], start, match[4], match[5]))
    if not rows:
        raise ValueError("Sample is empty.")
    for index, (original_id, start, speaker, content) in enumerate(rows):
        end = rows[index + 1][1] if index + 1 < len(rows) else None
        meeting.add(start_s=start, end_s=end, end_inferred=end is not None,
                    source_type="sample file", source_label=f"Sample transcript · {filename}",
                    source_segment_id=original_id, speaker=speaker, text=content)
    return meeting


def search_transcript(segments: list[Segment], query: str) -> list[str]:
    """Local lexical retrieval with recency and correction cues. No model-selected tools."""
    words = set(re.findall(r"[a-z]{3,}", query.lower())) - {"the", "and", "what", "are", "for", "this", "that"}
    scored = []
    for index, segment in enumerate(segments):
        text = segment.text.lower()
        score = sum(word in text for word in words) * 3
        score += 3 * bool(re.search(r"correction|replaces|unresolved|not resolved|instead|not approved", text))
        score += bool(re.search(r"will|decided|agree|owner|deadline|should|could", text))
        scored.append((score, index, segment.id))
    return [row[2] for row in sorted(scored, reverse=True)]


def read_transcript(segments: list[Segment], ids: list[str]) -> list[dict]:
    wanted = set(ids)
    return [segment.model_dump() for segment in segments if segment.id in wanted]


class Evidence(StrictModel):
    segment_id: str = Field(min_length=1, max_length=20)
    quote: str = Field(min_length=1, max_length=1600)


class Claim(StrictModel):
    id: str = Field(pattern=r"^[CDQA][1-9][0-9]?$")
    text: str = Field(min_length=1, max_length=600)
    evidence: list[Evidence] = Field(max_length=4)


class AnswerClaim(Claim):
    id: str = Field(pattern=r"^C[1-9][0-9]?$")


class DecisionClaim(Claim):
    id: str = Field(pattern=r"^D[1-9][0-9]?$")


class QuestionClaim(Claim):
    id: str = Field(pattern=r"^Q[1-9][0-9]?$")


class Action(StrictModel):
    id: str = Field(pattern=r"^A[1-9][0-9]?$")
    task: str = Field(min_length=1, max_length=400)
    owner: str | None = Field(max_length=80)
    due_date: str | None = Field(max_length=10)
    due_text: str | None = Field(max_length=120)
    status: Literal["proposed", "confirmed", "superseded"]
    evidence: list[Evidence] = Field(max_length=4)
    owner_evidence: list[Evidence] = Field(max_length=2)
    due_evidence: list[Evidence] = Field(max_length=2)
    supersedes: list[str] = Field(max_length=3)


class Proposal(StrictModel):
    actions: list[Action] = Field(max_length=8)
    confirmed_decisions: list[DecisionClaim] = Field(max_length=4)
    unresolved_questions: list[QuestionClaim] = Field(max_length=6)
    answer: list[AnswerClaim] = Field(max_length=4)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [row.id for group in (self.answer, self.confirmed_decisions, self.actions, self.unresolved_questions) for row in group]
        if len(ids) != len(set(ids)):
            raise ValueError("Claim IDs must be unique across all fields.")
        return self


class Verdict(StrictModel):
    id: str
    verdict: Literal["supported", "unsupported", "ambiguous"]
    reason: str = Field(max_length=350)
    owner_supported: bool | None
    due_supported: bool | None
    status_supported: bool | None
    supersession_supported: bool | None


class Audit(StrictModel):
    verdicts: list[Verdict] = Field(max_length=22)


def explicit_dates(text: str) -> set[str]:
    dates = set()
    for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
        try:
            dates.add(datetime.strptime(value, "%Y-%m-%d").date().isoformat())
        except ValueError:
            pass
    for value in re.findall(r"\b[A-Za-z]+ \d{1,2},? \d{4}\b", text):
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                dates.add(datetime.strptime(value.replace(",", ""), fmt).date().isoformat())
            except ValueError:
                pass
    return dates


def reconcile_explicit_corrections(proposal: Proposal, segments: list[dict]) -> tuple[Proposal, list[dict]]:
    """Recover a missing historical row only from an unambiguous explicit by-date promise.

    This deliberately handles a narrow form. It never invents a task/owner/date:
    each is copied from the older statement and its supplied speaker. Both a
    task-word match and an explicit reference to the old date are required.
    Unsupported/ambiguous forms remain for review rather than heuristic merging.
    """
    proposal = proposal.model_copy(deep=True)
    events = []
    for correction in segments:
        if not re.search(r"\bcorrection\b|\breplaces\b", correction["text"], re.I) or not correction["speaker"]:
            continue
        candidates = []
        for older in segments:
            if older["start_s"] >= correction["start_s"] or older["speaker"] != correction["speaker"]:
                continue
            match = re.match(r"I will (.+?) by (.+?)(?:\.|$)", older["text"], re.I)
            if not match:
                continue
            task, due_text = match.groups()
            dates = explicit_dates(due_text)
            words = set(re.findall(r"[a-z]{4,}", task.lower())) - {"will", "that", "this", "with", "from", "have"}
            if len(words.intersection(re.findall(r"[a-z]{4,}", correction["text"].lower()))) < 2 or len(dates) != 1:
                continue
            old_date = next(iter(dates))
            parsed_date = datetime.strptime(old_date, "%Y-%m-%d")
            day_text = f"{parsed_date.strftime('%B')} {parsed_date.day}"
            if old_date not in correction["text"] and not re.search(re.escape(day_text) + r"\b", correction["text"], re.I):
                continue
            candidates.append((older, task, due_text, old_date))
        if len(candidates) != 1:
            events.append({"step": "reconcile_correction", "segment_id": correction["id"],
                           "result": "No unique explicit prior task/date match. No supersession inferred."})
            continue
        older, task, due_text, old_date = candidates[0]
        current = [a for a in proposal.actions if a.owner == correction["speaker"]
                   and any(e.segment_id == correction["id"] and e.quote in correction["text"] for e in a.due_evidence)
                   and len(set(re.findall(r"[a-z]{4,}", a.task.lower())).intersection(re.findall(r"[a-z]{4,}", task.lower()))) >= 2]
        if len(current) != 1:
            continue
        new_action = current[0]
        old_actions = [a for a in proposal.actions if a.id != new_action.id and a.owner == older["speaker"] and a.due_date == old_date
                       and any(e.segment_id == older["id"] and e.quote in older["text"] for e in a.evidence)]
        if len(old_actions) > 1:
            continue
        if old_actions:
            old_action = old_actions[0]
        elif len(proposal.actions) < 8:
            old_id = next(f"A{number}" for number in range(1, 100) if all(a.id != f"A{number}" for a in proposal.actions))
            ref = Evidence(segment_id=older["id"], quote=older["text"])
            old_action = Action(id=old_id, task=task, owner=older["speaker"], due_date=old_date, due_text=due_text,
                                status="superseded", evidence=[ref], owner_evidence=[ref], due_evidence=[ref], supersedes=[])
            proposal.actions.insert(proposal.actions.index(new_action), old_action)
        else:
            continue
        previous_links = new_action.supersedes[:]
        new_action.supersedes = [old_action.id]
        old_action.status = "superseded"
        events.append({"step": "reconcile_correction", "old_segment": older["id"], "correction_segment": correction["id"],
                       "historical_action": old_action.id, "current_action": new_action.id, "model_links": previous_links,
                       "reconciled_links": new_action.supersedes,
                       "historical_fact": {"task": task, "owner": older["speaker"], "due_date": old_date, "due_text": due_text,
                                           "evidence": [{"segment_id": older["id"], "quote": older["text"]}]},
                       "result": "Exact prior first-person task/date retained. Same supplied speaker, task words and explicit old date match. Still subject to review."})
    return proposal, events


def validate_proposal(proposal: Proposal, segments: list[dict], audit: Audit | None, partial: bool, correction_proofs=None) -> dict:
    """Quotes/IDs are deterministic checks; semantic support remains fallible model review."""
    source = {row["id"]: row for row in segments}
    verdict_list = audit.verdicts if audit else []
    verdicts = {row.id: row for row in verdict_list if sum(v.id == row.id for v in verdict_list) == 1}
    result = {"answer": [], "confirmed_decisions": [], "review_decisions": [], "actions": [], "unresolved_questions": [], "evidence": []}
    seen_evidence = set()

    def check_refs(refs):
        problems = []
        if not refs:
            return ["No evidence supplied."]
        for ref in refs:
            row = source.get(ref["segment_id"])
            if row is None:
                problems.append(f"Unknown or omitted segment: {ref['segment_id']}.")
            elif not ref["quote"].strip() or ref["quote"] not in row["text"]:
                problems.append(f"Quote is not an exact substring of {ref['segment_id']}.")
            else:
                key = (ref["segment_id"], ref["quote"])
                if key not in seen_evidence:
                    seen_evidence.add(key)
                    result["evidence"].append({**ref, "speaker": row["speaker"], "source_label": row["source_label"], "start_s": row["start_s"], "end_s": row["end_s"]})
        return problems

    for group in ("answer", "confirmed_decisions", "actions", "unresolved_questions"):
        for model_item in getattr(proposal, group):
            item = model_item.model_dump()
            issues = check_refs(item["evidence"])
            item["citation_check"] = "matched" if not issues else "failed"
            verdict = verdicts.get(item["id"])
            item["semantic_check"] = verdict.verdict if verdict else "not checked"
            if not verdict or verdict.verdict != "supported":
                issues.append(verdict.reason if verdict else "Semantic review is missing; factual support is not established.")
            if partial:
                issues.append("Partial context: omitted segments may change this conclusion.")
            if group == "actions":
                item["model_proposal"] = {key: item[key] for key in ("owner", "due_date", "due_text", "status", "supersedes")}
                owner_refs = item["owner_evidence"]
                if item["owner"] is not None:
                    owner_issues = check_refs(owner_refs)
                    matched = any((source.get(e["segment_id"], {}).get("speaker") or "").casefold() == item["owner"].casefold()
                                  or re.search(r"(?<!\w)" + re.escape(item["owner"]) + r"(?!\w)", e["quote"], re.I) for e in owner_refs)
                    if owner_issues or not matched or not verdict or verdict.owner_supported is not True:
                        item["owner"] = None
                        issues.append("Proposed owner is not established by the cited text/supplied speaker label; cleared for review.")
                if item["owner"] is None:
                    issues.append("Owner is unresolved.")
                if item["due_date"] is not None or item["due_text"] is not None:
                    due_issues = check_refs(item["due_evidence"])
                    due_quotes = "\n".join(e["quote"] for e in item["due_evidence"])
                    if due_issues or not verdict or verdict.due_supported is not True:
                        item["due_date"] = item["due_text"] = None
                        issues.append("Proposed deadline is not supported; cleared for review.")
                    else:
                        if item["due_text"] is not None and item["due_text"] not in due_quotes:
                            item["due_text"] = None
                            issues.append("Due text is not an exact source phrase; cleared for review.")
                        if item["due_date"] is not None and item["due_date"] not in explicit_dates(due_quotes):
                            item["due_date"] = None
                            issues.append("No explicit calendar date supports the proposed date; relative dates are not guessed.")
                if item["due_date"] is None and item["due_text"] is None:
                    issues.append("Deadline is unresolved.")
                if item["status"] == "confirmed" and (item["citation_check"] != "matched" or not verdict or verdict.verdict != "supported" or verdict.status_supported is not True or partial):
                    item["status"] = "proposed"
                    issues.append("Confirmation is not established; retained as a proposal for review.")
            item["issues"] = issues
            item["review_required"] = True
            item["validation"] = "unsupported" if item["citation_check"] == "failed" or (verdict and verdict.verdict == "unsupported") else "needs review" if issues else "evidence matched · model reviewed"
            destination = "review_decisions" if group == "confirmed_decisions" and issues else group
            result[destination].append(item)

    actions = {a["id"]: a for a in result["actions"]}
    # A historical quote remains a historical fact when a model incorrectly
    # evaluates its old deadline as if it were a current deadline. Keep that
    # disagreement visible, while retaining the exact source record. Only the
    # strict host-derived correction proof can use this path, never model data.
    for proof in correction_proofs or []:
        if partial or "historical_fact" not in proof:
            continue
        old = actions.get(proof["historical_action"])
        current = actions.get(proof["current_action"])
        reviewed = verdicts.get(proof["current_action"])
        if (old and current and old["citation_check"] == current["citation_check"] == "matched"
                and reviewed and reviewed.verdict == "supported" and reviewed.supersession_supported is True
                and current["owner"] == proof["historical_fact"]["owner"]):
            fact = proof["historical_fact"]
            old.update({key: fact[key] for key in ("task", "owner", "due_date", "due_text", "evidence")})
            old["owner_evidence"] = old["due_evidence"] = fact["evidence"]
            old["issues"] = ["Historical statement retained by exact-source correction checks; it is not a current deadline."]
            if old["semantic_check"] != "supported":
                old["issues"].append("The semantic reviewer disputed this historical row. Inspect the original and correction quotes.")
            old["validation"] = "historical evidence matched · needs review"
    corrected = set()
    for action in actions.values():
        valid_links = []
        verdict = verdicts.get(action["id"])
        for old_id in action["supersedes"]:
            old = actions.get(old_id)
            newer_times = [source[e["segment_id"]]["start_s"] for e in action["evidence"] if e["segment_id"] in source]
            old_times = [source[e["segment_id"]]["start_s"] for e in old["evidence"] if e["segment_id"] in source] if old else []
            correction_words = any(re.search(r"correction|replaces|instead|no longer|reschedul|changed|updated", e["quote"], re.I) for e in action["evidence"])
            if (old and old_id != action["id"] and newer_times and old_times and max(newer_times) > min(old_times)
                    and correction_words and action["citation_check"] == old["citation_check"] == "matched"
                    and verdict and verdict.verdict == "supported" and verdict.supersession_supported is True
                    and action["owner"] == old["owner"] and not partial):
                old["status"] = "superseded"
                old["superseded_by"] = action["id"]
                corrected.add(old_id)
                valid_links.append(old_id)
            else:
                action["issues"].append(f"Supersession of {old_id} is unproven; contradiction remains unresolved.")
                action["validation"] = "needs review"
        action["supersedes"] = valid_links
    for action in actions.values():
        if action["status"] == "superseded" and action["id"] not in corrected:
            action["status"] = "proposed"
            action["issues"].append("No validated explicit correction establishes supersession.")
            action["validation"] = "needs review"
    return result
