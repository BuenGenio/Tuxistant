"""Weather and news for the built-in skills: read-only HTTPS requests to fixed hosts. Which news
source is yours to choose in Settings, but only from the list here.

Results go to a small model that reads them aloud, so they are short phrases rather than raw
numbers. Headlines are other people's text: they are cleaned and capped here, and the default
instructions tell the model that tool results are never commands.
"""
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from xml.etree import ElementTree

TIMEOUT_S = 6
MAX_BYTES = 1 << 20
USER_AGENT = "Tuxistant"

GEOCODER = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 7

# The news sources Settings offers. A fixed list, so a news request can only reach a host written
# here and in the manifest. All in English, because Tux's voice reads other languages as English.
# Each is (name, where its feeds live, the feed for each topic it has). Every source has "top".
SOURCES = {
    "bbc": ("BBC News", "https://feeds.bbci.co.uk/", {
        "top": "news/rss.xml", "world": "news/world/rss.xml", "uk": "news/uk/rss.xml",
        "business": "news/business/rss.xml", "politics": "news/politics/rss.xml",
        "technology": "news/technology/rss.xml",
        "science": "news/science_and_environment/rss.xml", "health": "news/health/rss.xml",
        "entertainment": "news/entertainment_and_arts/rss.xml", "sport": "sport/rss.xml"}),
    "guardian": ("The Guardian", "https://www.theguardian.com/", {
        "top": "international/rss", "world": "world/rss", "uk": "uk-news/rss",
        "business": "uk/business/rss", "politics": "politics/rss",
        "technology": "uk/technology/rss", "science": "science/rss",
        "health": "society/health/rss", "entertainment": "uk/culture/rss",
        "sport": "uk/sport/rss"}),
    "npr": ("NPR", "https://feeds.npr.org/", {
        "top": "1001/rss.xml", "world": "1004/rss.xml", "business": "1006/rss.xml",
        "politics": "1014/rss.xml", "technology": "1019/rss.xml", "science": "1007/rss.xml",
        "health": "1128/rss.xml", "entertainment": "1008/rss.xml", "sport": "1055/rss.xml"}),
    "cbc": ("CBC News", "https://www.cbc.ca/webfeed/rss/", {
        "top": "rss-topstories", "world": "rss-world", "business": "rss-business",
        "politics": "rss-politics", "technology": "rss-technology", "health": "rss-health",
        "entertainment": "rss-arts", "sport": "rss-sports"}),
    "abc": ("ABC News Australia", "https://www.abc.net.au/news/feed/", {"top": "51120/rss.xml"}),
    "dw": ("DW", "https://rss.dw.com/xml/", {
        "top": "rss-en-top", "world": "rss-en-world", "business": "rss-en-bus",
        "science": "rss-en-science", "entertainment": "rss-en-cul", "sport": "rss-en-sports"}),
    "france24": ("France 24", "https://www.france24.com/en/", {
        "top": "rss", "business": "business-tech/rss", "entertainment": "culture/rss",
        "sport": "sport/rss"}),
    "aljazeera": ("Al Jazeera", "https://www.aljazeera.com/xml/rss/", {"top": "all.xml"}),
    "kyivindependent": ("The Kyiv Independent", "https://kyivindependent.com/",
                        {"top": "news-archive/rss/"}),
}
DEFAULT_SOURCE = "bbc"
MAX_HEADLINES = 3
MAX_HEADLINE_CHARS = 160

WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
WEEKEND = {"saturday", "sunday"}
# What a small model puts in "place" when the question named only a time, or nowhere in particular.
NOT_PLACES = WEEKDAYS | {"", "here", "home", "home town", "hometown", "location", "town", "city",
                         "today", "tonight", "tomorrow", "now", "week", "weekend"}
REGION_CODES = {"uk": "gb", "usa": "us"}    # what people say, against what the geocoder returns
PLACE_FILLER = re.compile(r"^(?:(?:my|your|the|this|current)\s+)+")

# WMO weather interpretation codes, as Open-Meteo reports them.
CONDITIONS = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}


class _SameHost(urllib.request.HTTPRedirectHandler):
    """A redirect may not lead off HTTPS or to a host the request didn't name."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise urllib.error.HTTPError(newurl, code, "redirect to another host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(_SameHost)


def fetch(url: str, failure: str) -> bytes:
    """The body of url. failure is what the model says when the service can't be reached."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with OPENER.open(request, timeout=TIMEOUT_S) as response:
            body = response.read(MAX_BYTES + 1)
    except OSError as e:    # URLError, HTTPError and timeouts are all OSError
        raise RuntimeError(failure) from e
    if len(body) > MAX_BYTES:
        raise RuntimeError(failure)
    return body


def _search(town: str) -> list[dict]:
    query = urllib.parse.urlencode({"name": town.strip(), "count": 10, "language": "en",
                                    "format": "json"})
    body = fetch(f"{GEOCODER}?{query}", "The weather service can't be reached right now")
    return json.loads(body).get("results") or []


