#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""tapio — read and write Ethernet frames on a macvtap character device.

This is the guest's data path. A QEMU guest on a macvtap is, to the kernel,
whatever has /dev/tapN open: QEMU reads and writes frames on that char device
and nothing else. So writing here is not a simulation of the guest — it IS the
guest, minus the emulated NIC.

────────────────────────────────────────────────────────────────────────────────
⭐ THE VIRTIO HEADER, AND THE FALSE FAIL IT ALREADY CAUSED

/dev/tapN is NOT a raw frame pipe. The kernel's macvtap_open() sets
IFF_VNET_HDR by default, with vnet_hdr_sz = sizeof(struct virtio_net_hdr). So
every frame on that device is PREFIXED by a virtio header, in BOTH directions:

    write:  [ vnet_hdr ][ dst | src | ethertype | body ]
    read:   [ vnet_hdr ][ dst | src | ethertype | body ]

Get this wrong and the two failures are silent and look like the wire:
  • WRITING a bare 64-byte Ethernet frame -> EINVAL. Nothing goes out.
  • READING and parsing the ethertype at offset 12 reads INTO THE VIRTIO HEADER.
    The frames arrive perfectly and you count zero of them.

Both of those happened on the first real run of prove-macvtap-guest.sh, and the
scorer reported them as "the guest's data path did not receive the real board"
and "nothing from the guest reached it" — i.e. it blamed the WIRE for a bug in
its own reader and writer. That is why this module exists as a shared, tested
helper instead of three copies inlined in shell heredocs.

⚠️ AND THE HEADER SIZE IS QUERIED, NEVER ASSUMED. It is 10 on a plain
virtio_net_hdr and 12 with mergeable rx buffers, and a caller that hardcodes
either will be wrong on some kernel and will express that wrongness as a wire
fault. TUNGETVNETHDRSZ tells us; we ask.
"""
from __future__ import annotations

import fcntl
import os
import select
import struct

# _IOC(dir, type, nr, size) — computed rather than pasted, because a wrong
# hex constant here fails as "the wire is broken".
def _IOC(d: int, t: str, nr: int, size: int) -> int:
    return (d << 30) | (size << 16) | (ord(t) << 8) | nr


TUNGETVNETHDRSZ = _IOC(2, "T", 215, 4)
TUNSETVNETHDRSZ = _IOC(1, "T", 216, 4)


class Tap:
    """An open macvtap char device that handles its own virtio header.

    Use as a context manager; the fd is closed on exit even on an exception,
    because a leaked fd on a macvtap keeps an endpoint alive on a shared LAN.
    """

    def __init__(self, dev: str):
        self.dev = dev
        self.fd = os.open(dev, os.O_RDWR)
        self.vnet_hdr_sz = self._query_vnet_hdr_sz()

    def _query_vnet_hdr_sz(self) -> int:
        """Ask the kernel how many bytes of virtio header this device carries.

        Returns 0 if the ioctl is unsupported (a plain tap with no vnet header),
        which is the correct answer for that case rather than a failure.
        """
        try:
            buf = fcntl.ioctl(self.fd, TUNGETVNETHDRSZ, struct.pack("i", 0))
            return struct.unpack("i", buf)[0]
        except OSError:
            return 0

    # ── framing ──────────────────────────────────────────────────────────────
    def write_frame(self, frame: bytes) -> int:
        """Write ONE Ethernet frame, prepending the virtio header if required.

        Raises OSError on failure — deliberately. The caller must not be able to
        mistake "I could not send" for "the peer did not receive"; those are
        different findings and only one of them is about the wire.
        """
        return os.write(self.fd, (b"\0" * self.vnet_hdr_sz) + frame)

    def read_frame(self, timeout: float = 0.3) -> bytes | None:
        """Read ONE Ethernet frame with the virtio header stripped, or None on timeout."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        buf = os.read(self.fd, 65536)
        if len(buf) <= self.vnet_hdr_sz:
            return None
        return buf[self.vnet_hdr_sz:]

    def selftest(self) -> tuple[bool, str]:
        """⭐ PROVE THE WRITE PATH WORKS BEFORE ANY VERDICT DEPENDS ON IT.

        Writes one well-formed frame and reports whether the kernel accepted it.
        This is the check whose absence turned a crashed writer into a reported
        wire failure: a sender that cannot send must make the run INCONCLUSIVE,
        never FAIL, and the only way to know the difference is to ask first.
        """
        probe = bytearray(64)
        probe[0:6] = b"\xff" * 6
        probe[6:12] = b"\x02\x00\x00\x00\x00\x01"
        struct.pack_into("!H", probe, 12, 0x88BF)   # in-block but unused by any node
        try:
            n = self.write_frame(bytes(probe))
        except OSError as exc:
            return (False, "write to %s FAILED: %s (vnet_hdr_sz=%d). The sender is "
                           "broken; any 'peer did not receive' below would be a "
                           "statement about THIS BUG, not about the wire."
                    % (self.dev, exc, self.vnet_hdr_sz))
        return (True, "write path OK (%d bytes accepted, vnet_hdr_sz=%d)"
                % (n, self.vnet_hdr_sz))

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "Tap":
        return self

    def __exit__(self, *a) -> None:
        self.close()
