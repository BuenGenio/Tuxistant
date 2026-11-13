# The server

Tuxistant is the desktop side. Speech recognition, the language model and the voice run on a [LocalAI](https://localai.io) server of your own, and this folder sets one up. It can be the same computer as the desktop or another one on your network.

## What you need

- An NVIDIA GPU with 8 GB of memory, and its driver installed (`nvidia-smi` works). Everything together uses about 7.3 GB.
- Docker with the compose plugin.
- About 12 GB of disk for the image and the models.
- Linux on x86-64. This has run on Ubuntu with an RTX 4060 and LocalAI v4.12.1, and nowhere else yet.

## Set it up

Copy this folder to the server and run the script there:

```sh
./setup.sh
```

It is safe to run again, after a failure or to pick up a change. It:

1. Checks the GPU and Docker, and installs the NVIDIA Container Toolkit if containers can't see the GPU. That step uses `sudo`.
2. Writes `.env` with a new API key, and starts LocalAI on port 8080.
3. Installs the models and voices below. The first run downloads about 4 GB.
4. Tests speech, transcription and a tool call, and loads the pipeline.

At the end it prints the server's address and where the key is. Put both in Tuxistant's Settings under General. If the server is the same computer as the desktop, the default `localhost:8080` is already right and only the key is needed.

To check a whole spoken turn from any computer with Python and `websockets`:

```sh
LOCALAI_API_KEY=... python3 realtime_test.py --host yourserver:8080
```

It speaks a request, and prints what was heard, the tool call and the reply.

## What it installs

| Name | Does | GPU memory |
|---|---|---|
| `silero-vad-ggml` | Notices when you are speaking | about 0.4 GB, shared with Whisper |
| `whisper-large-turbo-q5_0` | Speech to text, any language. Dictation and, from 0.3.0, every Fn hold use it too | about 0.9 GB |
| `parakeet-cpp-realtime_eou_120m-v1` | Speech to text for `tux-realtime-eou`, English only | about 0.35 GB |
| `tux-brain` | Qwen3-4B, the model that answers and picks tools | about 5 GB |
| `tux-voice` | Kokoro, the voice | about 0.85 GB |
| `voice-*` | Piper voices that speak translations in ten languages | none, they run on the CPU |

Two pipelines join these up. `tux-realtime` is the default: it understands any language Whisper does and takes a pause as the end of your turn. `tux-realtime-eou` is English only and decides from your words when you have finished, so a pause to think doesn't cut you off. Pick one in Settings under General.

The files in `models/` are the ones written for Tuxistant. Change the model, its settings or the voice there, then run `./setup.sh` again.

`models/tux-voice-exceptions.txt` lists words the voice says wrong and how to say them. Edits apply from the next sentence, with no restart.

## Translation

Translation is optional and uses [LibreTranslate](https://libretranslate.com), which Tuxistant expects on port 5000 of the same server:

```sh
docker compose -f translate.yaml up -d
```

The first start downloads the language models, which takes a while. This file runs it on the CPU. The `latest-cuda` image is faster, but it shares the GPU, and with both pipelines loaded 8 GB has under 1 GB to spare. If it runs somewhere else, set that address in Settings under General.

## Keeping it private

- The API key is the only thing protecting the server, and Tuxistant sends it over plain HTTP unless the address starts with `https://`. Keep the server on a network you trust, or reach it over a VPN such as WireGuard or Tailscale.
- Whoever runs the server can hear what you say and read what Tuxistant's tools return. [docs/SECURITY.md](../docs/SECURITY.md) says what that means under "A hostile model".
- `.env` holds the key and is mode 600. Don't commit it. The `.gitignore` here leaves it out, along with downloaded models.

## Things that went wrong on the way

- Use `tux-voice` in a pipeline, not the gallery's `kokoro`. The gallery config names no model file, which inside a pipeline fails with `path is outside of trusted root`. Calling `kokoro` directly also starts a second Kokoro and can push another model out of an 8 GB card.
- The first connection after a restart takes about 6 seconds, as every stage loads before the session is announced. After that the first words of a reply come 0.4 to 0.8 seconds after you stop speaking.
- The container runs as you, not root. `/backends` is a named volume that `setup.sh` gives to you. Without that, installing a backend fails with `permission denied`.
- Inside the container the command is `/local-ai`, which isn't on the `PATH`.
- A backend you have just installed gives `backend not found` until the container restarts.
- Install backends with the command line, as `setup.sh` does. The web page's install button fails with `mkdir //downloading: permission denied`.
- Kokoro is set to English. It accepts voices from other languages but reads them with English sounds, and text in another alphabet comes out as noise. That is why translations are spoken by Piper.
- Check a new voice by sending its audio back through Whisper, not by how long the audio is. One Italian voice produced speech of the right length that was nonsense.
