#!/bin/bash
# Regenerate tests/fixtures/voice_test.aac from TTS.
#
# Requires `espeak-ng` on PATH. Output: ADTS AAC, mono, 44.1 kHz — same
# shape as real Signal voice messages in data/+491234567891/attachments/.
#
# The text is a mix of English (the canonical "quick brown fox" pangram)
# and a German greeting so we can verify language detection or forced
# language="de" both produce something recognisable.

set -euo pipefail

OUT="${1:-tests/fixtures/voice_test.aac}"
TMP_WAV="$(mktemp --suffix=.wav)"
trap 'rm -f "$TMP_WAV"' EXIT

if ! command -v espeak-ng >/dev/null 2>&1; then
    echo "espeak-ng not on PATH" >&2
    exit 1
fi

mkdir -p "$(dirname "$OUT")"

espeak-ng -v de -s 140 \
    "The quick brown fox jumps over the lazy dog. Hallo und herzlich willkommen. \
Dies ist eine Testnachricht fuer die Spracherkennung." \
    -w "$TMP_WAV"

ffmpeg -y -i "$TMP_WAV" -c:a aac -b:a 64k -ar 44100 -ac 1 "$OUT" >/dev/null 2>&1

ls -la "$OUT"
echo "Done. Test with: curl -F file=@$OUT -F language=de localhost:8001/v1/audio/transcriptions"