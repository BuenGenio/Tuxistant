"""Web search through the user's own SearXNG.

The server is at home but the search isn't: the query goes on to the search engines. That is why
the manifest's network permission is not "home", so after something private has been read a search
needs a click, from Skills.execute. Results are other people's text, so they reach the model
cleaned, capped and under a key that says what they are, as one string: given a list, a small model
reads it back as a numbered list.
"""
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from .realtime import _opener, _urls
from .webfetch import clean

PORT = 8888             # where our SearXNG listens, on the host of the server when none is set
TIMEOUT_S = 10
MAX_BYTES = 1 << 21     # SearXNG sends far more than is used here
MAX_QUERY_CHARS = 200
MAX_RESULTS = 4         # a small model reads out all it is given
MAX_HITS = 8            # research looks further down the list for pages it can open
MAX_TITLE_CHARS = 90
MAX_SNIPPET_CHARS = 180
MAX_SITE_CHARS = 60
USER_AGENT = "Tuxistant"

FAILED = "The search server can't be reached right now."
NO_JSON = "The search server refused that. It needs JSON output turned on."
NO_ENGINES = "The search engines aren't answering right now."
NOT_CLEAR = "I didn't catch what to search for."
NOTHING = "The search found nothing for that."


class Hit(NamedTuple):
    title: str
    site: str
    snippet: str
    url: str


def _base(config) -> str:
    if config["search_server"]:
        return _urls(config["search_server"])[0]
    return re.sub(r":\d+$", "", _urls(config["server"])[0]) + f":{PORT}"


def question(args: dict, key: str) -> str:
    return clean(str(args.get(key) or ""), MAX_QUERY_CHARS)


def _site(url: str) -> str:
    """The name to say. Taken from the address, which a page can't dress up as it can a title."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    # The end of the name, as that's whose it is: bbc.co.uk.aaaa.evil.example belongs to evil.example.
    return clean(host.removeprefix("www.")[-MAX_SITE_CHARS:], MAX_SITE_CHARS)


def domain(site: str) -> str:
    """Who owns a site, near enough: a.evil.example and b.evil.example aren't two sources."""
    labels = site.split(".")
    return ".".join(labels[-3:] if len(labels) > 2 and len(labels[-2]) <= 3 else labels[-2:])


def _hits(results) -> list[Hit]:
    hits = []
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url") or "")
        hit = Hit(clean(str(result.get("title") or ""), MAX_TITLE_CHARS), _site(url),
                  clean(str(result.get("content") or ""), MAX_SNIPPET_CHARS), url)
        if hit.site and (hit.title or hit.snippet):
            hits.append(hit)
    return hits[:MAX_HITS]


def _request(query: str, config) -> dict:
    """Through the opener with no proxies and no redirects, so the query goes only to the server
    the user set. It gets no key: that belongs to LocalAI."""
    url = f"{_base(config)}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "safesearch": 1})
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
        with _opener.open(request, timeout=TIMEOUT_S) as response:
            body = response.read(MAX_BYTES)     # cut off, it won't parse, which is the answer
    except urllib.error.HTTPError as e:
        e.close()
        raise RuntimeError(NO_JSON if e.code == 403 else FAILED) from None
    except (OSError, ValueError, http.client.HTTPException):
        raise RuntimeError(FAILED) from None
    try:
        data = json.loads(body)
    except ValueError:
        raise RuntimeError(FAILED) from None
    if not isinstance(data, dict):
        raise RuntimeError(FAILED)
    return data


def find(query: str, config) -> list[Hit]:
    data = _request(query, config)
    hits = _hits(data.get("results"))
    if not hits and data.get("unresponsive_engines"):
        raise RuntimeError(NO_ENGINES)
    return hits


def _sentence(text: str) -> str:
    return text if text[-1:] in (".", "?", "!") else f"{text}."


def _said(hit: Hit) -> str:
    return " ".join(_sentence(part) for part in (f"From {hit.site}: {hit.title}", hit.snippet)
                    if part)


def web_search(args: dict, config, _session) -> dict:
    query = question(args, "query")
    if not query:
        raise ValueError(NOT_CLEAR)
    hits = find(query, config)[:MAX_RESULTS]
    if not hits:
        return {"found": 0, "message": NOTHING}
    return {"found": len(hits), "untrusted_web_text": " ".join(map(_said, hits))}


HANDLERS = {"web.search": web_search}
