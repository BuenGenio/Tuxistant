#!/usr/bin/env bash
# Sets up the LocalAI server Tuxistant talks to. Run it on the server, from this folder:  ./setup.sh
# Idempotent: safe to re-run after a failure or to pick up config changes.
set -euo pipefail
cd "$(dirname "$0")"

LOCALAI_VERSION=v4.12.1
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
say "Preflight"
command -v nvidia-smi >/dev/null || die "nvidia-smi missing: install the NVIDIA driver first (ubuntu-drivers install)"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
command -v docker >/dev/null || die "docker missing: install docker-ce + compose plugin"
docker compose version >/dev/null || die "docker compose plugin missing"

if ! docker info 2>/dev/null | grep -qi 'nvidia'; then
  say "Installing NVIDIA Container Toolkit (needs sudo)"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
fi
docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L || die "containers cannot see the GPU"
docker rmi ubuntu:24.04 >/dev/null 2>&1 || true

# CUDA 13 images need driver >= 580; otherwise use CUDA 12.
DRIVER_MAJOR=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | cut -d. -f1)
if (( DRIVER_MAJOR >= 580 )); then CUDA=13; else CUDA=12; fi
VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
echo "driver $DRIVER_MAJOR -> cuda-$CUDA image; VRAM ${VRAM_MB} MiB"
(( VRAM_MB >= 7500 )) || echo "WARNING: <8 GB VRAM; whisper+qwen3-4b+kokoro may not all fit"

# ---------------------------------------------------------------- .env
PUID=$(id -u); PGID=$(id -g)    # the container runs as you (see compose.yaml)
LAN_IP=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
if [[ -f .env ]] && grep -q '^LOCALAI_API_KEY=' .env; then
  API_KEY=$(grep '^LOCALAI_API_KEY=' .env | cut -d= -f2-)
else
  API_KEY="sk-local-$(openssl rand -hex 24)"
fi
cat > .env <<EOF
LOCALAI_TAG=${LOCALAI_VERSION}-gpu-nvidia-cuda-${CUDA}
LOCALAI_API_KEY=${API_KEY}
LAN_IP=${LAN_IP}
PUID=${PUID}
PGID=${PGID}
EOF
chmod 600 .env
mkdir -p configuration data

# ---------------------------------------------------------------- start
say "Starting LocalAI ${LOCALAI_VERSION} (cuda-${CUDA})"
docker compose pull
# Named volume starts root-owned; the service runs as you (see compose.yaml).
docker volume create localai-backends >/dev/null
docker compose up -d
docker exec -u 0 local-ai chown -R "$PUID:$PGID" /backends
for i in $(seq 1 120); do
  curl -fs http://localhost:8080/readyz >/dev/null && break
  sleep 5; (( i == 120 )) && die "LocalAI not ready after 10 min: docker logs local-ai"
done
echo "ready"

# ---------------------------------------------------------------- models
say "Installing models (first run downloads ~4 GB)"
li() { docker exec -u "$PUID:$PGID" local-ai /local-ai "$@"; }
for m in silero-vad-ggml whisper-large-turbo-q5_0 parakeet-cpp-realtime_eou_120m-v1 qwen3-4b kokoro; do
  echo "-- $m"; li models install "$m"
done
echo "-- opus backend (WebRTC)"; li backends install opus || echo "opus install failed; WebSocket still works"
# Spoken translation. Piper runs on the CPU, so these take no VRAM; a second Kokoro for another
# language would not fit (see the gotchas in README.md).
echo "-- piper backend and voices"; li backends install cpu-piper || echo "piper install failed; translations will be shown but not spoken"
for v in uk-lada-x-low es_ES-davefx-medium fr-siwis-medium de_DE-thorsten-medium it-paola-medium \
         pt_BR-faber-medium ru_RU-irina-medium pl_PL-gosia-medium nl_NL-ronnie-medium zh_CN-huayan-medium; do
  li models install "voice-$v" || echo "voice-$v failed"
done

# Pronunciation exceptions for tux-voice (models/tux-voice-exceptions.txt). A backend reinstall
# replaces backend.py, so patch it again every run; the script skips a patched file.
echo "-- kokoro pronunciation exceptions"
for b in $(docker exec local-ai sh -c 'ls /backends/*kokoro*/backend.py'); do
  docker exec -i -u "$PUID:$PGID" local-ai "$(dirname "$b")/venv/bin/python" - "$b" < kokoro-exceptions.py
done

# Our own configs (tux-*.yaml) are already in ./models; restart so they are picked up next
# to the gallery-installed files.
docker compose restart
until curl -fs http://localhost:8080/readyz >/dev/null; do sleep 3; done

# ---------------------------------------------------------------- smoke tests
say "Smoke tests"
AUTH=(-H "Authorization: Bearer ${API_KEY}")
J=(-H "Content-Type: application/json")
OUT=$(mktemp -d)

echo "-- models"; curl -fs "${AUTH[@]}" http://localhost:8080/v1/models | grep -o '"id":"[^"]*"' | sort

echo "-- TTS (tux-voice / Kokoro)"
curl -fs "${AUTH[@]}" "${J[@]}" http://localhost:8080/v1/audio/speech \
  -d '{"model":"tux-voice","input":"Hello from Tux, running on your own server."}' \
  -o "$OUT/hello.wav" && ls -l "$OUT/hello.wav"

echo "-- STT (whisper, round trip of the TTS output)"
curl -fs "${AUTH[@]}" http://localhost:8080/v1/audio/transcriptions \
  -F model=whisper-large-turbo-q5_0 -F file=@"$OUT/hello.wav"; echo

echo "-- LLM tool call (tux-brain)"
curl -fs "${AUTH[@]}" "${J[@]}" http://localhost:8080/v1/chat/completions -d '{
  "model":"tux-brain",
  "messages":[{"role":"user","content":"Set a timer for 20 minutes. /no_think"}],
  "tools":[{"type":"function","function":{"name":"set_timer","description":"Start a countdown timer",
    "parameters":{"type":"object","properties":{"minutes":{"type":"integer"}},"required":["minutes"]}}}]
}' | python3 -c 'import json,sys; m=json.load(sys.stdin)["choices"][0]["message"]; print(json.dumps(m.get("tool_calls") or m.get("content"), indent=1))'

echo "-- warm the realtime pipeline"
curl -fs "${AUTH[@]}" "${J[@]}" http://localhost:8080/backend/load -d '{"model":"tux-realtime"}'; echo
nvidia-smi --query-gpu=memory.used,memory.total --format=csv

say "Done"
cat <<EOF
Web UI / Talk page : http://${LAN_IP}:8080
Realtime WebSocket : ws://${LAN_IP}:8080/v1/realtime?model=tux-realtime
                     ws://${LAN_IP}:8080/v1/realtime?model=tux-realtime-eou   (semantic VAD, English)
API key            : in $(pwd)/.env  (LOCALAI_API_KEY)

In Tuxistant's Settings, under General, set the server to ${LAN_IP}:8080 (or this machine's
host name) and paste the API key.
EOF
