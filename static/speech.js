"use strict";
// Only this dashboard's HTML audio element plays speech. Captured audio stays silent.
export class SpeechController {
  constructor({token, api, audio, onMeeting, onChange}, deps = {}) {
    Object.assign(this, {token, api, audio, onMeeting, onChange});
    this.fetch = deps.fetch || globalThis.fetch.bind(globalThis);
    this.urls = deps.urls || URL;
    this.delay = deps.delay || (ms => new Promise(resolve => setTimeout(resolve, ms)));
    this.run = null;
    audio.addEventListener("ended", () => this.stop("ended"));
    audio.addEventListener("error", () => {
      if (this.run && !this.run.cancelled) this.changed("Browser audio failed. Click Stop speaking before retrying.", "error");
    });
    audio.addEventListener("timeupdate", () => this.changed());
  }
  changed(message, state) {
    if (this.run) {
      if (message) this.run.message = message;
      if (state) this.run.state = state;
    }
    this.onChange({active: Boolean(this.run), playable: Boolean(this.run?.url && !this.run.cancelled && this.audio.paused),
      message: message || this.run?.message || "Select an answer and approve its exact text before speaking.",
      state: state || this.run?.state || "ready", elapsed: this.audio.currentTime || 0});
  }
  async start(selection) {
    if (this.run) return;
    const run = this.run = {cancelled: false, state: "processing", message: "Preparing local speech. Listening is paused; not transcribed."};
    this.changed();
    try {
      let meeting = await this.api("/api/meeting/speech", selection);
      run.id = meeting.speech.id;
      this.onMeeting(meeting);
      while (!run.cancelled && meeting.speech.active && meeting.speech.phase === "preparing") {
        await this.delay(400);
        meeting = await this.api("/api/meeting");
        this.onMeeting(meeting);
      }
      if (run.cancelled) { await this.finish(run, "stopped"); return; }
      if (!meeting.speech.active) { this.run = null; this.changed(meeting.speech.message, meeting.speech.state); return; }
      const response = await this.fetch(`/api/meeting/speech/${run.id}/wav`, {
        credentials: "same-origin", cache: "no-store", headers: {"X-Session-Token": this.token}, signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error("Local speech WAV is unavailable. Click Stop speaking, then try again.");
      const blob = await response.blob();
      if (run.cancelled || this.run !== run) return;
      run.url = this.urls.createObjectURL(blob);
      this.audio.src = run.url;
      this.audio.volume = 1; this.audio.muted = false;
      await this.play();
    } catch (error) {
      if (this.run === run && !run.cancelled) {
        if (!run.id) this.run = null;
        // A failed play/download does not resume ASR. The presenter can explicitly Stop.
        this.changed(error.message, "error");
      }
      if (this.run === run && run.cancelled && !run.id) { this.run = null; this.changed("Speech request did not complete. Check the session status before retrying.", "error"); }
    }
  }
  async play() {
    const run = this.run;
    if (!run?.url || run.cancelled) return;
    try {
      this.onMeeting(await this.api(`/api/meeting/speech/${run.id}/play`, {}));
      if (this.run !== run || run.cancelled) return;
      await this.audio.play();
      if (this.run === run && !run.cancelled) this.changed("Speaking through this dashboard tab. Listening paused; not transcribed.", "ready");
    } catch (error) {
      if (this.run === run && !run.cancelled) this.changed(error.name === "NotAllowedError"
        ? "Chrome blocked playback. Click Play reply below. Listening remains paused."
        : `Playback did not start: ${error.message}. Listening remains paused; retry Play or Stop speaking.`, "error");
    }
  }
  clearAudio(run) {
    this.audio.pause();
    this.audio.removeAttribute("src"); this.audio.load();
    if (run.url) { this.urls.revokeObjectURL(run.url); run.url = null; }
  }
  async finish(run, reason) {
    try {
      let meeting = await this.api("/api/meeting/speech/finish", {job_id: run.id, reason});
      this.onMeeting(meeting);
      while (meeting.speech?.id === run.id && meeting.speech.active) {
        await this.delay(400);
        meeting = await this.api("/api/meeting"); this.onMeeting(meeting);
      }
      if (this.run === run) { this.run = null; this.changed(meeting.speech?.message || "Speech stopped.", meeting.speech?.state || "ready"); }
    } catch (error) {
      if (this.run === run) this.changed(`Playback stopped locally. Cleanup is unconfirmed: ${error.message}. Click Stop speaking to retry.`, "error");
    }
  }
  async stop(reason = "stopped") {
    const run = this.run;
    if (!run) return;
    run.cancelled = true;
    this.clearAudio(run);
    this.changed("Playback stopped. Releasing temporary files and the listening pause.", "processing");
    if (run.id) {
      // Keep a short, visible paused tail for tab audio buffered by the meeting transport.
      await this.delay(500);
      await this.finish(run, reason);
    }
    // If start has not returned an ID, start() performs the cancellation handshake.
  }
  pagehide() {
    const run = this.run;
    if (!run) return;
    run.cancelled = true; this.clearAudio(run);
    if (run.id) this.fetch("/api/meeting/speech/finish", {method: "POST", credentials: "same-origin", keepalive: true,
      headers: {"X-Session-Token": this.token, "Content-Type": "application/json"},
      body: JSON.stringify({job_id: run.id, reason: "stopped"})}).catch(() => {});
  }
}
