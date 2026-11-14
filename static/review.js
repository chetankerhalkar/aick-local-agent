"use strict";
export class ReviewPanel {
  constructor({getMeeting, api, mutate, token}) {
    Object.assign(this, {getMeeting, api, mutate, token});
    this.key = ""; this.busy = true; this.capturing = false;
    this.el = id => document.getElementById(id);
    this.el("review-action").addEventListener("change", () => this.select());
    for (const id of ["review-task", "review-owner", "review-date", "review-due", "review-note"]) this.el(id).addEventListener("input", () => this.controls());
    for (const [id, operation] of [["approve-task", "approve"], ["reject-task", "reject"], ["save-task-edit", "edit"]]) {
      this.el(id).addEventListener("click", () => this.submit(operation));
    }
    this.el("export-approved").addEventListener("click", () => {
      const meeting = this.getMeeting();
      this.mutate("/api/meeting/exports", {review_version: meeting.review_version, title: this.el("export-title").value.trim() || "Meeting report"});
    });
  }
  node(tag, text, className) {
    const node = document.createElement(tag); node.textContent = text;
    if (className) node.className = className; return node;
  }
  fields() {
    return {task: this.el("review-task").value.trim(), owner: this.el("review-owner").value.trim() || null,
      due_date: this.el("review-date").value || null, due_text: this.el("review-due").value.trim() || null};
  }
  controls(options) {
    if (options) Object.assign(this, options);
    const meeting = this.getMeeting(), id = this.el("review-action").value;
    const original = meeting?.result?.actions.find(a => a.id === id), review = meeting?.task_reviews?.[id];
    const locked = this.busy || !meeting?.result || meeting.result_stale;
    const dirty = review && JSON.stringify(this.fields()) !== JSON.stringify(review.current);
    this.el("review-action").disabled = locked;
    for (const field of ["review-task", "review-owner", "review-date", "review-due", "review-note"]) this.el(field).disabled = locked || !review;
    this.el("approve-task").disabled = locked || !review || dirty || original?.status === "superseded" || original?.citation_check !== "matched";
    this.el("reject-task").disabled = locked || !review;
    this.el("save-task-edit").disabled = locked || !review || !dirty || !this.fields().task;
    this.el("export-approved").disabled = locked || this.capturing;
    this.el("review-unsaved").hidden = !dirty;
    this.el("export-title").disabled = this.busy;
  }
  select() {
    const meeting = this.getMeeting(), id = this.el("review-action").value;
    const original = meeting?.result?.actions.find(a => a.id === id), review = meeting?.task_reviews?.[id];
    this.el("review-original").textContent = original ? JSON.stringify(original, null, 2) : "Select a proposal to inspect its original fields, evidence and validation flags.";
    this.el("review-status").textContent = review ? `Presenter: ${review.status}${review.edited ? " · edited" : ""} · Model: ${original.status} · ${original.validation}` : "No proposal selected";
    for (const [id, field] of [["review-task", "task"], ["review-owner", "owner"], ["review-date", "due_date"], ["review-due", "due_text"]]) this.el(id).value = review?.current[field] || "";
    this.el("review-note").value = review?.note || "";
    this.controls();
  }
  async submit(operation) {
    const meeting = this.getMeeting();
    const body = {run_id: meeting.result.run_id, review_version: meeting.review_version,
      action_id: this.el("review-action").value, operation, note: this.el("review-note").value.trim()};
    if (operation === "edit") Object.assign(body, this.fields());
    await this.mutate("/api/meeting/review", body);
  }
  render(meeting) {
    const key = JSON.stringify([meeting.id, meeting.revision, meeting.result?.run_id, meeting.review_version, meeting.action_log.length, meeting.exports.length]);
    if (key === this.key) return;
    const newRun = this.meetingId !== meeting.id || this.runId !== meeting.result?.run_id;
    const selected = newRun ? "" : this.el("review-action").value;
    if (this.meetingId !== meeting.id) {
      this.el("export-title").value = "Meeting report";
      this.el("export-file-preview").textContent = "No export file selected.";
    }
    this.key = key; this.meetingId = meeting.id; this.runId = meeting.result?.run_id;
    const placeholder = this.node("option", "Choose a proposed task…"); placeholder.value = "";
    this.el("review-action").replaceChildren(placeholder, ...(meeting.result?.actions || []).map(item => {
      const option = this.node("option", `${item.id} · ${meeting.task_reviews[item.id]?.status || "pending"} · ${item.task}`); option.value = item.id; return option;
    }));
    this.el("review-action").value = selected; this.select();
    this.el("action-log").textContent = meeting.action_log.length ? JSON.stringify(meeting.action_log, null, 2) : "No proposals or presenter actions in this session.";
    this.el("export-results").replaceChildren(...meeting.exports.map(item => {
      const row = this.node("li", `${item.title} · ${new Date(item.created_at).toLocaleString()} · ${item.approved_count} approved tasks / ${item.decision_count} decisions`);
      row.append(this.node("p", item.directory, "export-path"));
      for (const [kind, label] of [["md", "Inspect Markdown"], ["csv", "Inspect CSV"]]) {
        const button = this.node("button", label, "button secondary"); button.type = "button";
        button.addEventListener("click", () => this.inspect(item, kind, meeting.id)); row.append(button);
      }
      return row;
    }));
    this.el("draft-follow-up").textContent = meeting.result_stale ? "Transcript changed. Run analysis again before reviewing or exporting." : "No current report preview.";
    this.el("approved-summary").textContent = "No approved tasks yet.";
    if (meeting.result && !meeting.result_stale) {
      this.api("/api/meeting/report-preview").then(data => {
        if (this.key !== key) return;
        this.el("draft-follow-up").textContent = data.draft_follow_up.join("\n\n");
        this.el("approved-summary").textContent = `${data.approved_tasks.length} approved tasks · ${data.confirmed_decisions.length} supported decisions · ${data.open_questions.length} open questions. ${data.excluded_decisions} decisions excluded for review. Missing owners/dates remain unresolved.`;
      }).catch(error => { if (this.key === key) this.el("draft-follow-up").textContent = error.message; });
    }
    this.controls();
  }
  async inspect(exported, kind, meetingId) {
    try {
      const response = await fetch(`/api/meeting/exports/${exported.id}/${kind}`, {credentials: "same-origin", cache: "no-store", headers: {"X-Session-Token": this.token}, signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw Error("Export is unavailable in the current session.");
      const text = await response.text();
      if (this.getMeeting().id !== meetingId) return;
      this.el("export-file-preview").textContent = `${exported.filenames[kind]}\n\n${text}`;
      this.el("export-inspection").open = true;
    } catch (error) { if (this.getMeeting().id === meetingId) this.el("export-file-preview").textContent = error.message; }
  }
}
