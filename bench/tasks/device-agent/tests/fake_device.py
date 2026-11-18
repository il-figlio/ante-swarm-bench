#!/usr/bin/env python3
"""fake_device.py — Podlet-1 firmware stand-in for the bench checks.

Speaks the pod's stdio protocol deterministically from
tests/manifest.json; the checks spawn it as the client's --device
command::

    python3 tests/fake_device.py --scenario clean [--state-dir DIR]

Scenarios (``manifest["scenarios"]``):

clean    answer every command normally
corrupt  inject raw garbage bytes and a bad-checksum frame partway
         through STREAM, then carry on with the good frames
flaky    close the pipe partway through the first STREAM — on the
         first connection only; a marker file under --state-dir
         records the death, so the respawned pod is healthy

Everything is canned: no clocks, no randomness, no sleeps.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "manifest.json").read_text())

MAGIC = 0x7E


def wire_frame(payload, corrupt=False):
    """Encode ``payload`` (text) as one wire frame; ``corrupt`` flips
    the checksum so the receiver must reject it."""
    data = payload.encode("ascii")
    check = MAGIC ^ len(data)
    for byte in data:
        check ^= byte
    if corrupt:
        check ^= 0xFF
    return bytes([MAGIC, len(data)]) + data + bytes([check])


def write_raw(data):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def send(payload, corrupt=False):
    write_raw(wire_frame(payload, corrupt=corrupt))


class FakePod:
    """The scripted firmware: command in, canned frames out."""

    def __init__(self, scenario, state_dir):
        self.cfg = MANIFEST["scenarios"][scenario]
        self.state_dir = Path(state_dir) if state_dir else None
        self.read_counts = {}

    # -- flaky bookkeeping: die once ever, across respawns -------------

    def _marker(self):
        if self.state_dir is None:
            return None
        return self.state_dir / self.cfg.get("marker", "died")

    def died_before(self):
        marker = self._marker()
        return marker is not None and marker.exists()

    def mark_died(self):
        marker = self._marker()
        if marker is not None:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("died\n")

    def should_die_now(self, sent):
        """Drop the pipe after ``sent`` stream frames — first
        connection only."""
        return (self.cfg.get("die_after_frames") == sent
                and not self.died_before())

    def die_mid_frame(self, entry, seq):
        """Write a truncated frame, then the pipe closes: a brown-out."""
        chan, value = entry
        full = wire_frame("SAMPLE %d %s %.2f" % (seq, chan, value))
        write_raw(full[: self.cfg.get("partial_bytes", 0)])
        self.mark_died()
        sys.exit(3)

    # -- commands ------------------------------------------------------

    def handle(self, line):
        parts = line.split()
        if not parts:
            return
        op, args = parts[0].upper(), parts[1:]
        if op == "PING":
            send("PONG")
        elif op == "READ":
            self.do_read(args)
        elif op == "STREAM":
            self.do_stream(args)
        elif op == "CHANNELS":
            send("CHAN " + " ".join(MANIFEST["device"]["channels"]))
        elif op == "CALIBRATE":
            send("OK")
        else:
            send("ERR unknown command %r" % op)

    def do_read(self, args):
        chan = args[0] if args else ""
        values = MANIFEST["reads"].get(chan)
        if not values:
            send("ERR unknown channel %r" % chan)
            return
        index = self.read_counts.get(chan, 0)
        self.read_counts[chan] = index + 1
        send("DATA %s %.2f" % (chan, values[index % len(values)]))

    def do_stream(self, args):
        try:
            count = int(args[0])
        except (IndexError, ValueError):
            send("ERR stream needs a count")
            return
        entries = MANIFEST["stream"][:count]
        sent = 0
        for seq, (chan, value) in enumerate(entries):
            if self.should_die_now(sent):
                self.die_mid_frame(entries[seq], seq)
            send("SAMPLE %d %s %.2f" % (seq, chan, value))
            sent += 1
            self.inject_after(seq)
        send("END")

    def inject_after(self, seq):
        """Emit whatever corruption the scenario schedules after the
        stream frame with this sequence number."""
        for spec in self.cfg.get("inject", []):
            if spec.get("after_seq") != seq:
                continue
            if spec.get("kind") == "garbage":
                write_raw(bytes.fromhex(spec["hex"]))
            elif spec.get("kind") == "bad_checksum":
                send(spec["payload"], corrupt=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Podlet-1 firmware stand-in (scripted).")
    parser.add_argument("--scenario", default="clean",
                        choices=sorted(MANIFEST["scenarios"]))
    parser.add_argument("--state-dir", default=None,
                        help="where the flaky scenario records its "
                             "first death")
    args = parser.parse_args(argv)

    pod = FakePod(args.scenario, args.state_dir)
    send(MANIFEST["device"]["banner"])
    for raw in sys.stdin.buffer:
        pod.handle(raw.decode("ascii", "replace").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
