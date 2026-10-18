# Test fixtures

Audio files (ADTS AAC, mono, 44.1 kHz, like Signal voice messages). Serve them to the agent with
`./send_mock_message.sh -a <file> ...` (see `AGENTS.md`), e.g. to compare STT backends.

| File | Content | Source | License |
|---|---|---|---|
| `voice_test.aac` | German/English test sentence (espeak-ng) | `scripts/generate_voice_fixture.sh` | generated, part of this repo |
| `librivox_de_verwandlung.aac` | Clean German narration, 20 s | Kafka, *Die Verwandlung*, ch. 1 at 0:45, [LibriVox](https://archive.org/details/verwandlung_mw_0811_librivox) | Public domain |
| `librivox_en_metamorphosis.aac` | Clean English narration, 20 s | Kafka, *The Metamorphosis*, ch. 1 at 0:45, [LibriVox](https://archive.org/details/metamorphosis_librivox) | Public domain |
| `nasa_apollo11_small_step.aac` | Noisy radio speech, 24 s | Apollo 11 "one small step", NASA, via [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Neil_Armstrong_small_step.wav) | Public domain (US government work) |

`mock_sites/` (see its README) and `test_site/` are self-made test websites.
