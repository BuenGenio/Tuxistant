#!/usr/bin/env python3
"""Builds the demo video from the real extension and Settings app in an isolated headless shell.

    python3 tools/demo_video.py <video.mp4> [--work <folder>] [--reuse] [--silent]

Everything on screen is real, and what it shows is scripted in tests/shell_harness/demo_shots.py
and demo_script.py. This tool adds the labels, the title and the end card, and the voices: each
line is spoken by the server in Settings, as the daemon would speak it, and the blob moves to that
sound. Only the speech endpoint is called. Without a server the film is silent.

--work keeps the voices and the frames, and --reuse cuts the film again from what is there.
Needs gnome-shell, ffmpeg and Pillow, like docs_images.py. Safe next to the desktop.
"""
import argparse
import array
import asyncio
import bisect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/shell_harness"
sys.path[:0] = [str(ROOT), str(HARNESS)]
import demo_script  # noqa: E402
from demo_script import LINES, SCENES, TUX  # noqa: E402
from tuxistant.audio import BYTES_PER_S, RATE, level  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.realtime import RealtimeError, sentences, speech  # noqa: E402

SIZE = (1600, 900)
FPS = 30
TAKES = {"close": ("1920x1080", "2"), "wide": ("1600x900", "1")}    # monitor and scale, as card.sh takes them
USER_VOICE = "am_michael"       # stands in for the person asking
CHUNK = BYTES_PER_S // 25       # what one Level signal covers
QUIET = 0.08
DISSOLVE_S = 0.3
FADE_S = 0.35
MARGIN = 44
WHITE, SOFT, ACCENT = (255, 255, 255), (214, 219, 240), (140, 170, 255)
PLATE = (18, 20, 32, 190)


def trimmed(pcm: bytes) -> bytes:
    """Without the silence a voice leaves at each end, so a line starts when its scene says."""
    loud = [i for i in range(0, len(pcm), CHUNK) if level(pcm[i:i + CHUNK]) >= QUIET]
    return pcm[max(0, loud[0] - CHUNK):loud[-1] + 3 * CHUNK] if loud else pcm


async def spoken(config, line: str) -> bytes:
    """Sentence by sentence, as the daemon speaks a reply."""
    who, words = LINES[line]
    voice = config["voice"] if who == TUX else USER_VOICE
    return trimmed(b"".join([await speech(config, part, voice) for part in sentences(words)]))


def save_wav(path: Path, pcm: bytes):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)


async def recorded(folder: Path) -> dict:
    config, clips = Config(), {}
    folder.mkdir(parents=True, exist_ok=True)
    for line in LINES:
        pcm = await spoken(config, line)
        save_wav(folder / f"{line}.wav", pcm)
        clips[line] = {"seconds": len(pcm) / BYTES_PER_S,
                       "levels": [round(level(pcm[i:i + CHUNK]), 3) for i in range(0, len(pcm), CHUNK)]}
    return clips


def voices(work: Path, silent: bool):
    """How long each line lasts and how loud it is as it goes, for the scenes to keep time with."""
    clips = {line: {"seconds": demo_script.estimate(line)} for line in LINES}
    shutil.rmtree(work / "voice", ignore_errors=True)
    if not silent:
        try:
            clips = asyncio.run(recorded(work / "voice"))
        except RealtimeError as e:
            shutil.rmtree(work / "voice", ignore_errors=True)
            print(f"The film will be silent. {e}", file=sys.stderr)
    (work / "voice.json").write_text(json.dumps(clips))


def shoot(work: Path):
    for take, (monitor, scale) in TAKES.items():
        out = work / take
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        shutil.copy(work / "voice.json", out)
        env = {**os.environ, "TUX_DRIVER": "demo_shots.py", "TUX_SCENARIO": f"demo-{take}",
               "TUX_MONITORS": monitor, "TUX_SCALE": scale, "TUX_TIMEOUT": "240"}
        subprocess.run([HARNESS / "card.sh", "tux", out], env=env, check=True)


class Scene:
    def __init__(self, name: str, take: Path, shot: dict, sounds: list):
        self.name, self.start = name, shot["start"]
        self.title, self.note = SCENES[name]
        self.frames = round((shot["end"] - shot["start"]) * FPS)
        order = sorted(range(len(shot["frames"])), key=shot["frames"].__getitem__)
        self.times = [shot["frames"][i] for i in order]
        self.paths = [take / "frames" / name / f"{i:05}.png" for i in order]
        self.sounds = [s for s in sounds if shot["start"] <= s["at"] < shot["end"]]

    def still(self, moment: float) -> Path:
        """The frame taken last before this moment."""
        return self.paths[max(0, bisect.bisect_right(self.times, moment) - 1)]


def scenes(work: Path) -> list:
    shots = {}
    for take in TAKES:
        film = json.loads((work / take / "film.json").read_text())
        shots.update({shot["name"]: (work / take, shot, film["sounds"]) for shot in film["scenes"]})
    return [Scene(name, *shots[name]) for name in SCENES]


def font(size: int, weight: str = "bold"):
    path = subprocess.run(["fc-match", "-f", "%{file}", f"sans:{weight}"], capture_output=True, text=True).stdout
    return ImageFont.truetype(path, size)


