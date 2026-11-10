"""The demo video's words and scenes: what is asked, what Tux says, and the label on each scene.

demo_shots.py acts them out in the isolated shell, and tools/demo_video.py gives them voices and
cuts the film. All of it is made up, like docs_shots.py, so the video shows no real name or file.
"""
USER, TUX = "user", "tux"
LINES = {
    "weather-ask": (USER, "What's the weather?"),
    "weather": (TUX, "It's nineteen degrees and sunny in Lisbon, with a light breeze."),
    "trash-ask": (USER, "Move everything in Downloads older than a year to the Trash."),
    "trash": (TUX, "Done. Three files and one folder are in the Trash."),
    "dictation": (USER, "Book the train tickets for Friday, and call the plumber about the kitchen tap."),
    "lights-ask": (USER, "Turn off the lights."),
    "lights": (TUX, "The lights are off."),
    "theme-tux": (TUX, "Listening Tux."),
    "theme-orb": (TUX, "Orb."),
    "theme-waves": (TUX, "Waves."),
    "theme-scanner": (TUX, "Scanner."),
    "outro": (TUX, "I'm Tux, a voice assistant for the Linux desktop. "
                   "Speech and the model run on a server of your own."),
}
THEMES = ("tux", "orb", "waves", "scanner")

NAME = "Tuxistant"
TAGLINE = "A voice assistant for the Linux desktop"
REPO = "github.com/BuenGenio/Tuxistant"
HOW_IT_WAS_MADE = "The real extension and Settings app in an isolated GNOME Shell, with scripted requests"

# In the order they are shown. name: (title, note), the label the film puts on that scene.
SCENES = {
    "intro": ("", ""),
    "ask": ("Hold Fn and ask", "Captions show what was heard and what Tux says"),
    "approve": ("Risky actions wait for your click", "The card says exactly what will happen"),
    "dictate": ("Dictation", "Tap Fn, hold it and speak. Your words are typed where the cursor is"),
    "lights": ("Smart lights", "Tapo lights on your own network"),
    "theme-tux": ("Four themes", "Listening Tux"),
    "theme-orb": ("Four themes", "Orb"),
    "theme-waves": ("Four themes", "Waves"),
    "theme-scanner": ("Four themes", "Scanner"),
    "settings": ("Settings", "Server, voice, appearance, skills and history"),
    "outro": ("", ""),
}


def text(line: str) -> str:
    return LINES[line][1]


def estimate(line: str) -> float:
    """How long a line takes to say, for a film with no voices."""
    return 0.5 + 0.062 * len(text(line))
