"""Translation through LibreTranslate, spoken in the right voice.

The model never sees a translation. Its voice reads everything as English and a small model
mangles long strings, so it is told only that the job is done, and the text goes to session.after
for the app to show and say. Whatever the model calls a language is worked out here.
"""
import asyncio
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .gate import Ask, Speak, shown
from .realtime import _opener, _urls, list_models

PORT = 5000             # LibreTranslate's own, on the host of the server when none is set
TIMEOUT_S = 15
MAX_BYTES = 1 << 20
MAX_TEXT_CHARS = 2000   # already over two minutes of speech
MAX_NAME_CHARS = 40     # what the model called a language goes back to it in the error
MODELS_RETRY_S = 30
FAILED = "The translation server can't be reached right now."

# LibreTranslate code: what to call the language. Not the server's own names, which say "Ukranian"
# and "Chinese (traditional)", because the model reads this one out.
LANGUAGES = {
    "en": "English", "sq": "Albanian", "ar": "Arabic", "az": "Azerbaijani", "eu": "Basque",
    "bn": "Bengali", "bg": "Bulgarian", "ca": "Catalan", "zh-Hans": "Chinese",
    "zh-Hant": "Traditional Chinese", "cs": "Czech", "da": "Danish", "nl": "Dutch",
    "eo": "Esperanto", "et": "Estonian", "fi": "Finnish", "fr": "French", "gl": "Galician",
    "de": "German", "el": "Greek", "he": "Hebrew", "hi": "Hindi", "hu": "Hungarian",
    "id": "Indonesian", "ga": "Irish", "it": "Italian", "ja": "Japanese", "ko": "Korean",
    "ky": "Kyrgyz", "lv": "Latvian", "lt": "Lithuanian", "ms": "Malay", "nb": "Norwegian",
    "fa": "Persian", "pl": "Polish", "pt": "Portuguese", "pt-BR": "Brazilian Portuguese",
    "ro": "Romanian", "ru": "Russian", "sk": "Slovak", "sl": "Slovenian", "es": "Spanish",
    "sw": "Swahili", "sv": "Swedish", "tl": "Tagalog", "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese",
}
# Codes that are wrong but common, and the bare code for a language LibreTranslate splits in two.
OTHER_CODES = {
    "zh": "zh-Hans", "cn": "zh-Hans", "zh-cn": "zh-Hans", "zh-tw": "zh-Hant", "zh-hk": "zh-Hant",
    "ua": "uk", "jp": "ja", "kr": "ko", "no": "nb", "gr": "el", "cz": "cs", "dk": "da", "se": "sv",
}
OTHER_NAMES = {
    "mexican": "es", "castilian": "es", "espanol": "es", "español": "es", "deutsch": "de",
    "francais": "fr", "français": "fr", "italiano": "it", "portugues": "pt", "português": "pt",
    "brazilian": "pt-BR", "brazil": "pt-BR", "portuguese brazil": "pt-BR", "flemish": "nl",
    "mandarin": "zh-Hans", "taiwanese": "zh-Hant", "taiwan": "zh-Hant",
    "chinese traditional": "zh-Hant",
    "farsi": "fa", "iranian": "fa", "filipino": "tl", "bokmal": "nb", "bokmål": "nb",
    "bangla": "bn", "azeri": "az", "ukranian": "uk", "indonesia": "id", "malaysian": "ms",
}
CODES = {code.lower(): code for code in LANGUAGES} | OTHER_CODES
NAMES = {name.lower(): code for code, name in LANGUAGES.items()} | OTHER_NAMES
LOCALE = re.compile(r"([a-z]{2})-[a-z]{2,4}")

# Piper models in LocalAI, by language with no region: pt and pt-BR share one voice, and so does
# all written Chinese.
VOICES = {
    "uk": "voice-uk-lada-x-low", "es": "voice-es_ES-davefx-medium", "fr": "voice-fr-siwis-medium",
    "de": "voice-de_DE-thorsten-medium", "it": "voice-it-paola-medium",
    "pt": "voice-pt_BR-faber-medium", "ru": "voice-ru_RU-irina-medium",
    "pl": "voice-pl_PL-gosia-medium", "nl": "voice-nl_NL-ronnie-medium",
    "zh": "voice-zh_CN-huayan-medium",
}

_known: dict[str, set[str]] = {}    # models by server, for as long as the daemon runs
_retry_at = 0.0


