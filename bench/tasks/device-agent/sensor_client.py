#!/usr/bin/env python3
"""podlink — client for the Podlet-1 sensor pod.

The pod is a small sensor head (temperature, humidity, pressure) that
speaks a framed protocol over a byte stream. On the bench the firmware
runs as a subprocess and this client talks to it over the child's
stdin/stdout pipes; on the gateway it is the same bytes on
``/dev/ttyS1``. Either way ``--device`` is the shell command that
launches the firmware and this client owns both pipes.

This file ships inside the deploy image. Standard library only, and
keep it lean — the image budget for the whole client is 24 KB.

Wire format (pod -> client), one frame per message::

    +------+------+==========+-------+
    | 0x7E | LEN  | payload  | CHECK |
    +------+------+==========+-------+

MAGIC is ``0x7E``, LEN the payload byte count, payload printable
ASCII, CHECK the XOR of MAGIC, LEN and every payload byte. Payloads
never contain the magic byte — the firmware only emits printable ASCII
below ``0x7E`` — so a receiver that loses framing can resync by
scanning forward for the next magic byte.

Commands are one ASCII line each (client -> pod)::

    PING            -> PONG
    READ <chan>     -> DATA <chan> <value>
    STREAM <n>      -> SAMPLE <seq> <chan> <value>  (n frames) then END
    CHANNELS        -> CHAN <chan> [<chan> ...]
    CALIBRATE       -> OK
    anything else   -> ERR <why>

Every connection opens with one banner frame from the pod,
``HELLO <model> <fw-version>``.
"""
import argparse
import shlex
import subprocess
import sys

MAGIC = 0x7E
MAX_PAYLOAD = 200


class DeviceError(Exception):
    """The pipe to the pod failed: EOF, a refused spawn, a write error."""


class FrameError(DeviceError):
    """A frame arrived that does not parse: bad magic, length, check."""


class UsageError(Exception):
    """The command line asked for something that makes no sense."""


def frame_check(length, payload):
    """The frame checksum: XOR of MAGIC, LEN and every payload byte."""
    check = MAGIC ^ length
    for byte in payload:
        check ^= byte
    return check


def encode_command(text):
    """One command line as it goes on the wire."""
    return text.encode("ascii") + b"\n"


def read_exact(stream, count):
    """Read exactly ``count`` bytes; DeviceError if the pipe closes."""
    buf = bytearray()
    while len(buf) < count:
        chunk = stream.read(count - len(buf))
        if not chunk:
            raise DeviceError("pod closed the pipe")
        buf += chunk
    return bytes(buf)


def read_frame(stream):
    """Read one frame from ``stream``; return the payload as text.

    FrameError on a bad magic byte, an implausible length, or a
    checksum mismatch; DeviceError when the pipe closes mid-frame.
    """
    magic = read_exact(stream, 1)[0]
    if magic != MAGIC:
        raise FrameError("bad magic byte 0x%02X" % magic)
    length = read_exact(stream, 1)[0]
    if length < 1 or length > MAX_PAYLOAD:
        raise FrameError("bad payload length %d" % length)
    payload = read_exact(stream, length)
    check = read_exact(stream, 1)[0]
    want = frame_check(length, payload)
    if check != want:
        raise FrameError(
            "bad checksum 0x%02X on %r, expected 0x%02X"
            % (check, payload, want))
    return payload.decode("ascii")


class Device:
    """One connection to the pod: a subprocess running its firmware."""

    def __init__(self, device_cmd):
        self.device_cmd = device_cmd
        self.proc = None
        self.model = None
        self.firmware = None
        self.connect()

    def connect(self):
        """Spawn the pod and read its HELLO banner."""
        try:
            self.proc = subprocess.Popen(
                shlex.split(self.device_cmd),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError as exc:
            raise DeviceError(
                "cannot start %r: %s" % (self.device_cmd, exc))
        hello = read_frame(self.proc.stdout)
        parts = hello.split(" ", 2)
        if len(parts) != 3 or parts[0] != "HELLO":
            raise FrameError("expected HELLO banner, got %r" % hello)
        self.model, self.firmware = parts[1], parts[2]

    def close(self):
        """Close the pipes and reap the pod process."""
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        self.proc.stdout.close()
        self.proc.wait()
        self.proc = None

    def send(self, command):
        """Write one command line to the pod."""
        try:
            self.proc.stdin.write(encode_command(command))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise DeviceError("pod closed the pipe: %s" % exc)

    def reply(self):
        """Read one reply frame from the pod."""
        return read_frame(self.proc.stdout)


def parse_data(payload, chan):
    """Value out of a ``DATA <chan> <value>`` reply, as a float."""
    parts = payload.split()
    if len(parts) != 3 or parts[0] != "DATA" or parts[1] != chan:
        raise DeviceError("unexpected reply %r" % payload)
    return float(parts[2])


def cmd_ping(dev, _args):
    """PING -> PONG, a liveness check."""
    dev.send("PING")
    return [dev.reply()]


def cmd_identify(dev, _args):
    """Print the model and firmware from the pod's HELLO banner."""
    return ["%s %s" % (dev.model, dev.firmware)]


def cmd_read(dev, args):
    """READ <chan> -> DATA <chan> <value>, one reading."""
    dev.send("READ %s" % args[0])
    return [dev.reply()]


def cmd_calibrate(dev, _args):
    """CALIBRATE -> OK, tare the ADC."""
    dev.send("CALIBRATE")
    return [dev.reply()]


def cmd_stream(dev, args):
    """STREAM <n> -> n SAMPLE frames, then END (not printed)."""
    try:
        count = int(args[0])
    except ValueError:
        raise UsageError("stream count must be an integer")
    if count < 1:
        raise UsageError("stream count must be positive")
    dev.send("STREAM %d" % count)
    lines = []
    while True:
        payload = dev.reply()
        if payload == "END":
            return lines
        lines.append(payload)


COMMANDS = {
    "ping": (cmd_ping, 0, "ping"),
    "identify": (cmd_identify, 0, "identify"),
    "read": (cmd_read, 1, "read <channel>"),
    "stream": (cmd_stream, 1, "stream <count>"),
    "calibrate": (cmd_calibrate, 0, "calibrate"),
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="sensor_client.py",
        description="Client for the Podlet-1 sensor pod.",
        epilog="replies are printed verbatim, one per line")
    parser.add_argument(
        "--device", required=True, metavar="CMD",
        help="shell command that launches the pod firmware")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("args", nargs="*")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    func, nargs, usage = COMMANDS[args.command]
    if len(args.args) != nargs:
        parser.error("usage: %s" % usage)

    dev = Device(args.device)
    try:
        for line in func(dev, args.args):
            print(line)
    except UsageError as exc:
        parser.error(str(exc))
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
