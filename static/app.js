"use strict";
import {CaptureController} from "/static/capture.js";
import {SpeechController} from "/static/speech.js";
import {ReviewPanel} from "/static/review.js";
(() => {
  const tokenElement = document.querySelector('meta[name="aick-session-token"]');
  const token = tokenElement.content;
  tokenElement.remove();
  const byId = (id) => document.getElementById(id);
  const states = new Set(["not connected", "ready", "listening", "processing", "error"]);
  let meeting = null, pending = false, checking = false, expired = false, renderKey = "";
  const capture = new CaptureController({token, getMeeting: () => meeting, onMeeting: renderMeeting, onChange: renderCapture});
  let speechKey = "";
  const speech = new SpeechController({token, api, audio: byId("reply-audio"), onMeeting: renderMeeting, onChange: renderSpeech});
  const reviews = new ReviewPanel({getMeeting: () => meeting, api, mutate, token});
  // Every external/model/transcript string uses textContent, never HTML or Markdown.
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  function showState(id, state) {
    const value = states.has(state) ? state : "error";
    byId(id).textContent = value;
    byId(id).dataset.state = value;
  }
  function notice(id, message) { byId(id).textContent = message; byId(id).hidden = !message; }
  function time(seconds) {
    if (seconds === null) return "unknown";
    return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
  }
  async function api(path, body, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, { method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
        headers: { "X-Session-Token": token, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal });
      if (response.status === 403) { expired = true; throw new Error("Session expired or rejected. Reload the dashboard."); }
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request rejected (${response.status}). Check the input fields.`);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Local request timed out. Check the workflow state before retrying.");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function controls() {
    const speaking = Boolean(speech.run || meeting?.speech?.active);
    const busy = pending || expired || !meeting || meeting.state === "processing" || speaking;
    const capturing = Boolean(capture.run || meeting?.capture?.active);
    byId("new-session").disabled = busy;
    for (const id of ["add-segment", "sample-select", "wav-file", "audio-offset"]) byId(id).disabled = busy || capturing;
    byId("transcribe-wav").disabled = busy || capturing || !byId("wav-file").files.length;
    byId("import-sample").disabled = busy || capturing || !byId("sample-select").value;
    byId("listen-meeting").disabled = busy || capturing;
    byId("replay-sample").disabled = busy || capturing;
    byId("stop-listening").disabled = !capturing;
    byId("pause-listening").disabled = !capture.run?.ready || speaking;
    byId("update-board").disabled = busy || !meeting.segments.length;
    byId("ask-agent").disabled = busy || !meeting.segments.length || !byId("question").value.trim();
    byId("question-help").textContent = busy && !expired && meeting ? "Local workflow processing. This can take a few minutes." : "Answers use only included transcript evidence. No external action is available.";
    byId("speech-select").disabled = busy || !meeting?.result?.answer.length || meeting?.result_stale;
    byId("speech-approve").disabled = busy || !byId("speech-select").value || meeting?.result_stale;
    byId("speak-reply").disabled = busy || meeting?.result_stale || !byId("speech-select").value || !byId("speech-approve").checked;
    byId("stop-speaking").disabled = !speech.run;
    byId("play-reply").disabled = !speech.run?.url || speech.run.cancelled || !byId("reply-audio").paused;
    reviews.controls({busy, capturing});
  }
  function renderSpeech(current) {
    showState("speech-output-state", current.state);
    byId("speech-message").textContent = current.message;
    byId("play-reply").hidden = !current.playable;
    byId("speech-elapsed").textContent = current.active ? `Playback ${current.elapsed.toFixed(1)} s` : "";
    if (current.active) byId("speech-approve").checked = false;
    controls();
  }
  function renderSpeechSelection() {
    const key = `${meeting.id}:${meeting.result?.run_id || ""}`;
    if (key !== speechKey) {
      speechKey = key;
      byId("speech-select").replaceChildren(element("option", "Choose one visible answer…"), ...(meeting.result?.answer || []).map(item => {
        const option = element("option", `${item.id} · ${item.text.slice(0, 95)}`); option.value = item.id; return option;
      }));
      byId("speech-select").firstChild.value = "";
      byId("speech-approve").checked = false;
      byId("speech-preview").textContent = "No answer selected.";
      byId("speech-review").textContent = "Read the evidence and any review flags above before approving speech.";
    }
    const report = meeting.speech;
    byId("speech-pause").hidden = !report?.active;
    byId("speech-pause").textContent = report?.active ? `Listening paused · NOT TRANSCRIBED · since ${new Date(report.started_at).toLocaleTimeString()}${report.paused_capture_ids.length ? " · live ASR held" : " · capture cannot start during speech"}` : "";
    byId("speech-trace").textContent = report ? JSON.stringify(report, null, 2) : "No speech has been requested.";
    if (report && !speech.run) renderSpeech({state: report.state, message: report.active ? "Speech is active in another dashboard tab. Use Stop speaking in the tab playing the reply. An abandoned reply expires after five minutes and stops capture." : report.message, active: false});
    controls();
  }
  function renderCapture(current) {
    showState("capture-state", current.state);
    byId("capture-message").textContent = current.message;
    byId("input-meter").value = Math.min(1, (current.rms || 0) * 5);
    byId("input-level").textContent = current.rms > 0 ? `${Math.max(-96, 20 * Math.log10(current.rms)).toFixed(0)} dBFS` : "No input level";
    byId("pause-listening").textContent = current.paused ? "Resume transcription" : "Pause transcription";
    byId("capture-status-text").textContent = current.active ? (current.state === "processing" && !capture.run?.ready ? "Waiting for capture setup" : current.mode === "sample replay" ? "Sample replay · not a meeting" : current.paused ? "Capture paused · not transcribed" : "Tab audio capture active") : "Audio capture is off";
    const report = current.report;
    byId("capture-format").textContent = report?.sample_rate ? `${report.sample_rate.toLocaleString()} Hz mono → 16,000 Hz · 8 s windows / 1 s overlap` : "Uses the actual AudioContext sample rate";
    byId("capture-backlog").textContent = `${report?.queue_windows || 0} / 2 queued${report?.asr_processing ? " · ASR processing" : ""}`;
    byId("capture-latency").textContent = report?.asr_latency_s == null ? "No ASR result yet" : `ASR latency ${report.asr_latency_s.toFixed(2)} s · inference ${report.transcription_duration_s.toFixed(2)} s`;
    const dropped = (report?.dropped_seconds || 0) + (current.browserDropped || 0);
    byId("capture-warning").hidden = !dropped && !(report?.queue_windows > 0);
    byId("capture-warning").textContent = dropped ? `Server omissions: ${(report?.dropped_seconds || 0).toFixed(2)} s · browser drops: ${(current.browserDropped || 0).toFixed(2)} s (these can overlap). Review the marked intervals.` : "ASR backlog is building. The queue is bounded; older windows will be dropped if it fills.";
    byId("capture-release").textContent = current.active ? "All acquired tracks will be stopped on Stop." : current.tracksReleased ? `Capture stopped · ${current.releasedTrackCount || 0} tracks ended · no late results accepted` : "No capture permission is retained by the application.";
    byId("capture-gaps").replaceChildren(...(report?.intervals || []).map(gap => element("li", `${time(gap.start_s)}–${time(gap.end_s)} · Not transcribed · ${gap.reason}`)));
    byId("capture-trace").textContent = report ? JSON.stringify(report, null, 2) : "No capture has started.";
    controls();
  }
  function evidence(refs) {
    const wrapper = element("details", undefined, "claim-evidence");
    wrapper.append(element("summary", `${refs.length} source quote${refs.length === 1 ? "" : "s"}`));
    for (const ref of refs) {
      const block = element("blockquote");
      const link = element("a", ref.segment_id);
      link.href = `#segment-${encodeURIComponent(ref.segment_id)}`;
      block.append(link, element("p", ref.quote)); wrapper.append(block);
    }
    return wrapper;
  }
  function claimRow(item, kind) {
    const row = element("li", undefined, "claim-row");
    const heading = element("div", undefined, "claim-heading");
    heading.append(element("strong", item.id), element("span", kind, `claim-kind ${item.status || ""}`));
    row.append(heading, element("p", item.task || item.text, "claim-text"));
    if (item.task) {
      row.append(element("p", `Owner: ${item.owner ?? "unresolved"} · Due: ${item.due_date || item.due_text || "unresolved"}`, "claim-fields"));
      if (item.superseded_by) row.append(element("p", `Superseded by ${item.superseded_by}. Original commitment retained.`, "correction-note"));
      if (item.supersedes.length) row.append(element("p", `Explicitly replaces ${item.supersedes.join(", ")}.`, "correction-note"));
    }
    row.append(element("span", item.validation, "validation-label"));
    if (item.issues.length) { const issues = element("ul", undefined, "issues"); for (const issue of item.issues) issues.append(element("li", issue)); row.append(issues); }
    row.append(evidence(item.evidence));
    return row;
  }
  function renderMeeting(next) {
    if (meeting && next.revision < meeting.revision) return;
    const replaced = meeting && next.id !== meeting.id;
    meeting = next;
    if (replaced) {
      // Clear unsent context too: a new meeting must not inherit an old question or draft input.
      for (const id of ["question", "segment-text", "segment-speaker", "segment-end", "sample-select", "wav-file"]) byId(id).value = "";
      byId("segment-start").value = "0";
      byId("audio-offset").value = "0";
      notice("meeting-notice", "");
    }
    reviews.render(meeting);
    renderSpeechSelection();
    if (capture.run && capture.run.meetingId !== meeting.id) capture.stop("Session changed. Capture stopped and late results discarded.");
    if (!capture.run && meeting.capture?.active) {
      renderCapture({...capture.last, report: meeting.capture, active: true, state: meeting.capture.state,
        mode: meeting.capture.source_label === "Live meeting tab audio" ? "live audio" : "sample replay",
        message: "Capture is active in another dashboard tab. Stop ends it there too."});
    } else if (!capture.run && replaced) {
      capture.changed({report: null, active: false, state: "not connected", browserDropped: 0,
        message: "New empty session. Previous capture is stopped; no late results are accepted."});
    }
    controls();
    showState("analysis-state", meeting.state);
    const key = JSON.stringify([meeting.id, meeting.revision, meeting.state, meeting.result?.run_id, meeting.error, meeting.trace.length, meeting.audio_jobs, meeting.capture]);
    if (key === renderKey) return;
    renderKey = key;
    const segments = meeting.segments;
    const sourceKinds = [...new Set(segments.map(s => s.source_type === "sample file" ? "Sample transcript" : s.source_label))];
    byId("session-source").textContent = sourceKinds.join(" + ") || "Empty local session";
    byId("session-id").textContent = `${meeting.id} · revision ${meeting.revision}`;
    byId("transcript-source-note").textContent = sourceKinds.join(" + ") || "No source connected";
    byId("transcript-count").textContent = `${segments.length} segments`;
    byId("nav-count").textContent = String(segments.length);
    byId("transcript-empty").hidden = segments.length > 0;
    byId("transcript-list").replaceChildren(...segments.map(segment => {
      const row = element("li", undefined, "segment"); row.id = `segment-${segment.id}`;
      row.append(element("div", `${segment.id} · ${time(segment.start_s)} → ${time(segment.end_s)}${segment.end_inferred ? " (end inferred from next start)" : ""}`, "segment-time"),
        element("div", `${segment.source_label} · ${segment.speaker ? `${segment.speaker} (supplied label)` : "Speaker not assigned"}`, "source-label"), element("p", segment.text));
      if (segment.upload_id) row.append(element("div", `${segment.upload_id} · File offset ${segment.audio_start_s.toFixed(2)}–${segment.audio_end_s.toFixed(2)} s`, "audio-offset-label"));
      if (segment.capture_id) row.append(element("div", `${segment.capture_id} · ASR latency ${segment.asr_latency_s.toFixed(2)} s`, "audio-offset-label"));
      if (segment.overlap_note) row.append(element("p", segment.overlap_note, "notice"));
      return row;
    }));
    const job = meeting.audio_jobs.at(-1);
    byId("audio-report").hidden = !job;
    if (job) {
      showState("audio-state", job.state);
      byId("audio-message").textContent = job.message;
      const seconds = value => value === undefined ? "Pending" : `${value.toFixed(2)} s`;
      byId("audio-duration").textContent = seconds(job.audio_duration_s);
      byId("transcription-duration").textContent = seconds(job.transcription_duration_s);
      byId("audio-added").textContent = String(job.segments_added);
      byId("audio-cleanup").textContent = job.audio_deleted ? "Temporary WAV deleted" : job.state === "processing" ? "Temporary WAV in use" : "Check cleanup status";
      byId("audio-trace").textContent = JSON.stringify(meeting.audio_jobs, null, 2);
    }
    const result = meeting.result;
    const outcomes = result ? [...result.actions.map(item => claimRow(item, `${item.status} action`)), ...result.confirmed_decisions.map(item => claimRow(item, "confirmed decision")), ...result.review_decisions.map(item => claimRow(item, "decision needs review"))] : [];
    byId("outcomes-list").replaceChildren(...outcomes);
    byId("outcomes-count").textContent = `${outcomes.length} items`;
    byId("outcomes-empty").hidden = outcomes.length > 0;
    byId("stale-notice").hidden = !meeting.result_stale;
    byId("result-mode").textContent = result ? `${result.mode === "ask" ? "Answer" : "Action board"} · ${result.duration_s}s · revision ${result.revision}` : "No analysis yet";
    byId("answer-list").replaceChildren(...(result?.answer || []).map(item => claimRow(item, "answer claim")));
    byId("questions-list").replaceChildren(...(result?.unresolved_questions || []).map(item => claimRow(item, "unresolved question")));
    byId("evidence-list").replaceChildren(...(result?.evidence || []).map(ref => {
      const block = element("div", undefined, "evidence-entry");
      block.append(element("strong", `${ref.segment_id} · ${ref.source_label} · ${ref.speaker || "Unknown speaker"}`), element("blockquote", ref.quote)); return block;
    }));
    byId("scope-notice").textContent = result ? `${result.budget.notice} Input ${result.budget.input_tokens} / ${result.budget.input_budget} tokens; output cap ${result.budget.output_budget}; server limit ${result.budget.server_limit}.${result.budget.omitted_ids.length ? ` Omitted IDs: ${result.budget.omitted_ids.join(", ")}.` : ""}` : "Input budget: 4,480 tokens · output maximum: 3,200 · server limit: 8,192. Omitted context will be listed explicitly.";
    byId("scope-notice").classList.toggle("partial-context", Boolean(result?.budget.omitted_ids.length));
    byId("review-notice").textContent = result?.review_notice || "A matching quote does not prove a claim. Unsupported and ambiguous results will be flagged.";
    byId("trace-output").textContent = meeting.trace.length ? JSON.stringify(meeting.trace, null, 2) : "No workflow has run.";
    byId("history-list").replaceChildren(...meeting.history.map(old => {
      const item = element("details"); item.append(element("summary", `Revision ${old.revision} · ${old.mode} · ${old.run_id}`), element("pre", JSON.stringify(old, null, 2))); return item;
    }));
    if (meeting.error) notice("meeting-notice", meeting.error);
  }
  async function mutate(path, body, long = false) {
    if (pending || expired || !meeting) return;
    pending = true; controls(); notice("meeting-notice", "");
    if (long) showState("analysis-state", "processing");
    try { renderMeeting(await api(path, { revision: meeting.revision, ...body }, long ? 220000 : 15000)); return true; }
    catch (error) { notice("meeting-notice", error.message); try { renderMeeting(await api("/api/meeting")); } catch (_) { showState("analysis-state", "error"); } return false; }
    finally { pending = false; controls(); }
  }
  async function checkReadiness() {
    if (checking || expired) return;
    checking = true; byId("refresh").disabled = true;
    try {
      const snapshot = await api("/api/state");
      for (const name of ["llama", "whisper", "speech"]) { showState(`${name}-state`, snapshot.services[name].state); byId(`${name}-detail`).textContent = snapshot.services[name].detail; }
      const services = snapshot.services;
      byId("llama-meta").textContent = services.llama.model_alias ? `Configured alias: ${services.llama.model_alias}` : "Local endpoint only";
      byId("whisper-meta").textContent = services.whisper.state === "ready" ? (services.whisper.loaded ? "Model loaded · CPU / int8 · 4 threads" : "Files present · loads on first audio job") : "Local files required";
      byId("speech-meta").textContent = services.speech.voice ? `${services.speech.voice} · ${services.speech.culture}` : "No usable voice confirmed";
      byId("ready-count").textContent = `${Object.values(services).filter(s => s.state === "ready").length} / 3 ready`;
      showState("runtime-state", snapshot.state); showState("meeting-state", snapshot.meeting?.capture?.active && snapshot.meeting.capture.source_label === "Live meeting tab audio" ? snapshot.meeting.capture.state : snapshot.meeting_state);
      byId("checked-at").textContent = `Checked ${new Date(snapshot.checked_at).toLocaleTimeString()} · rechecks every 20 seconds`;
      byId("config-path").textContent = snapshot.configuration.path;
      byId("llama-path").textContent = services.llama.endpoint || "Not configured";
      byId("whisper-path").textContent = services.whisper.path || "Not configured";
      notice("notice", snapshot.configuration.state === "error" ? snapshot.configuration.detail : "");
      renderMeeting(snapshot.meeting);
    } catch (error) {
      for (const name of ["llama", "whisper", "speech"]) { showState(`${name}-state`, "not connected"); byId(`${name}-detail`).textContent = "Readiness is currently unverified."; byId(`${name}-meta`).textContent = "No current result"; }
      showState("runtime-state", "error"); byId("ready-count").textContent = "— / 3 ready"; notice("notice", error.message);
    } finally { checking = false; byId("refresh").disabled = expired; }
  }
  byId("refresh").addEventListener("click", checkReadiness);
  byId("speech-select").addEventListener("change", () => {
    const item = meeting?.result?.answer.find(item => item.id === byId("speech-select").value);
    byId("speech-preview").textContent = item?.text || "No answer selected.";
    byId("speech-review").textContent = item ? `${item.validation}. ${item.issues.join(" ")} ${item.text.length} / 1,000 characters. Spoken exactly as shown.` : "Read the evidence and any review flags above before approving speech.";
    byId("speech-approve").checked = false; controls();
  });
  byId("speech-approve").addEventListener("change", controls);
  byId("speak-reply").addEventListener("click", () => {
    if (!byId("speak-reply").disabled) speech.start({revision: meeting.revision, run_id: meeting.result.run_id, answer_id: byId("speech-select").value, approved: true});
  });
  byId("play-reply").addEventListener("click", () => speech.play());
  byId("stop-speaking").addEventListener("click", async () => {
    if (speech.run) await speech.stop();
  });
  window.addEventListener("pagehide", () => speech.pagehide());
  byId("question").addEventListener("input", controls);
  byId("sample-select").addEventListener("change", controls);
  byId("wav-file").addEventListener("change", controls);
  byId("transcribe-wav").addEventListener("click", async () => {
    if (pending || expired || !meeting || meeting.state === "processing") return;
    const file = byId("wav-file").files[0], offset = byId("audio-offset");
    if (!file || !offset.value || !offset.checkValidity()) { notice("meeting-notice", "Choose a WAV and a valid session offset."); return; }
    if (!file.size || file.size > 25 * 1024 * 1024) { notice("meeting-notice", "Choose a nonempty WAV of at most 25 MiB."); return; }
    pending = true; controls(); notice("meeting-notice", "");
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 35000);
    try {
      const response = await fetch(`/api/meeting/audio?revision=${meeting.revision}&offset_s=${Number(offset.value)}`, {
        method: "POST", credentials: "same-origin", headers: { "X-Session-Token": token, "Content-Type": "audio/wav" },
        body: file, signal: controller.signal });
      if (response.status === 403) expired = true;
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "WAV upload rejected.");
      byId("wav-file").value = "";
      renderMeeting(data);
    } catch (error) {
      notice("meeting-notice", error.name === "AbortError" ? "Upload timed out. Check the job state before retrying." : error.message);
      try { renderMeeting(await api("/api/meeting")); } catch (_) { showState("analysis-state", "error"); }
    } finally { clearTimeout(timer); pending = false; controls(); }
  });
  byId("import-sample").addEventListener("click", () => mutate("/api/meeting/sample", { sample: byId("sample-select").value }));
  byId("new-session").addEventListener("click", async () => { await capture.stop(); await mutate("/api/meeting/new", {}); });
  byId("listen-meeting").addEventListener("click", () => capture.start("live audio"));
  byId("replay-sample").addEventListener("click", () => capture.start("sample replay"));
  byId("stop-listening").addEventListener("click", async () => {
    if (capture.run) await capture.stop();
    else if (meeting?.capture?.active) { try { renderMeeting(await api("/api/meeting/capture/stop", {capture_id: meeting.capture.id})); } catch(error) { notice("meeting-notice", error.message); } }
  });
  byId("pause-listening").addEventListener("click", () => { (capture.run?.paused ? capture.resume() : capture.pause()).catch(error => notice("meeting-notice", error.message)); });
  window.addEventListener("pagehide", () => capture.stop("Dashboard closed. Capture stopped."));
  byId("ask-agent").addEventListener("click", () => mutate("/api/meeting/ask", { question: byId("question").value.trim() }, true));
  byId("update-board").addEventListener("click", () => mutate("/api/meeting/board", {}, true));
  byId("add-segment").addEventListener("click", async () => {
    const text = byId("segment-text").value.trim();
    const start = byId("segment-start"), end = byId("segment-end");
    if (!text || !start.value || !start.checkValidity() || !end.checkValidity()) { notice("meeting-notice", "Enter transcript text and a valid start/end time."); return; }
    if (await mutate("/api/meeting/segments", { text, start_s: Number(start.value), end_s: end.value === "" ? null : Number(end.value), speaker: byId("segment-speaker").value.trim() || null })) byId("segment-text").value = "";
  });
  controls(); checkReadiness();
  setInterval(() => { if (!document.hidden) checkReadiness(); }, 20000);
  setInterval(async () => { if ((pending || meeting?.state === "processing" || capture.run || meeting?.speech?.active) && !expired) { try { renderMeeting(await api("/api/meeting")); } catch (_) { /* mutation reports failures */ } } }, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkReadiness(); });
})();
