"""Node control plane for the hardware (Large-tier) test scenarios.

Talks to the DEBUG firmware build only: /api/* endpoints + the RNS-over-TCP
injection port (8001, SLIP-escaped framing). Never flashes, never reboots.
All scenario accounting uses COUNTER DELTAS (ack-layer counters are
cumulative since boot and have no reset endpoint).
"""
import json
import socket
import time
import urllib.request

TCP_RNS_PORT = 4242
API_TIMEOUT = 4.0


class Node:
    def __init__(self, ip, name):
        self.ip = ip
        self.name = name

    # ---------- /api ----------
    def get(self, endpoint):
        with urllib.request.urlopen(
                'http://%s/api/%s' % (self.ip, endpoint), timeout=API_TIMEOUT) as r:
            return json.loads(r.read().decode('utf-8'))

    def post(self, endpoint, body=None):
        data = json.dumps(body if body is not None else {}).encode('utf-8')
        req = urllib.request.Request('http://%s/api/%s' % (self.ip, endpoint),
                                     data=data,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
            return json.loads(r.read().decode('utf-8'))

    def radio(self):
        return self.get('get_stat')['radio']

    def ack(self):
        return self.get('ack_cfg')

    def fw_version(self):
        return self.get('get_stat')['device'].get('ver', '?')

    # ---------- RNS injection over TCP (SLIP framing, 0x7E + escapes) ----------
    @staticmethod
    def slip_frame(payload):
        out = bytearray([0x7E])
        for b in payload:
            if b == 0x7E:
                out += bytes([0x7D, 0x5E])
            elif b == 0x7D:
                out += bytes([0x7D, 0x5D])
            else:
                out.append(b)
        out.append(0x7E)
        return bytes(out)

    def inject_rns(self, payload, timeout=5.0):
        """Send one SLIP-framed RNS packet into the node's TCP->RF path."""
        with socket.create_connection((self.ip, TCP_RNS_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(self.slip_frame(payload))
            time.sleep(0.2)

    def inject_rns_many(self, payloads, timeout=90.0, gap=0.0):
        """Send many RNS packets over ONE TCP connection, like an RNS client
        blasting a resource transfer. TCP backpressure (if any) is the same
        a real client would see; no artificial pacing unless gap > 0."""
        with socket.create_connection((self.ip, TCP_RNS_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            for p in payloads:
                s.sendall(self.slip_frame(p))
                if gap > 0.0:
                    time.sleep(gap)

    def listen_tcp(self):
        """Open a persistent RX connection BEFORE injecting, so delivered
        frames stream to us live instead of relying on server-side ring
        buffering for late clients."""
        import socket as _s
        sock = _s.create_connection((self.ip, TCP_RNS_PORT), timeout=3.0)
        sock.settimeout(0.2)
        return sock

    @staticmethod
    def slip_unescape(frame):
        out = bytearray()
        esc = False
        for b in frame:
            if esc:
                out.append(b ^ 0x20)
                esc = False
            elif b == 0x7D:
                esc = True
            else:
                out.append(b)
        return bytes(out)

    @staticmethod
    def read_sock(sock, seconds):
        buf = b''
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        return [Node.slip_unescape(p) for p in buf.split(b'~') if p]

    def drain_tcp(self, seconds=1.0):
        """Connect and collect whatever the RF->TCP side pushes (SLIP frames)."""
        frames = []
        try:
            with socket.create_connection((self.ip, TCP_RNS_PORT), timeout=2.0) as s:
                s.settimeout(seconds)
                deadline = time.time() + seconds
                buf = b''
                while time.time() < deadline:
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buf += chunk
                # split on 0x7E markers
                for part in buf.split(b'\x7E'):
                    if part:
                        frames.append(bytes(part))
        except OSError:
            pass
        return frames


def build_rns_packet(dest_byte=0x42, payload_len=180, dtype=0, ptype=0,
                     header_type=0, hops=0, context=0x00, seed=0xA7):
    """Reticulum wire layout (Packet.py): byte0 flags, byte1 hops, dest[16],
    context, payload. dtype/ptype/header_type are the 2-bit fields."""
    assert dtype in (0, 1, 2, 3) and ptype in (0, 1, 2, 3) and header_type in (0, 1)
    assert hops < 16
    b0 = (header_type << 6) | (dtype << 2) | ptype
    pkt = bytes([b0, hops]) + bytes([dest_byte] * 16) + bytes([context])
    pkt += bytes(((seed + i) & 0xFF) for i in range(payload_len))
    return pkt
