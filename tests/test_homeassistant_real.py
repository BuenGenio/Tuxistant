"""The Home Assistant skill against a real Home Assistant, in Docker, with its demo integration.

    TUX_HA_REAL=1 python3 -m unittest tests.test_homeassistant_real

Skipped without that, so the normal suite never starts Docker. The container has a name of its
own and no volume, its port is published on 127.0.0.1 only, and it is removed at the end whatever
happened. It is set up the way a person would, through the onboarding API: a user, then a
long-lived token, which only the WebSocket API can make. Nothing here reaches any other address.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import homeassistant  # noqa: E402
from tuxistant.gate import Ask  # noqa: E402

IMAGE = "ghcr.io/home-assistant/home-assistant:stable"
START_S = 300
# No default_config: that would bring discovery, which looks around the network it is on.
CONFIGURATION = """\
frontend:
demo:
input_boolean:
  guest_mode:
    name: Guest mode
scene:
  - name: Tux evening
    entities:
      light.bed_light: "on"
script:
  tux_goodnight:
    alias: Tux goodnight
    sequence:
      - action: light.turn_off
        target:
          entity_id: light.kitchen_lights
"""
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def docker(*args, check=True) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=600)
    if check and done.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def ask(url, body=None, form=False, token=None):
    """(status, JSON) straight from the server, so a test can see for itself what happened."""
    data = None if body is None else (
        urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with OPENER.open(urllib.request.Request(url, data, headers), timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, None


@unittest.skipUnless(os.environ.get("TUX_HA_REAL") == "1",
                     "set TUX_HA_REAL=1 to start a real Home Assistant in Docker")
class RealHomeAssistant(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker isn't installed")
        cls.name = "tuxistant-ha-test-" + secrets.token_hex(4)
        docker("create", "--name", cls.name, "-p", "127.0.0.1::8123", IMAGE)
        cls.addClassCleanup(docker, "rm", "--force", "--volumes", cls.name, check=False)
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "configuration.yaml").write_text(CONFIGURATION)
            docker("cp", f"{folder}/configuration.yaml", f"{cls.name}:/config/configuration.yaml")
        docker("start", cls.name)
        cls.published = docker("port", cls.name, "8123/tcp").splitlines()
        cls.port = int(cls.published[0].rsplit(":", 1)[1])
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.wait_for(lambda: ask(f"{cls.base}/api/onboarding")[0] == 200, "start")
        cls.token = cls.onboard()
        cls.config = {"ha_server": f"127.0.0.1:{cls.port}", "ha_token": cls.token}
        cls.wait_for(lambda: cls.real("light.bed_light") and cls.real("scene.tux_evening"),
                     "load its demo")
        cls.put_the_bed_light_in_the_bedroom()

    @classmethod
    def wait_for(cls, ready, what, seconds=START_S):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            try:
                if ready():
                    return
            except OSError:
                pass
            time.sleep(1)
        raise RuntimeError(f"Home Assistant didn't {what}: "
                           + docker("logs", "--tail", "20", cls.name, check=False)[-1500:])

    @classmethod
    def onboard(cls) -> str:
        client = f"{cls.base}/"
        cls.password = secrets.token_urlsafe(16)
        _, made = ask(f"{cls.base}/api/onboarding/users", {
            "client_id": client, "name": "Tux Test", "username": "tux", "password": cls.password,
            "language": "en"})
        _, tokens = ask(f"{cls.base}/auth/token", {
            "grant_type": "authorization_code", "code": made["auth_code"], "client_id": client},
            form=True)
        cls.session_token = tokens["access_token"]
        return cls.websocket({"type": "auth/long_lived_access_token",
                              "client_name": "Tuxistant test", "lifespan": 1})[0]

    @classmethod
    def websocket(cls, *commands) -> list:
        from websockets.sync.client import connect
        results = []
        with connect(f"ws://127.0.0.1:{cls.port}/api/websocket", proxy=None) as socket:
            assert json.loads(socket.recv())["type"] == "auth_required"
            socket.send(json.dumps({"type": "auth", "access_token": cls.session_token}))
            assert json.loads(socket.recv())["type"] == "auth_ok"
            for number, command in enumerate(commands, 1):
                socket.send(json.dumps({"id": number, **command}))
                answer = json.loads(socket.recv())
                if not answer.get("success"):
                    raise RuntimeError(f"{command['type']}: {answer.get('error')}")
                results.append(answer["result"])
        return results

    @classmethod
    def put_the_bed_light_in_the_bedroom(cls):
        """Areas live in the registry, which only the WebSocket API can change. Onboarding made
        a Bedroom, and the demo's light has a device, which is what an area is given to."""
        entry, = cls.websocket({"type": "config/entity_registry/get",
                                "entity_id": "light.bed_light"})
        cls.websocket({"type": "config/device_registry/update", "device_id": entry["device_id"],
                       "area_id": "bedroom"})

    @classmethod
    def real(cls, entity_id, attribute=None):
        """What the server itself says, without going through the skill."""
        status, state = ask(f"{cls.base}/api/states/{entity_id}", token=cls.token)
        if status != 200:
            return None
        return state["attributes"].get(attribute) if attribute else state["state"]

    def setUp(self):
        homeassistant._cache.clear()

    def set(self, **args):
        return homeassistant.set_state(args, self.config)

    def status(self, **args):
        return homeassistant.status(args, self.config)["status"]

    def said(self, do, *args, **kwargs):
        with self.assertRaises(Exception) as caught:
            do(*args, **kwargs)
        return str(caught.exception)

    def test_the_port_is_on_this_computer_only(self):
        self.assertEqual(len(self.published), 1)
        self.assertTrue(self.published[0].startswith("127.0.0.1:"), self.published)

    def test_a_light_goes_on_and_is_read_back(self):
        self.assertEqual(self.set(name="bed light", state="off"), {"now": "Bed Light is off."})
        self.assertEqual(self.real("light.bed_light"), "off")
        now = self.set(name="the bed light", state="on")["now"]
        self.assertTrue(now.startswith("Bed Light is on"), now)
        self.assertEqual(self.real("light.bed_light"), "on")
        self.assertEqual(self.status(name="bed light"), now)

    def test_brightness_and_colour_arrive_as_asked(self):
        self.assertEqual(self.set(name="bed light", brightness="40%", color="blue"),
                         {"now": "Bed Light is on at 40 percent, blue."})
        self.assertEqual(self.real("light.bed_light", "brightness"), 102)
        self.assertEqual(self.real("light.bed_light", "hs_color"), [240, 100])
        self.assertEqual(self.set(name="bed light", color="warm white", brightness="brighter"),
                         {"now": "Bed Light is on at 65 percent, warm white."})
        self.assertEqual(self.real("light.bed_light", "color_temp_kelvin"), 2700)

    def test_a_thermostat_takes_a_temperature_in_its_range(self):
        now = self.set(name="heat pump", temperature=23)["now"]
        self.assertTrue(now.startswith("HeatPump is set to 23 degrees Celsius"), now)
        self.assertEqual(self.real("climate.heatpump", "temperature"), 23)
        self.assertEqual(self.said(self.set, name="heatpump", temperature=80),
                         "HeatPump can be set between 7 and 35 degrees Celsius.")
        self.assertEqual(self.real("climate.heatpump", "temperature"), 23)
        self.assertEqual(self.said(self.set, name="ecobee", temperature=21),
                         "Home Assistant didn't accept that for Ecobee.",
                         "it holds a range, and the server's own refusal comes back plain")
        self.assertEqual(self.status(name="outside temperature"),
                         "Outside Temperature is 15.6 degrees Celsius.")

    def test_unlocking_waits_for_its_card(self):
        self.assertEqual(self.set(name="front door", state="lock"),
                         {"now": "Front Door is locked."})
        card = self.set(name="front door", state="unlock")
        self.assertIsInstance(card, Ask)
        self.assertEqual(card.title, "Unlock Front Door?")
        self.assertEqual(card.detail, "Front Door (lock.front_door)\nService: lock.unlock\n"
                                      f"Home Assistant at 127.0.0.1:{self.port}")
        time.sleep(3)   # the demo's lock takes two seconds to move, so it could have by now
        self.assertEqual(self.real("lock.front_door"), "locked", "nothing before the click")
        self.assertEqual(card.run(), {"now": "Front Door is unlocked."})
        self.assertEqual(self.real("lock.front_door"), "unlocked")
        self.assertEqual(self.set(name="front door", state="lock"),
                         {"now": "Front Door is locked."})

    def test_opening_the_garage_waits_and_closing_does_not(self):
        self.assertEqual(self.set(name="garage", state="close"), {"now": "Garage Door is closed."})
        card = self.set(name="garage door", state="open")
        self.assertEqual((card.title, card.detail.splitlines()[:2]),
                         ("Open Garage Door?", ["Garage Door (cover.garage_door)",
                                                "Service: cover.open_cover"]))
        time.sleep(1)
        self.assertEqual(self.real("cover.garage_door"), "closed")
        self.assertEqual(card.run(), {"now": "Garage Door is open."})
        self.assertEqual(self.real("cover.garage_door"), "open")
        self.assertEqual(self.set(name="garage door", state="close"),
                         {"now": "Garage Door is closed."})

    def test_a_scene_and_a_script_each_wait_for_their_card(self):
        self.set(name="bed light", state="off")
        card = self.set(name="tux evening")
        self.assertIsInstance(card, Ask)
        self.assertEqual(card.title, "Activate the scene Tux evening?")
        self.assertIn("(scene.tux_evening)\nService: scene.turn_on\n", card.detail)
        time.sleep(1)
        self.assertEqual(self.real("light.bed_light"), "off", "nothing before the click")
        self.assertEqual(card.run(), {"now": "Tux evening is activated."})
        self.assertEqual(self.real("light.bed_light"), "on")
        self.set(name="kitchen lights", state="on")
        card = self.set(name="tux goodnight")
        self.assertEqual(card.title, "Run the script Tux goodnight?")
        self.assertIn("(script.tux_goodnight)\nService: script.turn_on\n", card.detail)
        time.sleep(1)
        self.assertEqual(self.real("light.kitchen_lights"), "on")
        self.assertEqual(card.run(), {"now": "Tux goodnight has started."})
        self.wait_for(lambda: self.real("light.kitchen_lights") == "off", "run the script", 10)

    def test_the_other_kinds_of_thing(self):
        for name, entity_id, on in (("guest mode", "input_boolean.guest_mode", "on"),
                                    ("AC", "switch.ac", "on"),
                                    ("ceiling fan", "fan.ceiling_fan", "on"),
                                    ("lounge room tv", "media_player.lounge_room", "playing")):
            with self.subTest(entity_id):
                self.set(name=name, state="off")
                self.assertEqual(self.real(entity_id), "off")
                self.set(name=name, state="on")
                self.assertEqual(self.real(entity_id), on)
                self.set(name=name, state="off")
        self.assertEqual(self.said(self.set, name="security", state="disarm"),
                         homeassistant.NO_ALARMS)
        self.assertEqual(self.status(name="alarm"), "Security is disarmed.")

    def test_a_room_from_the_registry(self):
        self.set(name="bed light", state="off")
        now = self.set(name="the bedroom light", state="on")["now"]
        self.assertTrue(now.startswith("Bed Light is on"), "its name doesn't say bedroom: " + now)
        self.assertIn("Bed Light is on", self.status(name="bedroom"))

    def test_names_that_meet_and_kinds_that_differ(self):
        self.assertEqual(self.said(self.set, name="kitchen", state="open"),
                         "There are 2 that could be: Kitchen Door and Kitchen Window. "
                         "Say which one.")
        self.assertEqual(self.set(name="kitchen", state="off"), {"now": "Kitchen Lights is off."})
        self.assertEqual(self.real("media_player.kitchen"), "playing", "the speaker is left be")
        self.assertEqual(self.status(name="kitchen lights"), "Kitchen Lights is off.")
        self.assertTrue(self.status(kind="lights").endswith(" The other one is off."),
                        "six lights: the five that are on are said, and the one at rest counted")

    def test_a_token_that_is_wrong_and_one_that_is_not_an_administrators(self):
        wrong = {**self.config, "ha_token": "not-the-token"}
        self.assertEqual(self.said(homeassistant.status, {"name": "bed light"}, wrong),
                         homeassistant.WRONG_TOKEN)
        guest = {**self.config, "ha_token": self.a_guest()}
        homeassistant._cache.clear()
        self.assertEqual(ask(f"{self.base}/api/template", {"template": "1"}, token=guest[
            "ha_token"])[0], 401, "the template engine is for administrators")
        now = homeassistant.set_state({"name": "bed light", "state": "on"}, guest)["now"]
        self.assertTrue(now.startswith("Bed Light is on"), "names work without areas: " + now)
        self.assertIn("anything called bedroom light", self.said(
            homeassistant.set_state, {"name": "bedroom light", "state": "on"}, guest))

    def a_guest(self) -> str:
        """A token for a user who isn't an administrator, through the login a browser uses."""
        password, client = secrets.token_urlsafe(16), f"{self.base}/"
        made, = self.websocket({"type": "config/auth/create", "name": "Guest",
                                "group_ids": ["system-users"], "local_only": False})
        self.websocket({"type": "config/auth_provider/homeassistant/create",
                        "user_id": made["user"]["id"], "username": "guest", "password": password})
        _, flow = ask(f"{self.base}/auth/login_flow", {
            "client_id": client, "handler": ["homeassistant", None], "redirect_uri": client})
        _, done = ask(f"{self.base}/auth/login_flow/{flow['flow_id']}", {
            "username": "guest", "password": password, "client_id": client})
        _, tokens = ask(f"{self.base}/auth/token", {
            "grant_type": "authorization_code", "code": done["result"], "client_id": client},
            form=True)
        return tokens["access_token"]

    def test_the_token_is_in_nothing_that_is_said(self):
        said = [json.dumps(homeassistant.status({"kind": "lights"}, self.config)),
                json.dumps(self.set(name="bed light", state="on")),
                self.said(self.set, name="nothing like it", state="on"),
                homeassistant.check(self.config)]
        card = self.set(name="front door", state="unlock")
        said += [card.title, card.detail, repr(card)]
        for text in said:
            self.assertNotIn(self.token, text)


if __name__ == "__main__":
    unittest.main()
