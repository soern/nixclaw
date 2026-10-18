# signal-api: Signal voice calls

Container-side plumbing for the **experimental** voice-call feature: the bot
account can answer and place 1:1 Signal voice calls with full-duplex audio.
The agent side (`src/nixclaw_agent/voice/`) and its configuration are described
in `doc/ARCHITECTURE.md`, section "Voice calls". The manual test and results
below come from the initial proof of concept (echo test, no STT/TTS).

## How it works

- signal-cli >= 0.14.2 has call support (`startCall`, `acceptCall`,
  `hangupCall`, `rejectCall`, `listCalls`, `subscribeCallEvents` ->
  `callEvent` notifications). Our signal-api image already ships 0.14.8 (JVM
  build - the native build has a reflection bug on incoming calls, signal-cli
  #2144).
- Media is handled by `signal-call-tunnel` (Rust, wraps Signal's RingRTC),
  spawned by signal-cli once per call, found via `SIGNAL_CALL_TUNNEL_BIN`.
- Audio goes through PulseAudio: per call the tunnel creates null sinks;
  write 48 kHz mono s16le to `sink_for_<inputDeviceName>` (-> remote), read
  from `<outputDeviceName>.monitor` (<- remote).
- The REST wrapper stays untouched: it runs `signal-cli daemon --tcp
  127.0.0.1:6001`; `call_debug.py` is a second JSON-RPC client on that socket.
  Call events are opt-in per connection, so the wrapper never sees them.

## Files (production layout)

| File | Purpose |
|---|---|
| `Dockerfile.signal-api` (repo root) | Builds the tunnel (WebRTC audio processing disabled); runtime = pinned `bbernhard/signal-cli-rest-api:0.101` + PulseAudio + socat + tunnel |
| `s6-rc.d/pulseaudio/` | Headless PulseAudio, started before the signal-cli daemon; unix socket + TCP 4713 |
| `s6-rc.d/jsonrpc-forward/` | socat `0.0.0.0:6002` -> daemon `127.0.0.1:6001` (container loopback) |
| `call_debug.py` | Manual test driver (stdlib only), at `/usr/local/bin/call_debug.py` in the container |

`docker-compose.yml` publishes 6002 (JSON-RPC call API) and 4713 (PulseAudio)
on the host's 127.0.0.1 only; nixclaw (host network) uses both.

## Manual test

```bash
# incoming: call the bot from a phone; auto-accept + echo + record
docker exec -it nixclaw-signal-api call_debug.py listen --echo --record /tmp/rec.wav
# outgoing
docker exec -it nixclaw-signal-api call_debug.py call +49... --echo
```

## Source review of signal-call-tunnel (2026-10-11)

Pinned: `visigoth/signal-call-tunnel@02c84e9bfbb45e6872956ac2168adcc0b1e7a8ba`
(author = author of signal-cli PR #1932; `robertnisipeanu/signal-call-tunnel`
is an identical mirror). No malicious behaviour found:

- Crate (~1.4k lines): JSON over stdin/stdout only; no own network, file or
  process access; no obfuscated/embedded payloads.
- `build.rs`: only `git apply` of the bundled 12-line patch (env-var toggle
  for cubeb stream prefs).
- ringrtc submodule = official `signalapp/ringrtc` tag v2.65.1. Its
  `virtual_audio.sh` (compiled in) only runs `pactl load/unload-module` on
  Linux; the `git clone`/`sudo` parts are macOS-only. Prebuilt WebRTC is
  downloaded by ringrtc's `fetch-artifact` with SHA-256 verification.
- `Cargo.lock`: 279 crates.io packages + `signalapp/libsignal` v0.87.1 +
  `signalapp/curve25519-dalek`; no other git sources.
- Not audited: the crates.io/libsignal/WebRTC dependency tree itself.

Known functional caveats: incoming ICE candidates hard-code sender device 1
(`main.rs:378`, may break calls from linked devices); events are JSON
assembled with `format!` without escaping.

## Results (2026-10-11, live bot account, real phone)

| Test | Result |
|---|---|
| Incoming call, auto-accept | RINGING_INCOMING -> CONNECTED in ~2 s |
| Outgoing call (`startCall`) | ringing -> answered -> CONNECTED |
| phone -> bot audio | clean speech in recording (peaks ~25k) |
| bot -> phone audio | beeps (`--play`) heard; echo heard after disabling WebRTC APM |
| Text messaging during PoC | unaffected (REST wrapper + call client share the daemon socket) |

Findings:

- **WebRTC audio processing must be off.** The tunnel used
  `AudioConfig::default()` (AEC/NS/AGC/high-pass on). AEC cancelled the echo
  because the capture signal correlated with playout. The Dockerfile patches
  `main.rs` to disable the whole chain - correct for virtual devices anyway.
- **PulseAudio modules leak per call.** After a call ends (remote or local
  hangup) the call's `module-null-sink`/`module-remap-source` stay loaded
  (the tunnel's teardown doesn't run when signal-cli stops it; it does on
  stdin EOF). Production needs a sweep of `signal_(input|output)_<id>` modules
  for calls no longer in `listCalls`.
- The base image has no Python; the PoC image adds `python3` for
  `call_debug.py` (`python3-minimal` lacks `queue`).
- The bot account is the primary device (deviceId 1), so the tunnel's
  hard-coded sender device 1 for ICE didn't matter here.
