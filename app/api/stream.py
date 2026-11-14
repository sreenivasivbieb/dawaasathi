"""WebSocket /v1/stream: live mic audio in; transcripts, verdicts and reply audio out.

Client -> server
  {"type":"start","session_id":?,"lang":"hi","pouch":[...],"sample_rate":16000,"channel":"web|phone"}
  binary frames: PCM s16le mono at sample_rate (any size; re-framed to Prisma's 1024 bytes)
  {"type":"image","kind":"strip|rx","data":"<base64>"}   attached to the next utterance
  {"type":"text","text":"..."}                            typed query in live mode
  {"type":"interrupt"}                                    barge-in: stop the reply audio
  {"type":"commit"}                                       close the open utterance now
Server -> client
  {"type":"ready"} {"type":"speech_start"} {"type":"transcript","text"} {"type":"result",...}
  {"type":"audio_start","audio_id","sample_rate"} binary PCM ... {"type":"audio_end","interrupted"}
  {"type":"barge_in"} {"type":"error","code","message"}

Barge-in happens two ways: the client's VAD sends "interrupt", and Prisma's own VAD
("speech_start") while reply audio is playing cancels it on the server.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.guardrails import public_response
from app.adapters.gnani_stt import language_code
from app.metrics import METRICS, log_event
from app.pipeline import QueryInput

router = APIRouter()
FRAME = 1024


@router.websocket("/v1/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    app = ws.app
    settings = app.state.settings
    pipeline = app.state.pipeline
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=15)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        await ws.close(code=1008)
        return
    if first.get("type") != "start":
        await ws.send_json({"type": "error", "code": "EXPECTED_START", "message": "send start first"})
        await ws.close(code=1008)
        return
    lang = first.get("lang") or "hi"
    sample_rate = int(first.get("sample_rate") or 16000)
    if sample_rate not in (8000, 16000, 44100, 48000):
        sample_rate = 16000
    channel = "phone" if first.get("channel") == "phone" else "stream"
    pouch = [str(p)[:80] for p in (first.get("pouch") or [])][:50]
    session = app.state.sessions.get_or_create(first.get("session_id"), lang)
    images: dict[str, bytes] = {}
    state = {"audio_task": None, "playing": False, "closed": False}
    send_lock = asyncio.Lock()

    async def send_json(obj) -> None:
        async with send_lock:
            with contextlib.suppress(Exception):
                await ws.send_json(obj)

    async def send_audio(job) -> None:
        state["playing"] = True
        interrupted = False
        await send_json({"type": "audio_start", "audio_id": job.id, "sample_rate": job.sample_rate,
                         "tts": "timbre" if job.live else "mock"})
        try:
            async for chunk in job.iter_pcm():
                async with send_lock:
                    await ws.send_bytes(chunk)
        except asyncio.CancelledError:
            interrupted = True
            app.state.audio.cancel(job.id)
        except Exception:
            interrupted = True
        finally:
            state["playing"] = False
            await send_json({"type": "audio_end", "audio_id": job.id, "interrupted": interrupted})

    def stop_audio() -> bool:
        task = state["audio_task"]
        if task and not task.done():
            task.cancel()
            METRICS.count("barge_in")
            return True
        return False

    async def handle_turn(text: str | None) -> None:
        stop_audio()
        strip, rx = images.pop("strip", None), images.pop("rx", None)
        if not text and not strip and not rx:
            return
        inp = QueryInput(session_id=session.id, lang=lang, text=text, strip=strip, rx=rx, pouch=pouch,
                         channel="phone" if channel == "phone" else "stream",
                         tts_sample_rate=8000 if channel == "phone" else None)
        resp = await pipeline.query(inp)
        s = app.state.settings
        await send_json({"type": "result", **public_response(resp.model_dump(mode="json"), s, s.expose_debug)})
        job = app.state.audio.get(resp.audio_id) if resp.audio_id else None
        if job:
            state["audio_task"] = asyncio.create_task(send_audio(job))

    await send_json({"type": "ready", "session_id": session.id,
                     "stt": "prisma" if settings.stt_live else "mock"})

    upstream = None
    if settings.stt_live:
        headers = {
            "x-api-key-id": settings.gnani_api_key,
            "language_code": language_code(settings, lang),
            "model": settings.stt_model,
            "x-sample-rate": str(sample_rate),
            "format": settings.stt_format if settings.stt_model.startswith("prisma-v3") else "transcribe",
        }
        try:
            upstream = await websockets.connect(f"{settings.gnani_ws_url}/stt/v3/stream",
                                                additional_headers=headers, open_timeout=8, max_size=2**22)
        except Exception as exc:
            log_event("stt_ws_error", session=session.id, error=type(exc).__name__)
            await send_json({"type": "error", "code": "STT_STREAM_UNAVAILABLE",
                             "message": "Live transcription unavailable; use tap-to-talk."})

    async def from_upstream() -> None:
        if upstream is None:
            return
        try:
            async for raw in upstream:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                kind = msg.get("type")
                if kind == "speech_start":
                    if state["playing"] and stop_audio():
                        await send_json({"type": "barge_in"})
                    await send_json({"type": "speech_start"})
                elif kind == "transcript":
                    text = (msg.get("text") or "").strip()
                    if text:
                        await send_json({"type": "transcript", "text": text})
                        asyncio.create_task(handle_turn(text))
                elif kind == "error":
                    await send_json({"type": "error", "code": "STT", "message": msg.get("message", "")})
        except websockets.ConnectionClosed:
            pass

    upstream_task = asyncio.create_task(from_upstream())
    buf = bytearray()
    warned = False
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if upstream is None:
                    if not warned:
                        warned = True
                        await send_json({"type": "error", "code": "STT_NOT_CONFIGURED",
                                         "message": "No GNANI_API_KEY: send text messages instead."})
                    continue
                buf.extend(msg["bytes"])
                while len(buf) >= FRAME:
                    frame = bytes(buf[:FRAME])
                    del buf[:FRAME]
                    with contextlib.suppress(websockets.ConnectionClosed):
                        await upstream.send(frame)
                continue
            try:
                data = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "interrupt":
                if stop_audio():
                    session_audio = session.current_audio_id
                    if session_audio:
                        app.state.audio.cancel(session_audio)
            elif kind == "commit" and upstream is not None:
                with contextlib.suppress(websockets.ConnectionClosed):
                    await upstream.send(json.dumps({"type": "commit"}))
            elif kind == "image" and data.get("kind") in ("strip", "rx"):
                try:
                    raw = base64.b64decode(data.get("data", ""), validate=False)
                except ValueError:
                    continue
                if 0 < len(raw) <= settings.max_image_bytes:
                    images[data["kind"]] = raw
                    await send_json({"type": "image_ok", "kind": data["kind"]})
            elif kind == "text":
                text = (data.get("text") or "").strip()[:500]
                if text or images:
                    asyncio.create_task(handle_turn(text or None))
            elif kind == "pouch":
                pouch = [str(p)[:80] for p in (data.get("pouch") or [])][:50]
            elif kind == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        stop_audio()
        upstream_task.cancel()
        if upstream is not None:
            with contextlib.suppress(Exception):
                await upstream.close()
