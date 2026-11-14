"""Stage 2 adversarial software checks; all model responses here are labeled mocks."""
import asyncio
import json
import re
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import app
import agent_workflow as workflow
from meeting import Meeting, Proposal, Audit, TypedInput, parse_sample, validate_proposal, reconcile_explicit_corrections


def fixture_proposal(quote="I will draft the outline.", owner="Maya", date=None, status="confirmed"):
    evidence = [{"segment_id": "S0001", "quote": quote}]
    return Proposal.model_validate({"answer": [], "confirmed_decisions": [], "unresolved_questions": [], "actions": [
        {"id": "A1", "task": "Draft the outline", "owner": owner, "due_date": date, "due_text": None,
         "status": status, "evidence": evidence, "owner_evidence": evidence, "due_evidence": evidence, "supersedes": []}]})


def fixture_audit(**overrides):
    verdict = {"id": "A1", "verdict": "supported", "reason": "Mock explicit commitment", "owner_supported": True,
               "due_supported": True, "status_supported": True, "supersession_supported": None}
    return Audit.model_validate({"verdicts": [verdict | overrides]})


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.meeting = parse_sample("[S01 00:00] Maya: I will draft the outline.\n[S02 00:10] Eli: No budget was approved.", "unit-test.txt")
        self.segments = [s.model_dump() for s in self.meeting.segments]

    def test_stable_ids_supplied_timing_and_unknown_final_end(self):
        self.assertEqual([s.id for s in self.meeting.segments], ["S0001", "S0002"])
        self.assertEqual(self.meeting.segments[0].end_s, 10)
        self.assertTrue(self.meeting.segments[0].end_inferred)
        self.assertIsNone(self.meeting.segments[1].end_s)
        self.meeting.add(start_s=20, end_s=22, speaker=None, text="Additional typed text", source_type="typed", source_label="Typed transcript")
        self.assertEqual(self.meeting.segments[2].id, "S0003")
        self.assertEqual(self.meeting.segments[0].id, "S0001")

    def test_missing_ids_and_changed_quotes_are_unsupported(self):
        for quote, sid in (("I approved all spending.", "S0001"), ("I will draft the outline.", "S9999")):
            proposal = fixture_proposal(quote)
            proposal.actions[0].evidence[0].segment_id = sid
            result = validate_proposal(proposal, self.segments, fixture_audit(), False)["actions"][0]
            self.assertEqual(result["citation_check"], "failed")
            self.assertEqual(result["status"], "proposed")

    def test_real_quote_does_not_prove_unrelated_claim(self):
        result = validate_proposal(fixture_proposal(), self.segments, fixture_audit(verdict="unsupported", reason="Mock: unrelated assertion"), False)["actions"][0]
        self.assertEqual(result["citation_check"], "matched")
        self.assertEqual(result["validation"], "unsupported")
        self.assertEqual(result["status"], "proposed")

    def test_fabricated_owner_and_date_cleared_even_if_auditor_misses_them(self):
        action = validate_proposal(fixture_proposal(owner="Someone else", date="2030-12-01"), self.segments, fixture_audit(), False)["actions"][0]
        self.assertIsNone(action["owner"])
        self.assertIsNone(action["due_date"])
        self.assertEqual(action["model_proposal"]["owner"], "Someone else")

    def test_missing_semantic_review_never_confirms(self):
        for audit in (None, Audit(verdicts=[])):
            action = validate_proposal(fixture_proposal(), self.segments, audit, False)["actions"][0]
            self.assertEqual(action["status"], "proposed")
            self.assertTrue(action["review_required"])

    def test_partial_context_is_always_reviewable_and_not_confirmed(self):
        action = validate_proposal(fixture_proposal(), self.segments, fixture_audit(), True)["actions"][0]
        self.assertEqual(action["status"], "proposed")
        self.assertTrue(any("Partial context" in issue for issue in action["issues"]))

    def test_no_supersession_without_valid_explicit_correction(self):
        action = validate_proposal(fixture_proposal(status="superseded"), self.segments, fixture_audit(), False)["actions"][0]
        self.assertEqual(action["status"], "proposed")

    def test_explicit_correction_keeps_old_commitment_as_superseded(self):
        meeting = parse_sample("[S01 00:00] Maya: I will draft the outline by July 6, 2027.\n[S02 00:20] Maya: Correction: the outline is due July 8, 2027 instead.", "unit-test.txt")
        first = fixture_proposal().actions[0].model_dump()
        first.update(due_date="2027-07-06", due_text="July 6, 2027", status="superseded")
        second = dict(first, id="A2", due_date="2027-07-08", due_text="July 8, 2027", status="confirmed", supersedes=["A1"])
        for action, segment in zip((first, second), meeting.segments):
            for key in ("evidence", "owner_evidence", "due_evidence"):
                action[key] = [{"segment_id": segment.id, "quote": segment.text}]
        proposal = Proposal.model_validate({"actions": [first, second], "answer": [], "confirmed_decisions": [], "unresolved_questions": []})
        a1 = fixture_audit().verdicts[0].model_dump()
        audit = Audit.model_validate({"verdicts": [a1, dict(a1, id="A2", supersession_supported=True)]})
        result = validate_proposal(proposal, [s.model_dump() for s in meeting.segments], audit, False)
        self.assertEqual(result["actions"][0]["status"], "superseded")
        self.assertEqual(result["actions"][0]["due_date"], "2027-07-06")
        self.assertEqual(result["actions"][1]["due_date"], "2027-07-08")
        self.assertEqual(result["actions"][1]["supersedes"], ["A1"])

    def test_typed_input_rejects_fake_live_source_and_invalid_times(self):
        for data in ({"source_type": "live audio"}, {"start_s": -1}, {"end_s": 1, "start_s": 5}, {"start_s": float("nan")}):
            with self.assertRaises(ValidationError):
                TypedInput.model_validate({"text": "test", "revision": 0, "start_s": 0} | data)

    def test_reconciliation_retains_missing_history_without_cross_owner_link(self):
        text = "[S01 00:00] Eli: I will draft the design outline by July 6, 2027.\n[S02 00:10] Maya: I will review the security checklist by July 7, 2027.\n[S03 00:20] Maya: Correction. My security review is due July 9, 2027. That replaces the July 7 date."
        meeting = parse_sample(text, "unit-test.txt")
        first = fixture_proposal(owner="Eli").actions[0].model_dump()
        second = dict(first, id="A2", task="Review the security checklist", owner="Maya", due_date="2027-07-09", due_text="July 9, 2027", supersedes=["A1"])
        for key in ("evidence", "owner_evidence", "due_evidence"):
            second[key] = [{"segment_id": "S0003", "quote": meeting.segments[2].text}]
        proposal = Proposal.model_validate({"actions": [first, second], "answer": [], "confirmed_decisions": [], "unresolved_questions": []})
        updated, trace = reconcile_explicit_corrections(proposal, [s.model_dump() for s in meeting.segments])
        historical = next(a for a in updated.actions if a.due_date == "2027-07-07")
        self.assertEqual(historical.owner, "Maya")
        self.assertEqual(historical.status, "superseded")
        self.assertEqual(next(a for a in updated.actions if a.id == "A2").supersedes, [historical.id])
        self.assertEqual(trace[-1]["model_links"], ["A1"])
        self.assertEqual(proposal.actions[1].supersedes, ["A1"])  # Original model response retained.
        verdicts = [fixture_audit(id=a.id).verdicts[0].model_dump() for a in updated.actions]
        for verdict in verdicts:
            if verdict["id"] == historical.id:
                verdict.update(verdict="unsupported", owner_supported=False, due_supported=False, reason="Mock reviewer mistook historical due date for current")
            if verdict["id"] == "A2":
                verdict["supersession_supported"] = True
        result = validate_proposal(updated, [s.model_dump() for s in meeting.segments], Audit.model_validate({"verdicts": verdicts}), False, trace)
        old = next(a for a in result["actions"] if a["id"] == historical.id)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["due_date"], "2027-07-07")
        self.assertEqual(old["semantic_check"], "unsupported")
        self.assertTrue(any("disputed" in issue for issue in old["issues"]))


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app, base_url=app.ORIGIN)
        page = self.client.get("/")
        self.token = re.search(r'name="aick-session-token" content="([^"]+)"', page.text)[1]
        self.client.headers.update({"X-Session-Token": self.token, "Origin": app.ORIGIN})

    def tearDown(self):
        self.client.close()

    def test_no_automatic_import_and_cannot_ask_empty(self):
        view = self.client.get("/api/meeting").json()
        self.assertEqual(view["segments"], [])
        self.assertEqual(self.client.post("/api/meeting/ask", json={"revision": 0, "question": "Any tasks?"}).status_code, 409)

    def test_import_allowlist_rejects_paths(self):
        for sample in ("../evaluation/expected.json", "C:/secrets.txt", "04.txt"):
            self.assertEqual(self.client.post("/api/meeting/sample", json={"revision": 0, "sample": sample}).status_code, 422)

    def test_stale_revision_and_busy_session_rejected(self):
        result = self.client.post("/api/meeting/segments", json={"revision": 0, "start_s": 10, "text": "Typed test"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["segments"][0]["source_type"], "typed")
        self.assertEqual(self.client.post("/api/meeting/new", json={"revision": 0}).status_code, 409)
        app.MEETINGS[self.token].busy = True
        self.assertEqual(self.client.post("/api/meeting/new", json={"revision": 1}).status_code, 409)

    def test_sessions_do_not_share_transcript(self):
        self.client.post("/api/meeting/segments", json={"revision": 0, "start_s": 0, "text": "Private test fixture"})
        with TestClient(app.app, base_url=app.ORIGIN) as second:
            page = second.get("/")
            token = re.search(r'name="aick-session-token" content="([^"]+)"', page.text)[1]
            self.assertEqual(second.get("/api/meeting", headers={"X-Session-Token": token}).json()["segments"], [])


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_format_repair_for_entire_workflow(self):
        response = {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}]}
        repair = {"used": False}
        with patch.object(workflow, "count_prompt", new=AsyncMock(return_value=100)), patch.object(workflow, "post_json", new=AsyncMock(return_value=response)) as post:
            with self.assertRaises(workflow.WorkflowError):
                await workflow.generate_json(None, "http://127.0.0.1", "mock", [{"role": "system", "content": "test"}], Proposal, [], repair, "propose", 3200)
            self.assertEqual(post.await_count, 2)
            with self.assertRaises(workflow.WorkflowError):
                await workflow.generate_json(None, "http://127.0.0.1", "mock", [{"role": "system", "content": "test"}], Proposal, [], repair, "audit", 2400)
            self.assertEqual(post.await_count, 3)  # first audit attempt, no second repair

    async def test_budget_rejects_before_model_request(self):
        with patch.object(workflow, "count_prompt", new=AsyncMock(return_value=4481)), patch.object(workflow, "post_json", new=AsyncMock()) as post:
            with self.assertRaises(workflow.WorkflowError):
                await workflow.generate_json(None, "http://127.0.0.1", "mock", [], Proposal, [], {"used": False}, "propose", 3200)
            post.assert_not_called()

    async def test_omitted_context_ids_are_visible(self):
        meeting = Meeting()
        for n in range(10):
            meeting.add(start_s=n, end_s=None, speaker=None, text="Long unit test segment", source_type="typed", source_label="Typed transcript")
        async def token_count(client, url, payload, **kwargs):
            count = len(json.loads(payload["content"])) * 400
            return {"tokens": [0] * count}
        with patch.object(workflow, "post_json", side_effect=token_count), patch.object(workflow, "count_prompt", new=AsyncMock(return_value=2000)):
            context, _, budget = await workflow.retrieve(None, "http://127.0.0.1", meeting.segments, "question", [])
        self.assertTrue(budget["omitted_ids"])
        self.assertEqual(len(context) + len(budget["omitted_ids"]), 10)
        self.assertIn("has not read omitted content", budget["notice"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
