"""Stage 6 review/export tests. Proposal fixtures are explicit test data, not model acceptance."""
import copy
import csv
import io
import json
import re
import tempfile
import subprocess
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
import app
from meeting import Meeting
from meeting_exports import (ReviewError, ReviewInput, ExportInput, register_analysis, review_action,
                             report_data, export_report, download_path, csv_cell, safe_slug, export_directory)


def fixture():
    meeting = Meeting()
    for text in ("Maya will draft the outline.", "Someone could write a cost report; no owner or date.", "We agreed to a small pilot."):
        meeting.add(start_s=0, end_s=5, speaker=None, text=text, source_type="typed", source_label="Typed transcript")
    refs = lambda n: [{"segment_id": f"S{n:04}", "quote": meeting.segments[n-1].text}]
    actions = [{"id": f"A{n}", "task": task, "owner": None, "due_date": None, "due_text": None,
        "status": "proposed", "evidence": refs(n), "owner_evidence": [], "due_evidence": [],
        "citation_check": "matched", "semantic_check": "supported", "validation": "needs review", "issues": ["Owner is unresolved."], "supersedes": []}
        for n, task in ((1, "Draft the outline"), (2, "Write a cost report"))]
    result = {"run_id": "R-test", "revision": meeting.revision, "actions": actions, "confirmed_decisions": [
        {"id": "D1", "text": "Run a small pilot", "evidence": refs(3), "citation_check": "matched", "semantic_check": "supported", "issues": []}],
        "review_decisions": [], "unresolved_questions": [], "answer": [], "budget": {"notice": "All segments included.", "omitted_ids": []}, "trace": []}
    register_analysis(meeting, result, {"llm_model_alias": "unit mock model"})
    meeting.result = result
    return meeting


def review(meeting, action, operation, **fields):
    review_action(meeting, ReviewInput(revision=meeting.revision, review_version=meeting.review_version,
        run_id=meeting.result["run_id"], action_id=action, operation=operation, **fields))


class ReviewTests(unittest.TestCase):
    def test_rejected_action_is_only_in_labeled_audit_and_original_transcript(self):
        meeting = fixture()
        review(meeting, "A1", "approve"); review(meeting, "A2", "reject")
        data = report_data(meeting)
        self.assertEqual([a["id"] for a in data["approved_tasks"]], ["A1"])
        self.assertNotIn("cost report", " ".join(data["draft_follow_up"]))
        with tempfile.TemporaryDirectory(dir=app.ROOT / "work") as temp:
            exported = export_report(Path(temp), meeting, ExportInput(revision=3, review_version=meeting.review_version))
            csv_path = download_path(Path(temp), meeting, exported["id"], "csv")
            rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"))))
            self.assertEqual([r["item_id"] for r in rows if r["record_type"] == "approved_task"], ["A1"])
            self.assertTrue(any(r["record_type"] == "audit_history" and '"event": "rejection"' in r["text"] for r in rows))
            markdown = download_path(Path(temp), meeting, exported["id"], "md").read_text(encoding="utf-8")
            self.assertNotIn("cost report", markdown.split("## Approved tasks")[1].split("## Open questions")[0])
            for row in rows:
                for ref in json.loads(row["citations_json"]):
                    self.assertTrue(any(s.id == ref["segment_id"] and ref["quote"] in s.text for s in meeting.segments))

    def test_edits_revoke_approval_and_keep_original_evidence(self):
        meeting = fixture(); original = copy.deepcopy(meeting.result["actions"][0])
        review(meeting, "A1", "approve")
        review(meeting, "A1", "edit", task="Presenter wording", owner=None, due_date=None, due_text=None)
        self.assertEqual(report_data(meeting)["approved_tasks"], [])
        review(meeting, "A1", "approve")
        self.assertEqual(meeting.result["actions"][0], original)
        event = next(e for e in meeting.action_log if e["event"] == "presenter_correction")
        self.assertEqual(event["data"]["original_proposal"], original)
        self.assertEqual(event["data"]["before"]["status"], "approved")
        self.assertIn("Owner: unresolved. Due: unresolved.", " ".join(report_data(meeting)["draft_follow_up"]))

    def test_unsupported_decisions_and_invalid_task_citations_do_not_export(self):
        meeting = fixture()
        meeting.result["confirmed_decisions"][0]["semantic_check"] = "ambiguous"
        self.assertEqual(report_data(meeting)["confirmed_decisions"], [])
        meeting.result["actions"][0]["evidence"][0]["quote"] = "invented"
        with self.assertRaises(ReviewError): review(meeting, "A1", "approve")
        self.assertEqual(meeting.action_log[-1]["event"], "validation_failure")
        meeting = fixture(); review(meeting, "A1", "approve")
        meeting.result["actions"][0]["evidence"][0]["segment_id"] = "S9999"
        with self.assertRaises(ReviewError): report_data(meeting)

    def test_stale_run_version_and_new_analysis_cannot_reuse_approval(self):
        meeting = fixture(); review(meeting, "A1", "approve")
        with self.assertRaises(ReviewError): review_action(meeting, ReviewInput(revision=3, review_version=0, run_id="R-test", action_id="A1", operation="reject"))
        updated = copy.deepcopy(meeting.result); updated["run_id"] = "R-next"
        register_analysis(meeting, updated, {}); meeting.result = updated
        self.assertEqual(report_data(meeting)["approved_tasks"], [])
        self.assertTrue(any(e["event"] == "approval" for e in meeting.action_log))
        meeting.revision += 1
        with self.assertRaises(ReviewError): report_data(meeting)

    def test_superseded_tasks_and_unvalidated_edit_dates_are_rejected(self):
        meeting = fixture(); meeting.result["actions"][0]["status"] = "superseded"
        with self.assertRaises(ReviewError): review(meeting, "A1", "approve")
        with self.assertRaises(ValidationError): review(meeting, "A1", "edit", task="new", owner=None, due_date="2026-02-30", due_text=None)


