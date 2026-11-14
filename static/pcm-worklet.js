"use strict";
// Audio-only sink: zero outputs, no connection to the audible destination.
export class MonoPCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = Math.round(sampleRate / 10);
    this.buffer = new Float32Array(this.size);
    this.used = 0;
    this.position = 0;
    this.credits = 4;
    this.paused = false;
    this.epoch = 0;
    this.stopped = false;
    this.port.onmessage = ({data}) => {
      if (data.type === "credit") this.credits = Math.min(4, this.credits + 1);
      if (data.type === "state") { this.paused = data.paused; this.epoch = data.epoch; this.used = 0; }
      if (data.type === "stop") { this.stopped = true; this.used = 0; }
    };
  }
  process(inputs) {
    if (this.stopped) return false;
    const channels = inputs[0] || [];
    if (!channels.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let mono = 0;
      for (const channel of channels) mono += channel[i] || 0;
      this.buffer[this.used++] = Math.max(-1, Math.min(1, mono / channels.length));
      this.position++;
      if (this.used === this.size) {
        if (this.credits > 0) {
          this.credits--;
          let sum = 0;
          for (const sample of this.buffer) sum += sample * sample;
          const message = {position: this.position - this.size, frames: this.size, epoch: this.epoch,
                           rms: Math.sqrt(sum / this.size), pcm: this.paused ? null : this.buffer.buffer};
          this.port.postMessage(message, message.pcm ? [message.pcm] : []);
          this.buffer = new Float32Array(this.size);
        }
        this.used = 0;
      }
    }
    return true;
  }
}
registerProcessor("aick-mono-pcm", MonoPCMProcessor);
