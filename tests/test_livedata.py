"""Weather and news skills, with the network replaced by canned responses."""
import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from tuxistant import livedata
from tuxistant.config import Config
from tuxistant.gate import Session
from tuxistant.skills import HANDLERS, Skills

ABERDEEN = {"name": "Aberdeen", "latitude": 57.1, "longitude": -2.1, "admin1": "Portugal",
            "country": "United Kingdom", "country_code": "GB"}
ABERDEEN_US = {"name": "Aberdeen", "latitude": 45.4, "longitude": -98.4,
               "admin1": "South Dakota", "country": "United States", "country_code": "US"}
FORECAST = {
    "current": {"temperature_2m": 9.4, "apparent_temperature": 7.6, "weather_code": 61,
                "wind_speed_10m": 14.2},
    "daily": {"time": ["2026-11-04", "2026-11-05", "2026-11-06"],
              "weather_code": [61, 3, 999],
              "temperature_2m_max": [11.2, 10.5, 9.0],
              "temperature_2m_min": [3.4, 2.5, 1.0],
              "precipitation_probability_max": [70, 10, None]},
}


class FakeWeb:
    """Answers the geocoder and the forecast, and remembers what was asked."""

    def __init__(self, places=(ABERDEEN,)):
        self.places = list(places)
        self.urls = []

    def __call__(self, url, _failure):
        self.urls.append(url)
        if url.startswith(livedata.GEOCODER):
            return json.dumps({"results": self.places}).encode()
        return json.dumps(FORECAST).encode()

    def query(self, index):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.urls[index]).query)


def config(location="", fahrenheit=False, news_source="bbc"):
    return {"location": location, "fahrenheit": fahrenheit, "news_source": news_source}


