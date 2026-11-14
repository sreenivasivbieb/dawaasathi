// Live mode (beta): thin wrapper over WebSocket /v1/stream (protocol in app/api/stream.py).
// JSON control messages both ways; binary frames are PCM s16le (mic up, reply audio down).

export class LiveLink {
  constructor({ onjson, onbinary, onclose } = {}) {
    this.h = { onjson, onbinary, onclose };
    this.ws = null;
  }

  connect(start) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/v1/stream`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => ws.send(JSON.stringify({ type: "start", ...start }));
    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        let msg;
        try { msg = JSON.parse(e.data); } catch { return; }
        this.h.onjson?.(msg);
      } else {
        this.h.onbinary?.(new Uint8Array(e.data));
      }
    };
    ws.onclose = (e) => {
      if (this.ws === ws) {
        this.ws = null;
        this.h.onclose?.(e);
      }
    };
    this.ws = ws;
  }

  get open() { return this.ws?.readyState === WebSocket.OPEN; }

  json(obj) {
    if (this.open) this.ws.send(JSON.stringify(obj));
  }

  /** Send one Int16 mic frame. Drops audio if the socket is badly backed up (> 512 KB queued). */
  pcm(int16) {
    if (!this.open || this.ws.bufferedAmount > 512 * 1024) return;
    const exact = int16.byteOffset === 0 && int16.byteLength === int16.buffer.byteLength;
    this.ws.send(exact ? int16.buffer : int16.slice().buffer);
  }

  close() {
    const ws = this.ws;
    this.ws = null;
    if (!ws) return;
    try { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" })); } catch { /* closing */ }
    ws.close();
  }
}