def _in_region(places: list[dict], region: str) -> dict | None:
    region = region.strip().lower()
    region = REGION_CODES.get(region, region)
    for place in places:
        if region and region in (str(place.get(key, "")).lower()
                                 for key in ("admin1", "country", "country_code")):
            return place
    return None


def find_place(name: str) -> dict | None:
    """Best match for "Town", "Town, Region" or "Town Region". The geocoder takes only a name."""
    town, _, region = name.partition(",")
    places = _search(town)
    if not places and not region and " " in town.strip():
        # No comma, so the last word is only a guess at a region. It has to match, or
        # "Atlantis Prime" would quietly report the weather for a real town called Atlantis.
        town, guess = town.strip().rsplit(" ", 1)
        return _in_region(_search(town), guess)
    return _in_region(places, region) or (places[0] if places else None)


def _place_label(place: dict) -> str:
    parts = [place.get("name"), place.get("admin1"), place.get("country")]
    return ", ".join(dict.fromkeys(part for part in parts if part))


def _day_names(days: list[str]) -> list[str]:
    return ["today", "tomorrow"][:len(days)] + [
        date.fromisoformat(day).strftime("%A") for day in days[2:]]


def _conditions(code) -> str:
    return CONDITIONS.get(code, "unknown conditions")


def _chosen_days(labels: list[str], day: str) -> list[str]:
    """A small model reads out all it is given, so it is given only what was asked about."""
    if not day:
        return labels[:2]
    if day == "week":
        return labels
    wanted = WEEKEND if day == "weekend" else {day}
    return [label for label in labels if label.lower() in wanted] or labels[:2]


def weather(args: dict, config) -> dict:
    named = " ".join(str(args.get("place") or "").split())
    name = config["location"] if PLACE_FILLER.sub("", named.lower()) in NOT_PLACES else named
    if not name:
        return {"error": "No home town is set. Ask the user which town they mean."}
    place = find_place(name)
    if place is None:
        return {"error": f"No place called {name} was found."}

    fahrenheit = config["fahrenheit"]
    query = urllib.parse.urlencode({
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
        "forecast_days": FORECAST_DAYS,
        "temperature_unit": "fahrenheit" if fahrenheit else "celsius",
        "wind_speed_unit": "mph" if fahrenheit else "kmh",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max",
    })
    body = fetch(f"{FORECAST}?{query}", "The weather service can't be reached right now")
    data = json.loads(body)
    now, daily = data["current"], data["daily"]

    scale = "Fahrenheit" if fahrenheit else "Celsius"
    wind = "miles per hour" if fahrenheit else "kilometres per hour"
    day = str(args.get("day") or "").strip().lower()
    result = {"place": _place_label(place)}
    if not day:
        result["now"] = (f"{round(now['temperature_2m'])} degrees {scale}, feels like "
                         f"{round(now['apparent_temperature'])}, "
                         f"{_conditions(now['weather_code'])}, "
                         f"wind {round(now['wind_speed_10m'])} {wind}")
    labels = _day_names(daily["time"])
    for label in _chosen_days(labels, day):
        index = labels.index(label)
        rain = daily["precipitation_probability_max"][index]
        result[label] = (f"{_conditions(daily['weather_code'][index])}, "
                         f"high {round(daily['temperature_2m_max'][index])}, "
                         f"low {round(daily['temperature_2m_min'][index])}"
                         + ("" if rain is None else f", {rain} percent chance of rain"))
    return result


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]*>", " ", html.unescape(text))
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text[:MAX_HEADLINE_CHARS]


def parse_headlines(body: bytes) -> list[str]:
    if b"<!ENTITY" in body:     # feeds never need entities, and expansion is how XML bombs work
        raise ValueError("The news feed could not be read")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        raise ValueError("The news feed could not be read") from e
    titles = (_clean(item.findtext("title") or "") for item in root.iter("item"))
    return [title for title in titles if title][:MAX_HEADLINES]


def feed_urls() -> list[str]:
    return [base + path for _name, base, feeds in SOURCES.values() for path in feeds.values()]


def _source(config) -> tuple:
    # A source this version doesn't have, from a newer config or a hand edit, is the default.
    return SOURCES[config["news_source"] if config["news_source"] in SOURCES else DEFAULT_SOURCE]


def news_hosts(config) -> list[str]:
    """The one host a news request reaches, for a card that has to name it."""
    return [urllib.parse.urlsplit(_source(config)[1]).hostname]


def headlines(args: dict, config) -> dict:
    name, base, feeds = _source(config)
    asked = str(args.get("topic") or "top").strip().lower()
    topic = asked if asked in feeds else "top"
    body = fetch(base + feeds[topic], "The news service can't be reached right now")
    # One string of sentences: given a list, the model reads it back as a numbered list.
    spoken = " ".join(t if t[-1] in ".?!" else t + "." for t in parse_headlines(body))
    result = {"source": name, "topic": topic, "headlines": spoken}
    if topic != asked:
        result["note"] = f"{name} has no {asked} feed, so these are the top stories."
    return result