def find_language(said: str) -> str | None:
    """The LibreTranslate code for "uk", "pt_BR", "es-MX", "Ukrainian" or "Mexican Spanish"."""
    said = said.strip().lower().replace("_", "-")
    locale = LOCALE.fullmatch(said)
    code = CODES.get(said) or (locale and CODES.get(locale[1]))
    if code:
        return code
    # Pairs first, or "Brazilian Portuguese" would be Portuguese. The last word next, because
    # the language is what the other words qualify: "Latin American Spanish", "English to Spanish".
    words = re.findall(r"[^\W\d_]+", said)
    phrases = (*map(" ".join, zip(words, words[1:])), *reversed(words))
    return next((NAMES[phrase] for phrase in phrases if phrase in NAMES), None)


def _target(args: dict) -> str:
    said = " ".join(str(args.get("to") or "").split())
    if not said:
        raise ValueError("Which language should I translate into?")
    code = find_language(said)
    if code is None:
        raise ValueError(f"I can't translate into {said[:MAX_NAME_CHARS]}.")
    return code


def _source(args: dict, target: str) -> str:
    """The server detects better than a small model guesses, so a doubtful source is left to it.
    The same language on both sides would hand the text back untranslated."""
    code = find_language(str(args.get("from") or ""))
    return code if code and code != target else "auto"


def _base(config) -> str:
    if config["translate_server"]:
        return _urls(config["translate_server"])[0]
    return re.sub(r":\d+$", "", _urls(config["server"])[0]) + f":{PORT}"


def _request(text: str, source: str, target: str, config) -> str:
    body = {"q": text, "source": source, "target": target, "format": "text"}
    try:
        request = urllib.request.Request(f"{_base(config)}/translate", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
        with _opener.open(request, timeout=TIMEOUT_S) as response:
            translated = json.loads(response.read(MAX_BYTES))["translatedText"]
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 400:
            raise RuntimeError("The translation server can't translate that into "
                               f"{LANGUAGES[target]}.") from None
        raise RuntimeError(FAILED) from None
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        raise RuntimeError(FAILED) from None
    if not isinstance(translated, str) or not translated.strip():
        raise RuntimeError(FAILED)
    return translated


def _installed(config) -> set[str]:
    """Which models the server has. Not knowing must not cost the translation, whatever went
    wrong: it only means showing the text without saying it, and asking again before long."""
    global _retry_at
    server = config["server"]
    if server not in _known and time.monotonic() >= _retry_at:
        try:
            _known[server] = set(list_models(config))
        except Exception:
            _retry_at = time.monotonic() + MODELS_RETRY_S
    return _known.get(server, set())


def _speak(text: str, target: str, config) -> Speak:
    if target == "en":
        return Speak(text, config["voice"])
    model = VOICES.get(target.partition("-")[0])
    if model and model in _installed(config):
        return Speak(text, model, model)    # Piper ignores voice, but empty means don't speak
    return Speak(text)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or url


def _stays_home(config) -> bool:
    """The manifest says home, which only holds while the translation server is the same machine
    as the voice server. Settings can point it at someone else's."""
    return _host(_base(config)) == _host(_urls(config["server"])[0])


def _translate(text: str, source: str, target: str, config, session, private=False):
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"That is too long. I can translate about {MAX_TEXT_CHARS} characters "
                         "at a time.")

    def work() -> dict:
        speak = _speak(_request(text, source, target, config), target, config)
        session.after.append(speak)
        return {"done": True, "language": LANGUAGES[target], "spoken": bool(speak.voice)}
    if (private or session.tainted) and not _stays_home(config):
        return Ask(f"Send this text to {shown(_host(_base(config)))}?", text, work)
    return work()


def translate_text(args: dict, config, session) -> dict:
    target = _target(args)
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("I didn't catch what to translate.")
    return _translate(text, _source(args, target), target, config, session)


async def translate_selection(args: dict, config, session) -> dict:
    target = _target(args)      # first: a bad language is no reason to read the selection
    text = (await session.selection()).strip()
    if not text:
        raise ValueError("Nothing is selected. Select some text and ask again.")
    # An async handler runs on the event loop, which the audio and D-Bus are waiting on.
    return await asyncio.to_thread(_translate, text, "auto", target, config, session, True)


HANDLERS = {"translate.text": translate_text, "translate.selection": translate_selection}
