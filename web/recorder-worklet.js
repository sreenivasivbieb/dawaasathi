// AudioWorkletProcessor: collects the mic input (128-sample render quanta at the device rate)
// into larger Float32 batches and posts them to the main thread, which does VAD, downsampling
// and Int16 conversion (see audio.js). Stereo input is mixed down to mono.

class DawaRecorder extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const size = (options && options.processorOptions && options.processorOptions.batch) || 1024;
    this.buf = new Float32Array(size);
    this.n = 0;
    this.alive = true;
    this.port.onmessage = (e) => {
      if (e.data === "stop") this.alive = false;
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input.length) {
      const a = input[0];
      const b = input.length > 1 ? input[1] : null;
      for (let i = 0; i < a.length; i++) {
        this.buf[this.n++] = b ? (a[i] + b[i]) * 0.5 : a[i];
        if (this.n === this.buf.length) {
          // Transfer the buffer (zero-copy) and start a fresh one.
          this.port.postMessage(this.buf, [this.buf.buffer]);
          this.buf = new Float32Array(this.buf.length);
          this.n = 0;
        }
      }
    }
    return this.alive;
  }
}

registerProcessor("dawa-recorder", DawaRecorder);
