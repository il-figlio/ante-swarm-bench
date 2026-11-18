"""Unit tests for the protocol helpers in sensor_client.py — the repo
suite. Run it with:

    python3 -m unittest discover -s tests -t . -p 'test_*.py'
"""
import io
import unittest

import sensor_client as sc


def wire(payload, corrupt=False):
    """Hand-rolled frame bytes — independent of sensor_client's
    helpers on purpose."""
    data = payload.encode("ascii")
    check = sc.MAGIC ^ len(data)
    for byte in data:
        check ^= byte
    if corrupt:
        check ^= 0xFF
    return bytes([sc.MAGIC, len(data)]) + data + bytes([check])


class TestFrameCheck(unittest.TestCase):
    def test_known_vector(self):
        # MAGIC ^ 4 ^ P ^ O ^ N ^ G == 0x7E^4^0x50^0x4F^0x4E^0x47
        self.assertEqual(sc.frame_check(4, b"PONG"), 0x6C)

    def test_xor_fold(self):
        self.assertEqual(sc.frame_check(2, b"OK"), 0x7E ^ 2 ^ 0x4F
                         ^ 0x4B)


class TestReadExact(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(sc.read_exact(io.BytesIO(b"abcdef"), 3),
                         b"abc")

    def test_eof_raises(self):
        with self.assertRaises(sc.DeviceError):
            sc.read_exact(io.BytesIO(b"ab"), 5)


class TestReadFrame(unittest.TestCase):
    def test_good_frame(self):
        self.assertEqual(sc.read_frame(io.BytesIO(wire("PONG"))),
                         "PONG")

    def test_bad_magic(self):
        with self.assertRaises(sc.FrameError):
            sc.read_frame(io.BytesIO(b"\x00\x02OK\x00"))

    def test_bad_checksum(self):
        with self.assertRaises(sc.FrameError):
            sc.read_frame(io.BytesIO(wire("PONG", corrupt=True)))

    def test_eof_mid_frame(self):
        with self.assertRaises(sc.DeviceError):
            sc.read_frame(io.BytesIO(wire("PONG")[:3]))


class TestCommandTable(unittest.TestCase):
    def test_expected_commands(self):
        self.assertEqual(
            sorted(sc.COMMANDS),
            ["calibrate", "identify", "ping", "read", "stream"])

    def test_arities(self):
        self.assertEqual(sc.COMMANDS["ping"][1], 0)
        self.assertEqual(sc.COMMANDS["identify"][1], 0)
        self.assertEqual(sc.COMMANDS["read"][1], 1)
        self.assertEqual(sc.COMMANDS["stream"][1], 1)
        self.assertEqual(sc.COMMANDS["calibrate"][1], 0)


class TestParseData(unittest.TestCase):
    def test_good_reply(self):
        self.assertEqual(sc.parse_data("DATA temp 21.50", "temp"),
                         21.50)

    def test_wrong_shape(self):
        with self.assertRaises(sc.DeviceError):
            sc.parse_data("ERR unknown channel", "temp")


if __name__ == "__main__":
    unittest.main()
