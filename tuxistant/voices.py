"""Kokoro voices usable with the English tux-voice model."""
ACCENTS = {"a": "American", "b": "British"}
NAMES = """af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river
af_sarah af_sky am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck
bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis""".split()


def catalog() -> list[dict]:
    return [{"id": v, "name": v[3:].capitalize(), "accent": ACCENTS[v[0]],
             "gender": "Female" if v[1] == "f" else "Male"} for v in NAMES]