class Weather(unittest.TestCase):
    def run_weather(self, args, settings, web=None):
        web = web or FakeWeb()
        with mock.patch.object(livedata, "fetch", web):
            return livedata.weather(args, settings), web

    def test_reads_as_speech(self):
        result, _ = self.run_weather({"place": "Aberdeen"}, config())
        self.assertEqual(result["place"], "Aberdeen, Portugal, United Kingdom")
        self.assertEqual(result["now"], "9 degrees Celsius, feels like 8, light rain, "
                                        "wind 14 kilometres per hour")
        self.assertEqual(result["today"], "light rain, high 11, low 3, 70 percent chance of rain")
        self.assertIn("overcast", result["tomorrow"])

    def test_a_general_question_gets_only_now_today_and_tomorrow(self):
        result, _ = self.run_weather({"place": "Aberdeen"}, config())
        self.assertEqual(list(result), ["place", "now", "today", "tomorrow"])

    def test_a_named_day_gets_only_that_day(self):
        result, _ = self.run_weather({"place": "Aberdeen", "day": "Friday"}, config())
        self.assertEqual(result, {"place": "Aberdeen, Portugal, United Kingdom",
                                  "Friday": "unknown conditions, high 9, low 1"})

    def test_tomorrow_can_be_asked_for_alone(self):
        result, _ = self.run_weather({"place": "Aberdeen", "day": "tomorrow"}, config())
        self.assertEqual(list(result), ["place", "tomorrow"])

    def test_the_week_gets_every_day(self):
        result, _ = self.run_weather({"place": "Aberdeen", "day": "week"}, config())
        self.assertEqual(list(result), ["place", "today", "tomorrow", "Friday"])

    def test_a_day_past_the_forecast_falls_back_to_the_next_two(self):
        result, _ = self.run_weather({"place": "Aberdeen", "day": "sunday"}, config())
        self.assertEqual(list(result), ["place", "today", "tomorrow"])

    def test_a_time_given_as_the_place_means_the_home_town(self):
        for word in ("tomorrow", "Today", "saturday", "here", "this weekend", "your home town",
                     "your location", "my current location", "the city"):
            _, web = self.run_weather({"place": word}, config("Aberdeen"))
            self.assertEqual(web.query(0)["name"], ["Aberdeen"], word)

    def test_real_places_that_start_like_filler_are_kept(self):
        _, web = self.run_weather({"place": "The Hague"}, config("Aberdeen"))
        self.assertEqual(web.query(0)["name"], ["The Hague"])

    def test_a_time_given_as_the_place_with_no_home_town_asks(self):
        result, web = self.run_weather({"place": "tomorrow"}, config())
        self.assertIn("Ask the user", result["error"])
        self.assertEqual(web.urls, [])

    def test_a_named_place_beats_the_home_town(self):
        _, web = self.run_weather({"place": "Oslo"}, config("Aberdeen"))
        self.assertEqual(web.query(0)["name"], ["Oslo"])

    def test_home_town_is_used_when_no_place_is_named(self):
        _, web = self.run_weather({}, config("Aberdeen"))
        self.assertEqual(web.query(0)["name"], ["Aberdeen"])

    def test_no_place_at_all_asks_without_touching_the_network(self):
        result, web = self.run_weather({}, config())
        self.assertIn("Ask the user", result["error"])
        self.assertEqual(web.urls, [])

    def test_unknown_place_is_said_plainly(self):
        result, _ = self.run_weather({"place": "Nowhereville"}, config(), FakeWeb([]))
        self.assertIn("Nowhereville", result["error"])

    def test_fahrenheit_changes_the_request_and_the_words(self):
        result, web = self.run_weather({"place": "Aberdeen"}, config(fahrenheit=True))
        self.assertEqual(web.query(1)["temperature_unit"], ["fahrenheit"])
        self.assertEqual(web.query(1)["wind_speed_unit"], ["mph"])
        self.assertIn("degrees Fahrenheit", result["now"])
        self.assertIn("miles per hour", result["now"])

    def test_region_after_a_comma_chooses_between_towns(self):
        web = FakeWeb([ABERDEEN, ABERDEEN_US])
        result, _ = self.run_weather({"place": "Aberdeen, South Dakota"}, config(), web)
        self.assertEqual(result["place"], "Aberdeen, South Dakota, United States")
        self.assertEqual(web.query(0)["name"], ["Aberdeen"])

    def test_region_without_a_comma_is_retried_as_a_hint(self):
        class Picky(FakeWeb):
            def __call__(self, url, failure):
                if "Aberdeen+Portugal" in url:
                    self.urls.append(url)
                    return b"{}"
                return super().__call__(url, failure)

        result, web = self.run_weather({"place": "Aberdeen Portugal"}, config(), Picky())
        self.assertEqual(result["place"], "Aberdeen, Portugal, United Kingdom")
        self.assertEqual(web.query(1)["name"], ["Aberdeen"])

    def test_a_guessed_region_that_does_not_match_finds_nothing(self):
        class Literal(FakeWeb):
            def __call__(self, url, failure):
                if "Atlantis+Prime" in url:
                    self.urls.append(url)
                    return b"{}"
                return super().__call__(url, failure)

        atlantis = {"name": "Atlantis", "latitude": -33.5, "longitude": 18.4,
                    "admin1": "Western Cape", "country": "South Africa", "country_code": "ZA"}
        result, _ = self.run_weather({"place": "Atlantis Prime"}, config(), Literal([atlantis]))
        self.assertIn("Atlantis Prime", result["error"])

    def test_common_country_short_forms_match(self):
        class Literal(FakeWeb):
            def __call__(self, url, failure):
                if "Aberdeen+UK" in url:
                    self.urls.append(url)
                    return b"{}"
                return super().__call__(url, failure)

        web = Literal([ABERDEEN_US, ABERDEEN])
        result, _ = self.run_weather({"place": "Aberdeen UK"}, config(), web)
        self.assertEqual(result["place"], "Aberdeen, Portugal, United Kingdom")

    def test_an_unmatched_region_after_a_comma_keeps_the_best_town(self):
        result, _ = self.run_weather({"place": "Aberdeen, Narnia"}, config())
        self.assertEqual(result["place"], "Aberdeen, Portugal, United Kingdom")

    def test_place_is_encoded_not_pasted_into_the_url(self):
        _, web = self.run_weather({"place": "x&latitude=0#"}, config())
        self.assertNotIn("latitude", urllib.parse.urlparse(web.urls[0]).query.split("&")[1:2])
        self.assertEqual(web.query(0)["name"], ["x&latitude=0#"])


