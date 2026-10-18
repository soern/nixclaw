"""1:1 Signal voice calls: call control, audio I/O, speech segmentation.

rpc.py      - asyncio JSON-RPC client for signal-cli's call API
audio.py    - PulseAudio capture/playback (parec/pacat), WAV helpers
vad.py      - voice-activity detection + utterance segmentation
manager.py  - CallManager (call policy, lifecycle) and CallSession (per call)
"""
