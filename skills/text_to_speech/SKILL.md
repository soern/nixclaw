# Text to Speech

Turn text into a spoken audio message the user can listen to on Signal.

## When to Use

**USE this skill when:**
- User asks you to read something out loud, say it, or send a voice message
- User asks for spoken output in a supported language
- User asks which voices you have, or wants a different voice → `list_tts_voices` / `set_tts_voice`

**DON'T use this skill when:**
- User asks you to transcribe a voice message they sent → that's automatic (STT), not this tool
- The requested language isn't supported (the tool returns "Unsupported TTS language" and lists what is) → tell the user which languages are available instead of guessing
- Text-to-speech is disabled (the tool returns "Text-to-speech is disabled")

## How to Use

1. Call `text_to_speech` with:
   - `text`: the text to speak, in the target language. Keep it reasonably short and natural to read aloud - split very long text into a few shorter messages rather than one huge block.
   - `language` (optional): defaults to the configured default language. Only pass a value you know is supported.
   - `voice` (optional): normally omit it - the chat's saved voice (or the configured default) is used. Pass it only for a one-off voice the user asks for just this once.
2. The tool returns a filename. Send it to the user with `send_signal_attachment` using that exact filename (add a short caption if you like).

## Voices

- `list_tts_voices` (optional `language`): shows the voice each language uses in this chat and the voices the speech server offers (built-in voices and cloned ones, with the language a clone was recorded in). Use it when the user asks which voices exist - don't guess names.
- `set_tts_voice` with `language` + `voice`: saves the voice for that language in this chat; it stays until changed. `voice=""` goes back to the default. Use this when the user wants a voice from now on ("use ryan for English"); use the per-call `voice` argument only for a single message.
- A cloned voice can usually speak other languages too, but sounds most natural in the language it was recorded in.

## Example

```
User: Sag mir auf Deutsch "Guten Morgen, wie geht es dir?"

Agent: (text_to_speech with text="Guten Morgen, wie geht es dir?", language="de")
(send_signal_attachment with the returned filename)

Hier ist die Sprachnachricht!
```

## Notes

- Which languages are supported depends on the deployment; `list_tts_voices` shows them. If a user asks for an unsupported language, say so plainly rather than guessing a code.
- Don't pass exaggeratedly long text - break it into natural, shorter chunks.
