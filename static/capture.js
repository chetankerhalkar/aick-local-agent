"use strict";
export class CaptureController {
  constructor({token, getMeeting, onMeeting, onChange}, dependencies = {}) {
    Object.assign(this, {token, getMeeting, onMeeting, onChange});
    this.dependencies = {
      getDisplayMedia: options => navigator.mediaDevices.getDisplayMedia(options),
      createContext: () => new AudioContext(),
      createWorklet: context => new AudioWorkletNode(context, "aick-mono-pcm", {numberOfInputs: 1, numberOfOutputs: 0}),
      audioStream: tracks => new MediaStream(tracks),
      createSocket: () => new WebSocket("ws://127.0.0.1:8765/api/meeting/live"),
      fetch: (...args) => fetch(...args), ...dependencies
    };
    this.run = null;
    this.generation = 0;
    this.last = {active: false, state: "not connected", message: "Capture is off. Choose the meeting under Chrome Tab and enable Share tab audio."};
  }
  changed(extra = {}) {
    this.last = {...this.last, ...extra};
    this.onChange(this.last);
  }
  async start(mode = "live audio") {
    if (this.run) return;
    const meeting = this.getMeeting();
    const run = {generation: ++this.generation, meetingId: meeting.id, mode, stream: null, context: null,
      source: null, node: null, socket: null, position: 0, epoch: 0, ready: false, paused: false,
      lastPacket: performance.now(), browserDropped: 0};
    this.run = run;
    this.changed({active: true, state: "processing", mode, message: mode === "live audio" ?
      "Choose Chrome Tab → your meeting → Share tab audio in Chrome’s picker." : "Sample replay: testing the local audio pipeline. This is not a live meeting.", tracksReleased: false, rms: 0});
    try {
      if (mode === "live audio") {
        // Called directly from the user's click, before any await or server request.
        const stream = await this.dependencies.getDisplayMedia({video: true, audio: true,
          preferCurrentTab: false, selfBrowserSurface: "exclude", systemAudio: "exclude",
          monitorTypeSurfaces: "exclude", surfaceSwitching: "exclude"});
        run.stream = stream;
        if (this.run !== run) { stream.getTracks().forEach(track => track.stop()); return; }
        if (!stream.getAudioTracks().length) throw new Error("No tab audio was shared. All tracks stopped. Retry: select Chrome Tab, choose the meeting, enable Share tab audio, then click Share.");
        const video = stream.getVideoTracks()[0];
        if (!video || video.getSettings().displaySurface !== "browser") throw new Error("A browser tab is required. All tracks stopped. Retry in Chrome’s picker using Chrome Tab and Share tab audio.");
        // Video is retained only for capture lifetime. Never attach it to any processor or element.
        for (const track of stream.getTracks()) track.addEventListener("ended", () => {
          if (this.run === run) this.stop("Chrome ended tab sharing. All tracks stopped.");
        });
        for (const track of stream.getAudioTracks()) {
          track.addEventListener("mute", () => { if (this.run === run) this.pause("muted capture").catch(() => {}); });
          track.addEventListener("unmute", () => { if (this.run === run && run.pauseReason === "muted capture") this.resume().catch(() => {}); });
        }
      }
      run.context = this.dependencies.createContext();
      await run.context.resume();
      if (this.run !== run) { await run.context.close(); return; }
      await run.context.audioWorklet.addModule("/static/pcm-worklet.js");
      if (this.run !== run) return;
      run.node = this.dependencies.createWorklet(run.context);
      run.node.onprocessorerror = () => { if (this.run === run) this.stop("Audio worklet failed. Capture stopped; retry from the picker.", true); };
      if (mode === "live audio") {
        run.source = run.context.createMediaStreamSource(this.dependencies.audioStream(run.stream.getAudioTracks()));
      } else {
        const response = await this.dependencies.fetch("/api/samples/speech-smoke.wav", {headers: {"X-Session-Token": this.token}, credentials: "same-origin"});
        if (!response.ok) throw new Error("Prepared WAV replay is unavailable.");
        const buffer = await run.context.decodeAudioData(await response.arrayBuffer());
        if (this.run !== run) return;
        run.source = run.context.createBufferSource();
        const padded = run.context.createBuffer(buffer.numberOfChannels,
          buffer.length + Math.ceil(8 * buffer.sampleRate), buffer.sampleRate);
        for (let channel = 0; channel < buffer.numberOfChannels; channel++) padded.copyToChannel(buffer.getChannelData(channel), channel);
        run.source.buffer = padded;
        // Tail silence lets the final sentence reach a full eight-second window.
        run.source.onended = () => { if (this.run === run) { run.replayEnded = true; this.changed({message: "Sample replay ended; processing the final window. Stop when the last segment appears."}); } };
      }
      run.socket = this.dependencies.createSocket();
      run.socket.binaryType = "arraybuffer";
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("Local capture handshake timed out.")), 8000);
        run.handshakeReject = error => { clearTimeout(timer); reject(error); };
        run.socket.onopen = () => {
          if (this.run !== run) return;
          run.socket.send(JSON.stringify({type: "start", token: this.token, revision: meeting.revision,
            meeting_id: meeting.id, sample_rate: run.context.sampleRate, mode}));
        };
        run.socket.onmessage = event => {
          if (this.run !== run || this.getMeeting().id !== run.meetingId) return;
          let message;
          try { message = JSON.parse(event.data); } catch (_) { this.stop("Invalid local capture response.", true); return; }
          if (message.type === "error") { run.handshakeReject(new Error(message.message)); this.stop(message.message, true); return; }
          if (message.type === "ready") {
            run.captureId = message.capture_id; run.epoch = message.epoch; run.ready = true;
            clearTimeout(timer); resolve();
          }
          if (message.capture_id !== run.captureId) return;
          if (message.type === "control") {
            run.epoch = message.epoch;
            if (run.pendingControl?.type === message.control) run.pendingControl.resolve();
            // An earlier ping response cannot acknowledge a later pause or reopen ingestion.
            run.paused = Boolean(message.meeting.capture.paused || run.pendingControl?.type === "pause");
            run.node.port.postMessage({type: "state", paused: run.paused, epoch: run.epoch});
          }
          if (message.meeting) {
            this.onMeeting(message.meeting);
            this.changed({report: message.meeting.capture, state: message.meeting.capture.state,
              paused: run.paused, message: run.pendingControl?.type === "pause" ? "Pausing: waiting for the local acknowledgement. Incoming audio is not transcribed." : message.meeting.capture.message});
            if (!message.meeting.capture.active) this.stop(message.meeting.capture.message, message.meeting.capture.state === "error");
          }
        };
        run.socket.onerror = () => { run.handshakeReject(new Error("Local WebSocket failed.")); if (this.run === run) this.stop("Local WebSocket failed. Capture stopped.", true); };
        run.socket.onclose = () => { run.handshakeReject(new Error("Local WebSocket closed.")); if (this.run === run) this.stop("Local capture connection ended. All tracks stopped.", true); };
      });
      if (this.run !== run) return;
      run.node.port.onmessage = ({data}) => {
        if (this.run !== run) return;
        run.node.port.postMessage({type: "credit"});
        run.position = data.position + data.frames;
        run.lastPacket = performance.now();
        this.changed({rms: run.paused ? 0 : data.rms, browserDropped: run.browserDropped});
        if (!run.ready || run.paused || !data.pcm || data.epoch !== run.epoch) return;
        if (run.socket.readyState !== 1 || run.socket.bufferedAmount > 256 * 1024) {
          run.browserDropped += data.frames / run.context.sampleRate;
          this.changed({message: "Browser audio dropped: the local connection is falling behind. See the marked gap.", browserDropped: run.browserDropped});
          return;
        }
        const packet = new ArrayBuffer(12 + data.pcm.byteLength), header = new DataView(packet);
        header.setUint32(0, run.epoch, true); header.setFloat64(4, data.position, true);
        new Uint8Array(packet, 12).set(new Uint8Array(data.pcm));
        run.socket.send(packet);
      };
      run.node.port.postMessage({type: "state", epoch: run.epoch, paused: false});
      run.source.connect(run.node); // Sink has zero outputs; no captured audio reaches speakers.
      if (mode === "sample replay") run.source.start();
      run.lastPacket = performance.now();
      run.timer = setInterval(() => {
        if (this.run !== run) return;
        if (performance.now() - run.lastPacket > 8000 || run.context.state !== "running") {
          const replayFinished = run.mode === "sample replay" && run.replayEnded;
          this.stop(replayFinished ? "Sample replay finished. Audio processing stopped; pending audio cleared." :
            "Audio processing stopped or was suspended. Capture ended; retry to avoid an unmarked gap.", !replayFinished); return;
        }
        if (run.socket.readyState === 1) run.socket.send(JSON.stringify({type: "ping", position: run.position}));
      }, 2000);
      this.changed({state: "listening", sampleRate: run.context.sampleRate, paused: false,
        message: mode === "live audio" ? "Listening to the shared meeting tab. No audio is played back." : "Sample replay is running silently through the audio worklet. Not live meeting audio."});
    } catch (error) {
      if (this.run === run) await this.stop(error.name === "NotAllowedError" ? "Sharing was cancelled or denied. Retry Listen to meeting and choose Chrome Tab with Share tab audio." : error.message, true);
    }
  }
  async pause(reason = "user pause") {
    const run = this.run;
    if (!run?.ready || run.paused) return;
    run.paused = true; run.pauseReason = reason;
    run.node.port.postMessage({type: "state", paused: true, epoch: run.epoch});
    this.changed({paused: true, rms: 0, message: `Paused for ${reason}. Incoming audio is not transcribed.`});
    await this.control(run, {type: "pause", position: run.position, reason});
  }
  async resume() {
    const run = this.run;
    if (!run?.ready || !run.paused) return;
    await this.control(run, {type: "resume", position: run.position});
  }
  async control(run, message) {
    try {
      // Serialize controls so a rapid resume cannot overwrite the pending pause callback.
      while (run.pendingControl) await run.pendingControl.promise;
      if (this.run !== run) throw new Error("Capture stopped");
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      const pending = {type: message.type, promise};
      const finish = error => {
        clearTimeout(pending.timer);
        if (run.pendingControl === pending) run.pendingControl = null;
        if (error) reject(error); else resolve();
      };
      pending.resolve = () => finish();
      pending.reject = finish;
      pending.timer = setTimeout(() => finish(new Error("Pause/resume acknowledgement timed out.")), 5000);
      run.pendingControl = pending;
      try { run.socket.send(JSON.stringify(message)); } catch (error) { finish(error); }
      await promise;
    } catch (error) { if (this.run === run) await this.stop(error.message, true); throw error; }
  }
  // Future speech must await this before starting output. No speech is generated here.
  async pauseForSpeech() { await this.pause("speech output"); }
  async resumeAfterSpeech() { if (this.run?.pauseReason === "speech output") await this.resume(); }
  async stop(message = "Stopped. All tracks released; queued audio cleared.", error = false) {
    const run = this.run;
    if (!run) return;
    this.run = null;
    const stoppedGeneration = ++this.generation;
    clearInterval(run.timer);
    if (run.pendingControl) run.pendingControl.reject(new Error("Capture stopped"));
    if (run.handshakeReject) run.handshakeReject(new Error("Capture stopped"));
    if (run.node) { run.node.port.onmessage = null; run.node.port.postMessage({type: "stop"}); run.node.disconnect(); }
    if (run.source) { run.source.disconnect(); if (run.mode === "sample replay") { try { run.source.stop(); } catch (_) {} } }
    const tracks = run.stream?.getTracks() || [];
    tracks.forEach(track => track.stop());
    const tracksReleased = tracks.every(track => track.readyState === "ended");
    const closing = run.context ? run.context.close().catch(() => {}) : Promise.resolve();
    if (run.socket?.readyState === 1) run.socket.send(JSON.stringify({type: "stop", position: run.position}));
    if (run.socket) run.socket.close();
    this.changed({active: false, paused: false, rms: 0, state: error ? "error" : "not connected", message,
      tracksReleased, releasedTrackCount: tracks.length, report: {...this.last.report, active: false, queue_windows: 0}});
    if (run.captureId) {
      try {
        const response = await this.dependencies.fetch("/api/meeting/capture/stop", {method: "POST", credentials: "same-origin",
          signal: AbortSignal.timeout(5000),
          headers: {"Content-Type": "application/json", "X-Session-Token": this.token}, body: JSON.stringify({capture_id: run.captureId})});
        if (!response.ok) throw new Error("Stop acknowledgement failed");
        const snapshot = await response.json();
        if (this.generation === stoppedGeneration && this.getMeeting().id === run.meetingId) this.onMeeting(snapshot);
      } catch (_) { if (this.generation === stoppedGeneration) this.changed({message: message + " Server acknowledgement unavailable; socket closure also invalidates the capture."}); }
    }
    await closing;
  }
}
