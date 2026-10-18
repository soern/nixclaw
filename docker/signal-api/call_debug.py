#!/usr/bin/env python3
"""PoC driver for signal-cli 1:1 voice calls (run inside the signal-api container).

Connects as an extra JSON-RPC client to the signal-cli daemon the REST wrapper
already runs (127.0.0.1:6001), subscribes to call events and, once a call is
CONNECTED, wires the call's PulseAudio devices:

  remote audio  -> <outputDeviceName>.monitor  -> (record) -> echo back
  echo / --play -> sink_for_<inputDeviceName>  -> remote

Usage:
  call_debug.py listen [--echo] [--play WAV] [--record WAV] [--max-seconds N]
  call_debug.py call +49... [same options]

listen auto-accepts the first incoming call. Ctrl-C hangs up.
Standard library only.
"""

import argparse
import json
import queue
import socket
import subprocess
import sys
import threading
import time
import wave

RATE, CHANNELS = 48000, 1
CHUNK_BYTES = RATE * 2 * CHANNELS // 50  # 20 ms of s16le
PA_FMT = [f"--rate={RATE}", f"--channels={CHANNELS}", "--format=s16le", "--raw", "--latency-msec=20"]


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class Rpc:
    def __init__(self, host="127.0.0.1", port=6001):
        self.sock = socket.create_connection((host, port))
        self.file = self.sock.makefile("rwb")
        self.next_id = 0
        self.pending = {}
        self.events = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for raw in self.file:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).put(msg)
            elif msg.get("method") == "callEvent":
                self.events.put(msg["params"]["result"])
            # "receive" notifications (normal messages) are ignored; the REST
            # wrapper's own connection still gets them.
        self.events.put(None)

    def call(self, method, timeout=30, **params):
        with self.lock:
            self.next_id += 1
            rid = str(self.next_id)
            box = queue.Queue()
            self.pending[rid] = box
            req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            self.file.write(json.dumps(req).encode() + b"\n")
            self.file.flush()
        msg = box.get(timeout=timeout)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result")


def find_input_sink(input_name):
    out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout
    names = [line.split("\t")[1] for line in out.splitlines() if "\t" in line]
    for name in names:
        if name == f"sink_for_{input_name}":
            return name
    for name in names:
        if input_name in name and not name.startswith("signal_output"):
            return name
    raise RuntimeError(f"no sink for {input_name!r} among {names}")


class AudioBridge:
    def __init__(self, input_name, output_name, echo, play, record):
        sink = find_input_sink(input_name)
        log(f"audio: reading {output_name}.monitor, writing {sink}")
        self.procs = []
        self.rec = None
        if record:
            self.rec = wave.open(record, "wb")
            self.rec.setnchannels(CHANNELS)
            self.rec.setsampwidth(2)
            self.rec.setframerate(RATE)
        self.parec = subprocess.Popen(["parec", "-d", f"{output_name}.monitor", *PA_FMT],
                                      stdout=subprocess.PIPE)
        self.procs.append(self.parec)
        self.pacat = None
        if echo:
            self.pacat = subprocess.Popen(["pacat", "-p", "-d", sink, *PA_FMT], stdin=subprocess.PIPE)
            self.procs.append(self.pacat)
        if play:
            self.procs.append(subprocess.Popen(["paplay", "-d", sink, play]))
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        received = 0
        while True:
            chunk = self.parec.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            received += len(chunk)
            if self.rec:
                self.rec.writeframes(chunk)
            if self.pacat:
                try:
                    self.pacat.stdin.write(chunk)
                    self.pacat.stdin.flush()
                except BrokenPipeError:
                    self.pacat = None
            if received % (RATE * 2 * 5) < CHUNK_BYTES:
                peak = max(abs(int.from_bytes(chunk[i:i + 2], "little", signed=True))
                           for i in range(0, len(chunk), 2))
                log(f"audio: {received / (RATE * 2):.0f}s received, current peak {peak}")

    def close(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        if self.rec:
            self.rec.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["listen", "call"])
    ap.add_argument("number", nargs="?")
    ap.add_argument("--account", help="bot number (default: first account of the daemon)")
    ap.add_argument("--echo", action="store_true", help="send remote audio straight back")
    ap.add_argument("--play", metavar="WAV", help="play a WAV into the call once connected")
    ap.add_argument("--record", metavar="WAV", help="record remote audio")
    ap.add_argument("--max-seconds", type=int, default=120, help="hang up after N s connected")
    args = ap.parse_args()
    if args.mode == "call" and not args.number:
        ap.error("call needs a number")

    rpc = Rpc()
    account = args.account or rpc.call("listAccounts")[0]["number"]
    log(f"account {account}")
    rpc.call("subscribeCallEvents", account=account)

    call_id = None
    if args.mode == "call":
        res = rpc.call("startCall", account=account, recipient=args.number)
        call_id = res["callId"]
        log(f"startCall -> {res}")
    else:
        log("waiting for an incoming call (Ctrl-C to quit)")

    bridge = None
    connected_at = None
    try:
        while True:
            try:
                ev = rpc.events.get(timeout=1)
            except queue.Empty:
                if connected_at and time.time() - connected_at > args.max_seconds:
                    log("max-seconds reached, hanging up")
                    break
                continue
            if ev is None:
                log("daemon connection closed")
                break
            log(f"callEvent {json.dumps(ev)}")
            if call_id is not None and ev["callId"] != call_id:
                continue
            state = ev["state"]
            if state == "RINGING_INCOMING" and call_id is None:
                call_id = ev["callId"]
                log(f"acceptCall -> {rpc.call('acceptCall', account=account, callId=call_id)}")
            elif state == "CONNECTED" and bridge is None:
                connected_at = time.time()
                bridge = AudioBridge(ev["inputDeviceName"], ev["outputDeviceName"],
                                     args.echo, args.play, args.record)
            elif state == "ENDED":
                call_id = None
                break
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        if bridge:
            bridge.close()
        if call_id is not None:
            try:
                rpc.call("hangupCall", account=account, callId=call_id, timeout=10)
                log("hung up")
            except Exception as e:  # noqa: BLE001 - PoC: report and exit
                log(f"hangupCall failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
