"""Stretch: WhatsApp Cloud API voice-note channel.

GET  /webhooks/whatsapp  Meta verification (hub.challenge)
POST /webhooks/whatsapp  returns 200 at once; a background task downloads the voice note (or
                         photo), runs the same pipeline and replies with an OGG/Opus voice note
                         plus a short text card. Needs WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID,
                         WHATSAPP_VERIFY_TOKEN (and optionally WHATSAPP_APP_SECRET).
Untested against Meta in this repo: the demo uses the web PWA.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import hmac
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from app.audio_io import prepare_for_prisma
from app.metrics import log_event
from app.pipeline import QueryInput
from app.tts.cache import encode

router = APIRouter()
_SESSION_KEY = secrets.token_bytes(32)  # per process; sessions are in memory anyway
GRAPH = "https://graph.facebook.com/" + os.environ.get("WHATSAPP_GRAPH_VERSION", "v21.0")


@router.get("/webhooks/whatsapp")
async def verify(request: Request):
    s = request.app.state.settings
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and s.whatsapp_verify_token and \
            q.get("hub.verify_token") == s.whatsapp_verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    return Response(status_code=403)


@router.post("/webhooks/whatsapp")
async def inbound(request: Request):
    s = request.app.state.settings
    body = await request.body()
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if secret:
        sig = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return Response(status_code=403)
    if not (s.whatsapp_token and s.whatsapp_phone_id):
        return {"ok": True, "note": "whatsapp not configured"}
    try:
        payload = await request.json()
    except ValueError:
        return {"ok": True}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                asyncio.create_task(_handle(request.app, msg))
    return {"ok": True}


async def _handle(app, msg: dict) -> None:
    s = app.state.settings
    client: httpx.AsyncClient = app.state.http
    sender = msg.get("from")
    kind = msg.get("type")
    headers = {"Authorization": f"Bearer {s.whatsapp_token}"}
    try:
        # Keyed hash: knowing someone's phone number must not give their session id.
        sid = hmac.new(_SESSION_KEY, (sender or "").encode(), hashlib.sha256).hexdigest()[:24]
        inp = QueryInput(session_id=f"wa_{sid}", lang="hi", channel="whatsapp", trusted_session=True)
        if kind in ("audio", "image"):
            media_id = msg[kind]["id"]
            meta = (await client.get(f"{GRAPH}/{media_id}", headers=headers)).json()
            data = (await client.get(meta["url"], headers=headers)).content
            if kind == "audio":
                inp.audio, inp.audio_name = await asyncio.to_thread(prepare_for_prisma, data, "note.ogg")
            else:
                inp.strip = data
        elif kind == "text":
            inp.text = msg["text"]["body"][:500]
        else:
            return
        resp = await app.state.pipeline.query(inp)
        job = app.state.audio.get(resp.audio_id)
        if job and job.task:
            await job.task
            ogg, _ = await asyncio.to_thread(encode, job.pcm(), job.sample_rate, "ogg")
            up = await client.post(
                f"{GRAPH}/{s.whatsapp_phone_id}/media", headers=headers,
                data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                files={"file": ("reply.ogg", ogg, "audio/ogg")},
            )
            audio_media = up.json().get("id")
            if audio_media:
                await client.post(f"{GRAPH}/{s.whatsapp_phone_id}/messages", headers=headers, json={
                    "messaging_product": "whatsapp", "to": sender, "type": "audio", "audio": {"id": audio_media}})
        card = resp.card
        icon = {"check": "✔", "cross": "✖", "question": "?"}[card.icon] if card else "?"
        text = f"{icon} {resp.script.text if resp.script else ''}"
        if resp.script and resp.script.chemist_text:
            text += f"\n\nकेमिस्ट से कहें: {resp.script.chemist_text}"
        await client.post(f"{GRAPH}/{s.whatsapp_phone_id}/messages", headers=headers, json={
            "messaging_product": "whatsapp", "to": sender, "type": "text", "text": {"body": text[:4000]}})
    except Exception as exc:  # never crash the webhook worker
        log_event("whatsapp_error", error=f"{type(exc).__name__}")
