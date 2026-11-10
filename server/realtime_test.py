#!/usr/bin/env python3
"""End-to-end check of a LocalAI realtime pipeline over WebSocket.

Speaks a prompt via Kokoro, streams it into /v1/realtime as one user turn, and reports
transcript, tool calls, reply text, audio received and time-to-first-audio.

  LOCALAI_API_KEY=... python3 realtime_test.py [--host localhost:8080] [--model tux-realtime]
"""
import argparse, array, asyncio, base64, io, json, os, time, urllib.request, wave

import websockets

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="localhost:8080")
ap.add_argument("--model", default="tux-realtime")
ap.add_argument("--say", default="Please set a timer for twenty minutes.")
ap.add_argument("--out", default="reply.wav")
ap.add_argument("--tail", type=float, default=1.0, help="seconds of trailing silence (a mic keeps streaming)")
args = ap.parse_args()
KEY = os.environ["LOCALAI_API_KEY"]
RATE = 24000


def tts(text: str) -> bytes:
    req = urllib.request.Request(
        f"http://{args.host}/v1/audio/speech",
        data=json.dumps({"model": "tux-voice", "input": text}).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r, wave.open(io.BytesIO(r.read())) as w:
        pcm, ch, sw, sr = w.readframes(w.getnframes()), w.getnchannels(), w.getsampwidth(), w.getframerate()
    assert sw == 2, f"expected 16-bit TTS output, got {sw * 8}-bit"
    a = array.array("h", pcm)
    if ch == 2:
        a = array.array("h", ((a[i] + a[i + 1]) // 2 for i in range(0, len(a), 2)))
    if sr != RATE:  # linear resample to 24 kHz
        n = int(len(a) * RATE / sr)
        a = array.array("h", (a[min(int(i * sr / RATE), len(a) - 1)] for i in range(n)))
    pcm = a.tobytes()
    return pcm


async def main():
    speech = tts(args.say) + b"\x00\x00" * int(RATE * args.tail)  # trailing silence ends the turn
    url = f"ws://{args.host}/v1/realtime?model={args.model}"
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {KEY}"},
                                  max_size=None, open_timeout=300) as ws:
        t0 = time.monotonic()
        # The server loads VAD/STT/LLM/TTS before announcing the session; audio sent
        # earlier is not reliably turn-detected, so wait for it (slow only when cold).
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), 300))
            if ev["type"] == "session.created":
                print(f"session ready after {time.monotonic()-t0:.1f}s")
                break
            if ev["type"] == "error":
                raise SystemExit(f"session failed: {ev}")
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": "You are Tux, a concise desktop voice assistant. Use tools when they fit.",
            "tools": [{"type": "function", "name": "set_timer", "description": "Start a countdown timer",
                       "parameters": {"type": "object", "properties": {"minutes": {"type": "integer"}},
                                      "required": ["minutes"]}}],
        }}))
        chunk = RATE * 2 // 10  # 100 ms
        for i in range(0, len(speech), chunk):
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(speech[i:i + chunk]).decode()}))
            await asyncio.sleep(0.1)  # real-time pacing
        t_sent = time.monotonic()

        audio, first_audio, reply, answered = bytearray(), None, "", False
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), 120))
            t = ev["type"]
            if t == "error":
                print("ERROR", ev); break
            if t == "conversation.item.input_audio_transcription.completed":
                print(f"[{time.monotonic()-t_sent:5.2f}s] heard: {ev.get('transcript')!r}")
            elif t == "response.function_call_arguments.done":
                print(f"[{time.monotonic()-t_sent:5.2f}s] tool call: {ev.get('name')}({ev.get('arguments')})")
                if not answered:  # act like the daemon: return the tool result, ask for a reply
                    answered = True
                    await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                        "type": "function_call_output", "call_id": ev["call_id"],
                        "output": json.dumps({"ok": True, "ends_at": "21:20"})}}))
                    await ws.send(json.dumps({"type": "response.create"}))
            elif t in ("response.output_audio.delta", "response.audio.delta"):
                if first_audio is None:
                    first_audio = time.monotonic() - t_sent
                    print(f"[{first_audio:5.2f}s] first audio")
                audio += base64.b64decode(ev["delta"])
            elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                reply = ev.get("transcript", "")
                print(f"[{time.monotonic()-t_sent:5.2f}s] reply: {reply!r}")
            elif t == "response.done" and audio:
                break
        with wave.open(args.out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(bytes(audio))
        print(f"session+upload {t_sent-t0:.1f}s | reply audio {len(audio)/2/RATE:.1f}s -> {args.out}")

asyncio.run(main())
