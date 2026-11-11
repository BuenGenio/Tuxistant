"""What people say for a brightness and a colour, as typed values a light can be sent.

Home Assistant uses these. They are a copy of the tables and the three helpers in lights.py, so
that it stands by itself and the two skills hear "half", "warm" and "#ff0000" the same way.
"""
import colorsys
import re

from .gate import shown

WARMEST, COOLEST = 2500, 6500
STEP = 25                   # "brighter" and "dimmer"

# Saturation 100 unless it says otherwise. Hues run 0 to 360.
COLOURS = {"red": (0, 100), "orange": (25, 100), "amber": (40, 100), "yellow": (55, 100),
           "lime": (90, 100), "green": (120, 100), "teal": (170, 100), "cyan": (185, 100),
           "turquoise": (175, 80), "sky blue": (200, 70), "blue": (240, 100),
           "indigo": (255, 100), "purple": (275, 100), "violet": (270, 70),
           "lavender": (270, 35), "magenta": (300, 100), "pink": (330, 55),
           "hot pink": (330, 80), "crimson": (348, 100)}
WHITES = {"candle": 2500, "warm white": 2700, "warm": 2700, "soft white": 2700,
          "yellow white": 3000, "white": 4000, "neutral white": 4000, "neutral": 4000, "natural": 4000, "normal": 4000,
          "cool white": 5500, "cool": 5500, "bright white": 5500, "cold": 6500,
          "cold white": 6500, "daylight": 6500, "blue white": 6500}
NOISE = re.compile(r"\b(?:colou?r|light|lights|bulb|the|a|to|in|please|ish|shade|of)\b")
BRIGHTER = {"brighter", "up", "more", "increase", "higher", "raise"}
DIMMER = {"dimmer", "dim", "down", "less", "decrease", "lower", "darker"}
LEVELS = {"full": 100, "max": 100, "maximum": 100, "brightest": 100, "half": 50,
          "min": 1, "minimum": 1, "lowest": 1, "dimmest": 1}
ON = {"on", "true", "yes", "1", "turn on", "switch on"}
OFF = {"off", "false", "no", "0", "turn off", "switch off"}


def brightness(value, current: int) -> int | None:
    """1 to 100, or 0 for off. Numbers can come as 40, 0.4, "40%" or words."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Say a brightness from 1 to 100 percent.")
    if isinstance(value, (int, float)):
        level = value * 100 if isinstance(value, float) and 0 < value < 1 else value
    else:
        text = " ".join(str(value).casefold().replace("percent", "").replace("%", "").split())
        if text in BRIGHTER:
            return min(100, current + STEP)
        if text in DIMMER:
            return max(1, current - STEP)
        if text in LEVELS:
            return LEVELS[text]
        try:
            level = float(text)
        except ValueError:
            raise ValueError("Say a brightness from 1 to 100 percent.") from None
    return max(0, min(100, round(level)))


def colour(value) -> dict | None:
    """Settings for a colour name, a white, a temperature in kelvin, or #rrggbb."""
    if value is None or value == "":
        return None
    text = " ".join(str(value).casefold().replace("-", " ").split())
    if match := re.fullmatch(r"#?([0-9a-f]{6})", text):
        red, green, blue = (int(match[1][i:i + 2], 16) / 255 for i in (0, 2, 4))
        hue, saturation, _ = colorsys.rgb_to_hsv(red, green, blue)
        if saturation < 0.1:
            return {"color_temp": WHITES["white"]}
        return {"hue": round(hue * 360) % 360, "saturation": max(1, round(saturation * 100)),
                "color_temp": 0}
    if match := re.fullmatch(r"(\d{4,5}) ?(?:k|kelvin|kelvins)?", text):
        return {"color_temp": max(WARMEST, min(COOLEST, int(match[1])))}
    for name in (text, " ".join(NOISE.sub(" ", text).split())):
        if name in WHITES:
            return {"color_temp": WHITES[name]}
        if name in COLOURS:
            hue, saturation = COLOURS[name]
            return {"hue": hue, "saturation": saturation, "color_temp": 0}
    raise ValueError(f"I don't know the colour {shown(value, 40)}. Try a name like blue, "
                     "warm white or daylight.")


def describe(info: dict) -> str:
    """The nearest name for a colour temperature, or for a hue and a saturation."""
    temp = info.get("color_temp") or 0
    if temp:
        return "warm white" if temp < 3300 else "white" if temp < 5000 else "cool white"
    hue, saturation = info.get("hue", 0) % 360, info.get("saturation", 0)
    if saturation < 15:
        return "white"

    def distance(name):
        h, s = COLOURS[name]
        return min(abs(hue - h), 360 - abs(hue - h)) + abs(saturation - s) / 2

    return min(COLOURS, key=distance)