class Headlines(unittest.TestCase):
    def feed(self, *titles):
        items = "".join(f"<item><title>{title}</title></item>" for title in titles)
        return f"<rss><channel><title>Feed</title>{items}</channel></rss>".encode()

    def test_only_item_titles_and_only_the_first_few(self):
        titles = [f"Story {n}" for n in range(9)]
        self.assertEqual(livedata.parse_headlines(self.feed(*titles)),
                         titles[:livedata.MAX_HEADLINES])

    def test_markup_and_control_characters_are_removed(self):
        body = self.feed("&lt;b&gt;Big&lt;/b&gt; &amp;  news", "Line&#10;break", "&lt;i&gt;&lt;/i&gt;")
        self.assertEqual(livedata.parse_headlines(body), ["Big & news", "Line break"])

    def test_long_headlines_are_cut(self):
        title = livedata.parse_headlines(self.feed("x" * 1000))[0]
        self.assertEqual(len(title), livedata.MAX_HEADLINE_CHARS)

    def test_entity_declarations_are_refused(self):
        bomb = b'<!DOCTYPE r [<!ENTITY a "aaaa">]><rss><item><title>&a;</title></item></rss>'
        with self.assertRaisesRegex(ValueError, "could not be read"):
            livedata.parse_headlines(bomb)

    def test_broken_feed_is_a_plain_error(self):
        with self.assertRaisesRegex(ValueError, "could not be read"):
            livedata.parse_headlines(b"<rss><item>")

    def test_topic_picks_the_feed(self):
        with mock.patch.object(livedata, "fetch", return_value=self.feed("A")) as fetch:
            result = livedata.headlines({"topic": "Technology"}, config())
        self.assertEqual(fetch.call_args.args[0],
                         "https://feeds.bbci.co.uk/news/technology/rss.xml")
        self.assertEqual(result, {"source": "BBC News", "topic": "technology", "headlines": "A."})

    def test_headlines_arrive_as_sentences_not_a_list(self):
        with mock.patch.object(livedata, "fetch", return_value=self.feed("One.", "Two", "Three?")):
            result = livedata.headlines({}, config())
        self.assertEqual(result["headlines"], "One. Two. Three?")

    def test_unknown_topic_says_what_it_gave_instead(self):
        with mock.patch.object(livedata, "fetch", return_value=self.feed("A")) as fetch:
            result = livedata.headlines({"topic": "gardening"}, config())
        self.assertEqual(fetch.call_args.args[0], "https://feeds.bbci.co.uk/news/rss.xml")
        self.assertIn("gardening", result["note"])

    def test_the_chosen_source_is_the_one_read(self):
        with mock.patch.object(livedata, "fetch", return_value=self.feed("A")) as fetch:
            result = livedata.headlines({"topic": "sport"}, config(news_source="npr"))
        self.assertEqual(fetch.call_args.args[0], "https://feeds.npr.org/1055/rss.xml")
        self.assertEqual(result["source"], "NPR")

    def test_a_topic_the_source_lacks_gives_its_top_stories_and_says_so(self):
        with mock.patch.object(livedata, "fetch", return_value=self.feed("A")) as fetch:
            result = livedata.headlines({"topic": "uk"}, config(news_source="aljazeera"))
        self.assertEqual(fetch.call_args.args[0], "https://www.aljazeera.com/xml/rss/all.xml")
        self.assertEqual(result["topic"], "top")
        self.assertEqual(result["note"], "Al Jazeera has no uk feed, so these are the top stories.")

    def test_a_source_this_version_lacks_falls_back_to_the_default(self):
        with mock.patch.object(livedata, "fetch", return_value=self.feed("A")) as fetch:
            result = livedata.headlines({}, config(news_source="a-feed-from-the-future"))
        self.assertEqual(fetch.call_args.args[0], "https://feeds.bbci.co.uk/news/rss.xml")
        self.assertEqual(result["source"], "BBC News")

    def test_every_source_is_https_has_top_stories_and_only_known_topics(self):
        topics = set(Skills().actions["news.headlines"]["params"]["topic"]["enum"])
        for source, (name, base, feeds) in livedata.SOURCES.items():
            with self.subTest(source):
                self.assertTrue(name and base.startswith("https://") and base.endswith("/"))
                self.assertIn("top", feeds)
                self.assertLessEqual(set(feeds), topics)
        self.assertIn(livedata.DEFAULT_SOURCE, livedata.SOURCES)