class PathAndFormatTests(unittest.TestCase):
    def test_formula_prefixes_unicode_whitespace_and_cell_boundaries(self):
        for value in ('=1+2', '+cmd', '-1+2', '@SUM(A1)', '\tanything', '\ranything', '\nanything', ' \ufeff=1', '\0=1', '＝1', '＋1', '－1', '＠x'):
            self.assertTrue(csv_cell(value).startswith("Text: "), repr(value))
        self.assertEqual(csv_cell('safe, "quoted"\nvalue'), 'safe, "quoted"\nvalue')

    def test_no_title_can_choose_an_output_path_and_names_do_not_collide(self):
        meeting = fixture()
        with tempfile.TemporaryDirectory(dir=app.ROOT / "work") as temp:
            root = Path(temp)
            for title in ('../../outside', 'C:\\escape\\CON', '<script>x</script>', '=cmd', 'NUL', '😀'):
                exported = export_report(root, meeting, ExportInput(revision=3, review_version=1, title=title))
                for kind in ('md', 'csv'):
                    path = download_path(root, meeting, exported["id"], kind)
                    self.assertEqual(path.resolve().parent, root / 'exports' / meeting.id)
            self.assertEqual(len(list(root.rglob('*.md'))), 6)
            self.assertTrue(all(p.is_relative_to(root / 'exports') for p in root.rglob('*') if p.is_file()))
            with self.assertRaises(ReviewError): download_path(root, meeting, '../other', 'md')
            meeting.exports[0]['filenames']['md'] = '../../escape.md'
            with self.assertRaises(ReviewError): download_path(root, meeting, meeting.exports[0]['id'], 'md')

    def test_redirected_directory_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=app.ROOT / 'work') as temp:
            root = Path(temp); outside = root / 'different'; outside.mkdir()
            link = root / 'exports'
            try: link.symlink_to(outside, target_is_directory=True)
            except OSError:
                if os.name != 'nt': raise
                # Windows junctions need no symlink privilege. Both targets are controlled test paths.
                created = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(outside)], shell=False, capture_output=True)
                self.assertEqual(created.returncode, 0, created.stderr)
            try:
                with self.assertRaises(ReviewError): export_directory(root, fixture().id)
            finally:
                self.assertEqual(link.resolve(), outside.resolve())
                self.assertTrue(outside.resolve().is_relative_to((app.ROOT / 'work').resolve()))
                if link.is_symlink(): link.unlink()
                else: link.rmdir()  # remove only the junction, never recurse into its target


class ApiTests(unittest.TestCase):
    def test_security_reset_and_fixed_export_contract(self):
        with TestClient(app.app, base_url=app.ORIGIN) as client:
            token = re.search(r'name="aick-session-token" content="([^"]+)"', client.get('/').text)[1]
            headers = {'X-Session-Token': token, 'Origin': app.ORIGIN}
            meeting = fixture(); app.MEETINGS[token] = meeting
            body = {'revision':3,'review_version':1,'run_id':'R-test','action_id':'A1','operation':'approve'}
            self.assertEqual(client.post('/api/meeting/review', json=body).status_code,403)
            self.assertEqual(client.post('/api/meeting/review', json=body, headers=headers|{'Origin':'https://evil.invalid'}).status_code,403)
            self.assertEqual(client.post('/api/meeting/review', json=body, headers=headers).status_code,200)
            export_body = {'revision':3,'review_version':meeting.review_version,'title':'../../evil'}
            self.assertEqual(client.post('/api/meeting/exports', json=export_body|{'path':'C:/elsewhere'}, headers=headers).status_code,422)
            with tempfile.TemporaryDirectory(dir=app.ROOT/'work') as temp, patch.object(app,'ROOT',Path(temp)):
                response=client.post('/api/meeting/exports',json=export_body,headers=headers)
                self.assertEqual(response.status_code,200,response.text)
                item=response.json()['exports'][0]
                path=f"/api/meeting/exports/{item['id']}/csv"
                self.assertEqual(client.get(path,headers=headers).status_code,200)
                reset=client.post('/api/meeting/new',json={'revision':3},headers=headers).json()
                for key in ('segments','action_log','exports'): self.assertEqual(reset[key],[])
                self.assertEqual(reset['task_reviews'],{})
                self.assertIsNone(reset['result'])
                self.assertEqual(client.get(path,headers=headers).status_code,404)
                self.assertEqual(client.get('/api/meeting/report-preview',headers=headers).status_code,409)


if __name__ == '__main__': unittest.main()
