# Solo Studio provider canary

This canary verifies one small artifact from each selected provider:

- Video: Higgsfield CLI, `seedance_2_0`, 5 seconds, 16:9.
- Voiceover: ElevenLabs text-to-speech, one short sentence.
- Music: Higgsfield CLI, `seed_audio`, short instrumental track.

The default command is a no-network dry run. A live run requires the CLI flags
`--live --confirm-spend`, the environment gate
`SOLO_STUDIO_PROVIDER_CANARY_LIVE=1`, and an exact host allowlist:
`SOLO_STUDIO_HIGGSFIELD_ALLOWED_HOSTS` must contain the exact Higgsfield
artifact hostnames expected from the authenticated CLI, comma-separated. Do not
use a wildcard or an arbitrary public hostname; the downloader rejects hosts not
listed exactly. A live run may consume provider credits.

## Safe dry run

In the container, from the application directory:

```bash
cd /app
python provider_canary.py
```

For a local checkout, use its application directory instead:

```bash
cd /opt/data/solo-studio-video
python3 provider_canary.py
```

Expected output is JSON with `"status": "dry_run"` and
`"network_called": false`.

## VPS preflight

Use the Hostinger Browser Terminal when SSH is unavailable. Inspect only command
availability and file metadata; do not print credential contents:

```bash
command -v higgsfield
higgsfield --version
command -v ffprobe
stat -c '%a %U %G %n' "$HOME/.config/higgsfield/credentials.json"
test -r "$HOME/.config/higgsfield/credentials.json"
```

The credentials file must already be authenticated for the runtime user. Do not
copy it into the repository, image layers, chat, or command arguments.

## Live canary (requires explicit spend approval)

Only run this after confirming that one small video generation, one TTS request,
and one music generation may consume provider credits:

```bash
cd /app
SOLO_STUDIO_PROVIDER_CANARY_LIVE=1 \
SOLO_STUDIO_ENABLE_HIGGSFIELD=1 \
SOLO_STUDIO_ENABLE_TTS=1 \
python provider_canary.py --live --confirm-spend
```

The runner creates private temporary output under `/tmp`, verifies video/audio
streams and durations with `ffprobe`, emits only safe metadata, and removes the
temporary directory before exiting. It never emits provider URLs, credentials,
raw provider stdout/stderr, or artifact paths.

Exit codes:

- `0`: all three artifacts passed provider and media verification;
- `1`: a provider request or artifact verification failed;
- `2`: a required live gate, CLI, or configuration prerequisite is missing.

This canary does **not** claim that the production worker assembles or publishes
a final MP4. The worker now has an optional, fail-closed music stage: it rejects
malformed storyboard durations, bounds provider metadata, verifies the returned
artifact through the descriptor-bound media verifier, and persists only safe
metadata. Final MP4 audio muxing remains a separate implementation stage.