class Fetch(unittest.TestCase):
    def open_with(self, **kwargs):
        return mock.patch.object(livedata.OPENER, "open", **kwargs)

    def redirect(self, location):
        handler = livedata._SameHost()
        request = urllib.request.Request("https://www.theguardian.com/business/rss")
        return handler.redirect_request(request, None, 301, "Moved", {}, location)

    def test_a_redirect_within_the_host_is_followed(self):
        followed = self.redirect("https://www.theguardian.com/uk/business/rss")
        self.assertEqual(followed.full_url, "https://www.theguardian.com/uk/business/rss")

    def test_a_redirect_to_another_host_or_off_https_is_refused(self):
        for location in ("https://evil.example/feed", "http://www.theguardian.com/uk/business/rss",
                         "https://www.theguardian.com.evil.example/x", "http://127.0.0.1:8080/"):
            with self.subTest(location), self.assertRaises(urllib.error.HTTPError) as refused:
                self.redirect(location)
            refused.exception.close()

    def test_a_refused_redirect_is_only_the_spoken_message(self):
        error = urllib.error.HTTPError("https://evil.example/", 301, "redirect", {}, None)
        self.addCleanup(error.close)
        with self.open_with(side_effect=error), self.assertRaises(RuntimeError) as caught:
            livedata.fetch("https://feeds.npr.org/1001/rss.xml", "Not reachable")
        self.assertEqual(str(caught.exception), "Not reachable")

    def test_fetch_goes_through_the_guarded_opener(self):
        self.assertTrue(any(isinstance(h, livedata._SameHost) for h in livedata.OPENER.handlers))

    def test_unreachable_service_gives_only_the_spoken_message(self):
        error = urllib.error.URLError("Name or service not known: api.open-meteo.com")
        with self.open_with(side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                livedata.fetch("https://api.open-meteo.com/", "Not reachable")
        self.assertEqual(str(caught.exception), "Not reachable")

    def test_timeouts_count_as_unreachable(self):
        with self.open_with(side_effect=TimeoutError()):
            with self.assertRaises(RuntimeError):
                livedata.fetch("https://api.open-meteo.com/", "Not reachable")

    def test_oversized_bodies_are_refused(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"x" * (livedata.MAX_BYTES + 1)
        with self.open_with(return_value=response):
            with self.assertRaises(RuntimeError):
                livedata.fetch("https://api.open-meteo.com/", "Not reachable")

    def test_every_request_has_a_timeout(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with self.open_with(return_value=response) as urlopen:
            livedata.fetch("https://api.open-meteo.com/", "Not reachable")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], livedata.TIMEOUT_S)


class Wiring(unittest.TestCase):
    def test_every_builtin_action_has_a_handler(self):
        self.assertEqual(set(Skills().actions), set(HANDLERS))

    def test_network_skills_are_not_low_risk(self):
        skills = Skills()
        for skill_id in ("org.tuxistant.weather", "org.tuxistant.news"):
            self.assertEqual(skills.skills[skill_id]["risk"], "medium")

    def test_declared_hosts_are_the_hosts_used(self):
        skills = Skills()
        declared = (skills.skills["org.tuxistant.weather"]["permissions"]["network"]
                    + skills.skills["org.tuxistant.news"]["permissions"]["network"])
        used = {urllib.parse.urlparse(url).hostname
                for url in (livedata.GEOCODER, livedata.FORECAST, *livedata.feed_urls())}
        self.assertEqual(set(declared), used)

    def test_the_tool_reads_the_live_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Config(Path(folder) / "config.json")
            skills = Skills(config=settings)
            settings.set("location", "Aberdeen")
            web = FakeWeb()
            with mock.patch.object(livedata, "fetch", web):
                name, result = asyncio.run(skills.execute("weather_forecast", "{}", Session(skills)))
        self.assertEqual(name, "weather.forecast")
        self.assertEqual(web.query(0)["name"], ["Aberdeen"])
        self.assertIn("now", result)

    def test_a_failed_service_reaches_the_model_as_words(self):
        with mock.patch.object(livedata.OPENER, "open", side_effect=OSError("boom")):
            skills = Skills()
            _, result = asyncio.run(skills.execute("news_headlines", "{}", Session(skills)))
        self.assertEqual(result, {"error": "The news service can't be reached right now"})


class NewsSetting(unittest.TestCase):
    def test_only_a_listed_source_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Config(Path(folder) / "config.json")
            self.assertEqual(settings["news_source"], "bbc")
            settings.set("news_source", "kyivindependent")
            self.assertEqual(Config(Path(folder) / "config.json")["news_source"], "kyivindependent")
            for bad in ("https://evil.example/feed", "", "BBC"):
                with self.subTest(bad), self.assertRaisesRegex(ValueError, "news sources"):
                    settings.set("news_source", bad)

    def test_a_card_names_only_the_source_it_will_reach(self):
        asked = []

        async def click(ask):
            asked.append(ask)
            return False

        with tempfile.TemporaryDirectory() as folder:
            settings = Config(Path(folder) / "config.json")
            skills = Skills(config=settings)
            settings.set("news_source", "npr")
            session = Session(skills, click)
            session.tainted = True      # something private was read earlier in the conversation
            with mock.patch.object(livedata, "fetch") as fetch:
                _, result = asyncio.run(skills.execute("news_headlines", "{}", session))
        self.assertEqual(asked[0].title, "Let org.tuxistant.news reach feeds.npr.org?")
        self.assertTrue(result["declined"])
        fetch.assert_not_called()

    def test_the_tool_reads_the_chosen_source(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Config(Path(folder) / "config.json")
            skills = Skills(config=settings)
            settings.set("news_source", "cbc")
            feed = b"<rss><channel><item><title>A</title></item></channel></rss>"
            with mock.patch.object(livedata, "fetch", return_value=feed) as fetch:
                _, result = asyncio.run(skills.execute("news_headlines", "{}", Session(skills)))
        self.assertEqual(fetch.call_args.args[0], "https://www.cbc.ca/webfeed/rss/rss-topstories")
        self.assertEqual(result["source"], "CBC News")


class LocationSetting(unittest.TestCase):
    def test_spacing_is_tidied_and_length_capped(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Config(Path(folder) / "config.json")
            settings.set("location", "  Lisbon ,\n  Portugal ")
            self.assertEqual(settings["location"], "Lisbon , Portugal")
            settings.set("location", "x" * 500)
            self.assertEqual(len(settings["location"]), 100)

    def test_it_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Config(Path(folder) / "config.json")
            settings.set("location", "Aberdeen")
            settings.set("location", "")
            self.assertEqual(settings["location"], "")


if __name__ == "__main__":
    unittest.main()
