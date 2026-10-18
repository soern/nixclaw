# NixClaw

A personal AI agent you talk to over [Signal](https://signal.org). NixClaw is a small Python asyncio harness around a self-hosted LLM (Ollama, vLLM, sglang, or any other OpenAI API-compatible backend). It runs as a Docker Compose stack next to [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) and a Playwright browser server.

Design goals: simplicity, robustness, security.

Website: [nixclaw.ai](https://www.nixclaw.ai)

## Features

- 1:1 and group chats (groups: the bot answers when @-mentioned); per-chat workspace, history, summaries and persona (`SOUL.md`)
- Security by design: separate containers per component and three user tiers (see [Security](#security))
- Tools: files, web browsing and search, scheduled jobs and subagents, vector memory, skills (`skills/`), approval polls before sensitive scripts run (example: the disabled-by-default print skill)
- Optional media backends: voice-message transcription, image generation and editing, text-to-speech
- Optional web interface (chat + file browser, login via a code sent over Signal)
- Voice calls over Signal (**experimental**, off by default)

## Security

An agent with tools can be misled by anything it reads: web pages, messages, files. NixClaw assumes this can happen and limits what a misled model can reach.

**Separate containers.** Each component runs in its own container. All ports are bound to `127.0.0.1`.

```
Signal ──▶ signal-api ──webhook + secret──▶ nixclaw agent ──▶ LLM server
           (Signal client)                  (tools, chat       (Ollama / vLLM / sglang)
                                             workspaces, DB)
                                                 │
                                                 ▼
                                             browser
                                             (Playwright, no access to bot data)
```

- **signal-api** runs the Signal client. Only the host can reach it, and the agent rejects webhook calls that don't carry `SIGNAL_WEBHOOK_SECRET`.
- **browser** renders web pages in its own Playwright container, which has none of the bot's files. Each chat gets its own browser context, and scheduled jobs and subagents get a throwaway one.
- **Received audio** is decoded by ffmpeg running as `nobody`, with resource limits.
- **Git hooks:** the agent container mounts `.git` read-only, so it can't plant a hook that would run on the host.
- **Optional services** (web interface, speech-to-text, text-to-speech, image generation) each run in their own container and talk to the agent over HTTP.

**Three user tiers.** Each sender is classified by phone number, and every tool call checks the sender's tier in code, not just in the prompt:

| Tier | Who | Tools |
|---|---|---|
| Privileged | `auth.privileged_numbers` | Everything, including bash, scheduled jobs, subagents and memory |
| Known | `auth.known_numbers` | Files in the chat's workspace, web browsing and search, message search, image and speech tools |
| Unknown | everyone else | Skills only. Not answered at all in 1:1 chats (unless `auth.unknown_users.allow_direct`); in groups only while a known or privileged user is a member (`allow_in_groups: false` turns that off) |

Other safeguards:
- **Per-chat workspaces:** file tools check every path and can't leave the chat's directory.
- **Approval polls:** a script can ask for approval before it acts. The bot posts a Yes/No poll in Signal, and only a "Yes" from a privileged user lets the script run.
- **Tool limits:** every tool call has a timeout, and repeated failures trip a circuit breaker.
- **Web interface:** login uses a code sent over Signal, and every request that changes data must carry a header that blocks cross-site request forgery.

The tiers protect the bot from other people. They don't protect it from privileged users, whose shell runs as root in the agent container (see Setup, step 3). Details are in [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md).

## Setup

**You need:** Docker with Compose, a running LLM server ([Ollama](https://ollama.com), [vLLM](https://docs.vllm.ai), [sglang](https://github.com/sgl-project/sglang), or any other OpenAI API-compatible backend), and a phone number for the bot. The bot needs its own Signal account: it ignores messages from its own number, so you can't use it with your personal account.

1. **Clone** (`deploy/` is a submodule with optional deployment scripts for model servers):
   ```bash
   git clone --recurse-submodules https://github.com/soern/nixclaw.git nixclaw && cd nixclaw
   ```
2. **Create the webhook secret** (signal-api sends it with every message; docker-compose won't start without it):
   ```bash
   echo "SIGNAL_WEBHOOK_SECRET=$(openssl rand -hex 32)" >> .env
   ```
3. **Configure the agent:** `cp config/agent.yaml.example config/agent.yaml`, then set:
   - `ollama.host` / `model`, or `backend: "vllm"` and `vllm.host`. The agent uses host networking, so a server on the same machine is `http://localhost:<port>`. The `vllm` backend works with any OpenAI API-compatible server (vLLM, sglang, etc.).
   - `signal.phone_number`: the bot's number.
   - `auth.privileged_numbers`: your number. Privileged users get every tool, **including a root shell** in the agent container. That shell uses the host's network and can read the whole checkout (including `.env` and the bot's data) and use the local Signal API. Web pages or messages the bot reads could try to steer it, so only list numbers you trust.
   - `auth.known_numbers` (optional): people who may use files, web and media tools. Everyone else can only use skills, and only gets an answer in groups that a known or privileged user is a member of; unknown numbers are ignored in 1:1 chats. Change this under `auth.unknown_users` (`allow_direct`, `allow_in_groups`).
   - `agent.name`: the name the bot answers to when @-mentioned in groups.
4. **Start:**
   ```bash
   docker-compose up -d --build
   ```
5. **Connect the bot's Signal account** through the signal-api container (port 8080, reachable on the host only; from another machine use `ssh -L 8080:localhost:8080 <host>`):
   - If the number already has Signal on a phone: open `http://localhost:8080/v1/qrcodelink?device_name=nixclaw` and scan the QR code in that phone's Signal app (Settings → Linked devices).
   - Otherwise register the number directly, see [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) (`/v1/register`).

   Then run `docker-compose restart signal-api`. The account data is stored in `.signal-data/`.
6. **Test:** message the bot from your privileged number in a 1:1 chat. Watch with `docker logs -f nixclaw`.

**Optional:**
- **Media backends:** voice-message transcription (`stt:`), text-to-speech (`tts:`) and image generation (`image_gen:`) in `config/agent.yaml`. Scripts for the servers are in `deploy/`.
- **Web interface:** set `webui.enabled: true` in `config/agent.yaml` and run `docker-compose restart nixclaw`. The UI is at `https://localhost:8443`, login with a code sent over Signal. Settings are in `config/webinterface.yaml` (copy from `.example`).
- **Voice calls** (experimental): `voice_calls:` in `config/agent.yaml`, see [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md).
- **Contributing:** `pip install -r requirements-dev.txt` installs the test and type-check tools (`python3 -m pytest tests/`, `mypy src/ --ignore-missing-imports --explicit-package-bases`). `scripts/install-git-hooks.sh` enables the pre-commit privacy check (`config/dev.yaml` from `.example`).

## Documentation

| Document | Content |
|---|---|
| [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) | Architecture, components, concurrency, full configuration schema |
| [config/agent.yaml.example](config/agent.yaml.example) | Annotated configuration |
| [config/prompts/README.md](config/prompts/README.md) | Customizing prompts and the persona |
| [doc/DEBUGGING.md](doc/DEBUGGING.md) | Debug logging and troubleshooting |
| [doc/ISSUES.md](doc/ISSUES.md) | Known issues |
| [AGENTS.md](AGENTS.md) | Developer reference: commands, code map, conventions |
| [process/DEVELOPMENT_PROCESS.md](process/DEVELOPMENT_PROCESS.md) | Development process |
| [process/TESTING_WITH_MOCK_SIGNAL_API.md](process/TESTING_WITH_MOCK_SIGNAL_API.md) | Testing without a real Signal account |

## Third-party content

Media files from other sources that are included in this repository:

| Files | Source | License |
|---|---|---|
| `tests/fixtures/librivox_de_verwandlung.aac` | Kafka, *Die Verwandlung*, ch. 1, [LibriVox](https://archive.org/details/verwandlung_mw_0811_librivox) | Public domain |
| `tests/fixtures/librivox_en_metamorphosis.aac` | Kafka, *The Metamorphosis*, ch. 1, [LibriVox](https://archive.org/details/metamorphosis_librivox) | Public domain |
| `tests/fixtures/nasa_apollo11_small_step.aac` | Apollo 11 "one small step", NASA, via [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Neil_Armstrong_small_step.wav) | Public domain (US government work) |
| `deploy/dgx-spark/qwen3-tts/voices/developpeuse.safetensors` (voice embedding) | Kyutai recording `unmute-prod-website/developpeuse-3.wav`, [kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices) | CC0 |
| `deploy/dgx-spark/qwen3-tts/voices/es-villarraza.safetensors` (voice embedding) | LibriVox reader Victor Villarraza, Perrault, *Cuentos de Hadas*, ch. 1, [LibriVox](https://archive.org/details/cuentosdehadas_1701_librivox) | Public domain |

Details: [tests/fixtures/README.md](tests/fixtures/README.md), [deploy/dgx-spark/qwen3-tts/README.md](deploy/dgx-spark/qwen3-tts/README.md). Model weights and container images used at runtime are downloaded, not included; check their licenses before use.

## License

GPL-3.0, see [LICENSE](LICENSE).
