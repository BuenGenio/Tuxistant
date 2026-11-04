#!/usr/bin/python3
"""Do the translation voices really say the words? Each sample goes through the voice and back
through Whisper, then both are printed to compare.

    python3 tools/check_voices.py [voice-model ...]

Check a new voice this way, not by the length of its audio: voice-it_IT-aurora-medium had a
plausible length and turned "Vorrei un caffè" into "Whatever they're for". Whisper mishearing one
word is normal. Writes nothing and plays nothing.
"""
import asyncio
import io
import json
import sys
import urllib.request
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tuxistant import translate  # noqa: E402
from tuxistant.audio import RATE  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.realtime import _bearer, _opener, _urls, speech  # noqa: E402

WHISPER = "whisper-large-turbo-q5_0"
SAMPLES = {"uk": "Де знаходиться залізнична станція?", "es": "¿Dónde está la estación de tren?",
           "fr": "Où est la gare ?", "de": "Wo ist der Bahnhof?", "it": "Vorrei un caffè, per favore.",
           "pt": "Onde fica a estação de trem?", "ru": "Где находится вокзал?",
           "pl": "Gdzie jest dworzec kolejowy?", "nl": "Waar is het treinstation?",
           "zh": "火车站在哪里？"}


def transcribe(config, pcm: bytes) -> str:
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    mark = uuid.uuid4().hex
    body = (f'--{mark}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{WHISPER}\r\n'
            f'--{mark}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n").encode() + audio.getvalue() + (
            f"\r\n--{mark}--\r\n").encode()
    request = urllib.request.Request(
        _urls(config["server"])[0] + "/v1/audio/transcriptions", body,
        {"Authorization": _bearer(config), "Content-Type": f"multipart/form-data; boundary={mark}"})
    with _opener.open(request, timeout=60) as response:
        return json.load(response).get("text", "").strip()


async def main():
    config = Config()
    wanted = set(sys.argv[1:])
    for language, model in translate.VOICES.items():
        if wanted and model not in wanted:
            continue
        text = SAMPLES.get(language.partition("-")[0], "")
        try:
            heard = transcribe(config, await speech(config, text, model, model))
        except Exception as e:
            heard = f"FAILED: {e}"
        print(f"{model}\n   said:  {text}\n   heard: {heard}")


asyncio.run(main())