def label(title: str, note: str) -> Image.Image:
    """The name of a scene on a dark plate, top left, where nothing in the scenes ever is."""
    layer = Image.new("RGBA", SIZE)
    draw, big, small = ImageDraw.Draw(layer), font(38), font(22, "regular")
    width = max(draw.textlength(title, big), draw.textlength(note, small))
    draw.rounded_rectangle((MARGIN, 76, MARGIN + width + 44, 76 + 108), 18, PLATE)
    draw.text((MARGIN + 22, 86), title, WHITE, big)
    draw.text((MARGIN + 22, 142), note, SOFT, small)
    return layer


def scrim() -> Image.Image:
    """Darkens the left of the desktop, so that the words on it can be read and Tux still seen."""
    shade = Image.linear_gradient("L").rotate(270).resize(SIZE).point(lambda v: max(0, min(215, round((v - 110) * 2.2))))
    layer = Image.new("RGBA", SIZE, (12, 13, 24, 255))
    layer.putalpha(shade)
    return layer


def card(lines: list, small_print: str) -> Image.Image:
    """The title and the end card: big lines down the left, and how the film was made below them."""
    layer = scrim()
    draw, y = ImageDraw.Draw(layer), 250
    for words, size, colour, weight in lines:
        draw.text((MARGIN + 36, y), words, colour, font(size, weight))
        y += round(size * 1.55)
    draw.text((MARGIN + 36, SIZE[1] - 64), small_print, SOFT, font(19, "regular"))
    return layer


def layers(voiced: bool) -> dict:
    name = (demo_script.NAME, 112, WHITE, "bold")
    tagline = (demo_script.TAGLINE, 36, SOFT, "regular")
    made = demo_script.HOW_IT_WAS_MADE + (" and synthesised voices" if voiced else "")
    out = {scene: label(title, note) for scene, (title, note) in SCENES.items() if title}
    out["intro"] = card([name, tagline], made)
    out["outro"] = card([name, tagline, (demo_script.REPO, 36, ACCENT, "bold")], made)
    return out


def strength(film: list, index: int, frame: int) -> float:
    """A label fades in and out, except between scenes that share a title and at the film's ends."""
    scene, value = film[index], 1.0
    if index and film[index - 1].title != scene.title:
        value = min(value, frame / FPS / FADE_S)
    if index + 1 < len(film) and film[index + 1].title != scene.title:
        value = min(value, (scene.frames - frame) / FPS / FADE_S)
    return max(0.0, value)


class Projector:
    """Turns scenes into the film's pictures, opening each frame once however long it is held."""

    def __init__(self, film: list, voiced: bool):
        self.film, self.layers = film, layers(voiced)
        self.path = self.picture = None

    def still(self, scene: Scene, moment: float) -> Image.Image:
        path = scene.still(moment)
        if path != self.path:
            self.path = path
            self.picture = Image.open(path).convert("RGB").resize(SIZE, Image.Resampling.LANCZOS)
        return self.picture

    def labelled(self, picture: Image.Image, index: int, frame: int) -> Image.Image:
        layer, amount = self.layers.get(self.film[index].name), strength(self.film, index, frame)
        if not layer or amount <= 0:
            return picture
        picture = picture.copy()
        picture.paste(layer, mask=layer.getchannel("A").point(lambda a: round(a * amount)))
        return picture

    def pictures(self):
        before = None
        for index, scene in enumerate(self.film):
            for frame in range(scene.frames):
                picture = self.still(scene, scene.start + frame / FPS)
                if before and frame < DISSOLVE_S * FPS:
                    picture = Image.blend(before, picture, frame / FPS / DISSOLVE_S)
                yield self.labelled(picture, index, frame)
            before = self.still(scene, scene.start + scene.frames / FPS)


def soundtrack(film: list, work: Path) -> Path | None:
    """Each line where its scene said it, which is also when the blob moved to it."""
    if not (work / "voice").is_dir():
        return None
    track, at = array.array("h", bytes(2 * RATE * sum(scene.frames for scene in film) // FPS)), 0
    for scene in film:
        for sound in scene.sounds:
            with wave.open(str(work / "voice" / f"{sound['line']}.wav")) as clip:
                samples = array.array("h", clip.readframes(clip.getnframes()))
            start = round((at / FPS + sound["at"] - scene.start) * RATE)
            samples = samples[:max(0, len(track) - start)]
            track[start:start + len(samples)] = samples
        at += scene.frames
    save_wav(work / "track.wav", track.tobytes())
    return work / "track.wav"


def cut(work: Path, video: Path):
    film = scenes(work)
    track = soundtrack(film, work)
    sound = ["-i", track, "-c:a", "aac", "-b:a", "160k", "-ar", "48000"] if track else []
    encoder = subprocess.Popen([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{SIZE[0]}x{SIZE[1]}", "-r", str(FPS), "-i", "-", *sound,
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", video], stdin=subprocess.PIPE)
    for picture in Projector(film, bool(track)).pictures():
        encoder.stdin.write(picture.tobytes())
    encoder.stdin.close()
    if encoder.wait():
        raise SystemExit("ffmpeg could not write the video.")


def build(work: Path, args):
    if not args.reuse:
        voices(work, args.silent)
        shoot(work)
    cut(work, args.video)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", type=Path, help="the MP4 to write")
    parser.add_argument("--work", type=Path, help="keep the voices and the frames in this folder")
    parser.add_argument("--reuse", action="store_true", help="cut again from what --work already holds")
    parser.add_argument("--silent", action="store_true", help="no voices, and no call to the server")
    args = parser.parse_args()
    if args.reuse and not args.work:
        parser.error("--reuse needs the --work folder of an earlier run")
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        build(args.work.resolve(), args)
        return 0
    with tempfile.TemporaryDirectory() as folder:
        build(Path(folder), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
