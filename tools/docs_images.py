#!/usr/bin/env python3
"""Rebuilds docs/images from the real extension and Settings app in an isolated headless shell.

    python3 tools/docs_images.py [--raw <folder of an earlier run>]

Everything on screen is real, and what it shows is scripted in tests/shell_harness/docs_shots.py.
Needs gnome-shell, ffmpeg and Pillow. Pillow is for this tool alone, and the daemon never loads it.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/images"
THEMES = (("tux", "Listening Tux"), ("orb", "Orb"), ("waves", "Waves"), ("scanner", "Scanner"))
STATES = ("listening", "thinking", "speaking")
SCREEN = (1600, 900)
TURN_BOX = (800, 450, 1600, 900)
CARD_BOX = (1050, 400, 1600, 880)
PANEL_BOX = (1250, 0, 1600, 480)
CELL = (300, 275)
SOURCE = {"waves": (660, 605), "scanner": (360, 330)}     # the rest fit one to one, away from the edge
GAP, LABEL, MARGIN = 10, 34, 16
BACKGROUND = (24, 26, 40)
TEXT = (232, 234, 245)
SETTINGS = ("general", "voice", "appearance", "skills", "history")


def shoot(raw: Path):
    env = {**os.environ, "TUX_DRIVER": "docs_shots.py", "TUX_SCENARIO": "docs", "TUX_TIMEOUT": "280"}
    subprocess.run([ROOT / "tests/shell_harness/card.sh", "orb", raw], env=env, check=True)


def font(size: int):
    path = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True).stdout
    return ImageFont.truetype(path, size)


def save(image: Image.Image, name: str, colours: int = 0):
    image = image.convert("RGB")
    if colours:
        image = image.quantize(colours, dither=Image.Dither.FLOYDSTEINBERG)
    image.save(OUT / name, optimize=True)


def around(box: dict, width: int, height: int) -> tuple:
    """A window of this size on the blob, moved as far as it takes to stay on the screen."""
    x = box["x"] + box["width"] // 2 - width // 2
    y = box["y"] + box["height"] // 2 - height // 2
    x, y = min(max(x, 0), SCREEN[0] - width), min(max(y, 0), SCREEN[1] - height)
    return x, y, x + width, y + height


def cell(raw: Path, crops: dict, theme: str, state: str) -> Image.Image:
    name = f"theme-{theme}-{state}"
    width, height = SOURCE.get(theme, CELL)
    shot = Image.open(raw / f"{name}.png").crop(around(crops[name], width, height))
    return shot.resize(CELL, Image.Resampling.LANCZOS)


def grid(raw: Path, crops: dict, states: tuple, side_labels: bool) -> Image.Image:
    left = 110 if side_labels else 0
    width = MARGIN * 2 + left + len(THEMES) * CELL[0] + (len(THEMES) - 1) * GAP
    height = MARGIN * 2 + LABEL + len(states) * CELL[1] + (len(states) - 1) * GAP
    sheet = Image.new("RGB", (width, height), BACKGROUND)
    draw, label = ImageDraw.Draw(sheet), font(19)
    for column, (theme, title) in enumerate(THEMES):
        x = MARGIN + left + column * (CELL[0] + GAP)
        draw.text((x + CELL[0] / 2, MARGIN + LABEL / 2 - 4), title, TEXT, label, anchor="mm")
        for row, state in enumerate(states):
            y = MARGIN + LABEL + row * (CELL[1] + GAP)
            sheet.paste(cell(raw, crops, theme, state), (x, y))
            if side_labels and not column:
                draw.text((MARGIN, y + CELL[1] / 2), state.capitalize(), TEXT, font(17), anchor="lm")
    return sheet


def turn_gif(raw: Path):
    """Each frame keeps the time it was really taken at, as the shell does not shoot evenly."""
    frames = sorted((raw / "motion").glob("*.png"))
    times = json.loads((raw / "motion/times.json").read_text())
    lengths = [later - earlier for earlier, later in zip(times, times[1:])] + [2.0]
    with tempfile.TemporaryDirectory() as folder:
        listing = Path(folder) / "frames.txt"
        listing.write_text("".join(f"file '{frame}'\nduration {length:.3f}\n"
                                   for frame, length in zip(frames, lengths)) + f"file '{frames[-1]}'\n")
        x, y, right, bottom = TURN_BOX
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing,
            "-vf", f"crop={right - x}:{bottom - y}:{x}:{y},split[a][b];[a]palettegen=stats_mode=diff[p];"
                   "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
            "-fps_mode", "vfr", "-loop", "0", OUT / "turn.gif"], check=True)


def build(raw: Path):
    OUT.mkdir(exist_ok=True)
    crops = json.loads((raw / "crops.json").read_text())
    turn_gif(raw)
    save(grid(raw, crops, STATES, True), "themes.png")
    save(grid(raw, crops, ("speaking",), False), "themes-row.png")
    for name in ("card-trash", "card-admin"):
        save(Image.open(raw / f"{name}.png").crop(CARD_BOX), f"{name}.png")
    save(Image.open(raw / "panel-menu.png").crop(PANEL_BOX), "panel-menu.png", 256)
    for page in SETTINGS:
        box = crops[f"settings-{page}"]
        area = (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
        save(Image.open(raw / f"settings-{page}.png").crop(area), f"settings-{page}.png", 256)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, help="reuse the shots of an earlier run")
    args = parser.parse_args()
    if args.raw:
        build(args.raw)
        return 0
    with tempfile.TemporaryDirectory() as folder:
        shoot(Path(folder))
        build(Path(folder))
    return 0


if __name__ == "__main__":
    sys.exit(main())
