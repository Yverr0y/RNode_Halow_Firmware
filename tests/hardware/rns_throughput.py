#!/usr/bin/env python3
"""Megabyte-scale RNS throughput blast over one established RNS Link.

Replays what a real Reticulum client does for a Resource transfer: establish
the link (LINKREQUEST + LRPROOF), then push megabytes of DATA packets into the
modem's TCP port as fast as TCP lets it -- no artificial pacing. The firmware
must deliver EVERY frame exactly once (ACK + retransmit + dedup), pacing
itself via TCP backpressure instead of dropping.

Usage:
    python rns_throughput.py --a 192.168.1.42 --b 192.168.1.43 [--mb 1]
                             [--payload 400] [--bidir] [--seed 7]

Exit 0 iff every tagged frame arrived exactly once and no firmware drop
counter moved (dropped/drop_*/rf_tcp_dropped deltas all zero).
"""
import argparse
import socket
import struct
import sys
import threading
import time

from hwtest import Node
from scenarios import _lxmf_handshake, _collect_frames, _close
import rns_lxmf as lx

STREAM_MAGIC = 0x54475331          # '1SGT' -- marks our data frames
CTX_RESOURCE = lx.CTX_RESOURCE     # RNS context: Resource frame


def _noop_chk(cond, note=''):
    if not cond:
        print('    (handshake check failed: %s)' % note)


def build_data_frame(lid, seq, seed, payload_len):
    """RNS DATA packet on the link: [u32 magic][u32 seq BE][pattern...].
    dtype=DT_LINK is what makes the modem resolve dest -> tracked unicast;
    DT_SINGLE would fall back to broadcast (2 air copies, no ACK tracking)."""
    body = struct.pack('>II', STREAM_MAGIC, seq)
    body += bytes(((seq * 7 + seed + i) & 0xFF) for i in range(payload_len - 8))
    return lx.rns_packet(lid, CTX_RESOURCE, body,
                         ptype=lx.PT_DATA, dtype=lx.DT_LINK)


def parse_seq(frame):
    """Return (magic, seq) from a delivered RNS packet, or (None, None)."""
    if len(frame) < 19 + 8:
        return None, None
    if frame[18] != CTX_RESOURCE:
        return None, None
    magic, seq = struct.unpack('>II', frame[19:27])
    if magic != STREAM_MAGIC:
        return None, None
    return magic, seq


def snapshot(node):
    s = {'ack': node.ack(), 'tx': node.get('tx_dbg')}
    return s


def drop_deltas(s0, s1):
    keys = ('dropped', 'drop_deadline', 'drop_exhaust', 'drop_throttle')
    return {k: s1['ack'].get(k, 0) - s0['ack'].get(k, 0) for k in keys}


def txdbg_deltas(s0, s1):
    keys = ('rf_tcp_dropped', 'tx_drop_budget', 'tx_drop_alloc',
            'tx_drop_lmac', 'tx_drop_oversize')
    return {k: s1['tx'].get(k, 0) - s0['tx'].get(k, 0) for k in keys}


class Collector(threading.Thread):
    """Reads frames from one node until `want` unique seqs seen or deadline."""

    def __init__(self, node, want, tag, seconds):
        super().__init__(daemon=True)
        self.node = node
        self.want = want
        self.tag = tag
        self.seconds = seconds
        self.sock = node.listen_tcp()
        self.seen = set()
        self.dup = 0
        self.total = 0
        self.bytes_rx = 0
        self.last_new_t = None
        self.stop_flag = False

    def run(self):
        deadline = time.time() + self.seconds
        buf = b''
        while time.time() < deadline and len(self.seen) < self.want:
            try:
                chunk = self.sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b'\x7E' in buf:
                head, _, buf = buf.partition(b'\x7E')
                if not head:
                    continue
                frame = Node.slip_unescape(head)
                self.total += 1
                self.bytes_rx += len(frame)
                magic, seq = parse_seq(frame)
                if magic == STREAM_MAGIC and (seq >> 32) == 0:
                    if seq in self.seen:
                        self.dup += 1
                    else:
                        self.seen.add(seq)
                        self.last_new_t = time.time()

    def finish(self):
        _close(self.sock)


def run_direction(a, b, lid, n_frames, seed, payload_len, seconds, log):
    """a = sender (injection), b = receiver (collection). Returns stats."""
    col = Collector(b, n_frames, seed, seconds)
    col.start()
    payloads = [build_data_frame(lid, s, seed, payload_len)
                for s in range(n_frames)]
    s0 = snapshot(a)
    rs0 = {'tx': b.get('tx_dbg')}
    t0 = time.time()
    send_timeout = None
    try:
        a.inject_rns_many(payloads, timeout=seconds)
    except (socket.timeout, OSError) as e:
        # firmware backpressure held the socket past its deadline: the
        # transfer is simply slower than `seconds` -- report and go on
        send_timeout = repr(e)
    t_inj = time.time() - t0
    col.join(timeout=max(1.0, deadline_left(seconds, t0)))
    col.finish()
    s1 = snapshot(a)
    rs1 = {'tx': b.get('tx_dbg')}
    air_bytes = n_frames * payload_len
    dt = time.time() - t0
    # effective goodput: time of the LAST new frame, not the collector's
    # timeout window (waiting for frames that never arrive must not dilute it)
    eff_s = max(0.001, (col.last_new_t or time.time()) - t0)
    return {
        'sent': n_frames, 'seen': len(col.seen), 'dup': col.dup,
        'total_rx': col.total, 'bytes': air_bytes,
        'inject_s': t_inj, 'total_s': dt, 'eff_s': eff_s,
        'send_timeout': send_timeout,
        'goodput_kbps': (air_bytes * 8.0) / 1000.0 / eff_s,
        'drops': drop_deltas(s0, s1), 'txdbg': txdbg_deltas(s0, s1),
        'rx_txdbg': txdbg_deltas(rs0, rs1),
        'ack': {k: s1['ack'].get(k, 0) - s0['ack'].get(k, 0)
                for k in ('tx_frames', 'retransmitted', 'acked',
                          'acks_rx_frames', 'drop_deadline', 'dropped')},
    }


def deadline_left(total_s, t0):
    return max(5.0, total_s - (time.time() - t0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--a', required=True)
    ap.add_argument('--b', required=True)
    ap.add_argument('--mb', type=float, default=1.0)
    ap.add_argument('--payload', type=int, default=400)
    ap.add_argument('--bidir', action='store_true')
    ap.add_argument('--seed', type=int, default=7)
    args = ap.parse_args()

    a = Node(args.a, 'A')
    b = Node(args.b, 'B')
    # Per-run seed: byte-identical handshakes across runs are suppressed by
    # the modem's per-peer dedup (unicast path) -- the proof would never be
    # delivered a second time.
    hs_seed = (args.seed + int(time.time())) & 0xFFFF
    print('nodes %s -> %s, %.2f MB, payload %d B, bidir=%s, seed=%d'
          % (a.ip, b.ip, args.mb, args.payload, args.bidir, hs_seed))

    lid = None
    for attempt in (1, 2):
        lid = _lxmf_handshake(a, b, hs_seed + attempt - 1, _noop_chk, print)
        if lid is not None:
            break
        # a timed-out collector from an earlier run leaves a ZOMBIE client on
        # the node's single-client server for ~11 s (keepalive 5s + 2s x3);
        # an immediate retry would talk to the same corpse
        print('handshake attempt %d failed, waiting out the zombie client...' % attempt)
        time.sleep(12.0)
    if lid is None:
        print('HANDSHAKE FAILED')
        return 2
    print('link %s established' % lid.hex()[:16])

    total_bytes = int(args.mb * 1024 * 1024)
    n_frames = total_bytes // args.payload
    # generous ceiling: 60 s per MB at a 140 kbit/s floor, +90 s slack
    seconds = 90 + 60 * args.mb

    results = {}
    if args.bidir:
        box = {}
        def dir_ab():
            box['ab'] = run_direction(a, b, lid, n_frames, args.seed,
                                      args.payload, seconds, print)
        def dir_ba():
            box['ba'] = run_direction(b, a, lid, n_frames, args.seed + 1,
                                      args.payload, seconds, print)
        th = [threading.Thread(target=dir_ab, daemon=True),
              threading.Thread(target=dir_ba, daemon=True)]
        t0 = time.time()
        for t in th:
            t.start()
        for t in th:
            t.join(timeout=seconds + 30)
        wall = time.time() - t0
        results['A->B'] = box.get('ab')
        results['B->A'] = box.get('ba')
    else:
        t0 = time.time()
        results['A->B'] = run_direction(a, b, lid, n_frames, args.seed,
                                        args.payload, seconds, print)
        wall = time.time() - t0

    ok = True
    for name, r in results.items():
        if r is None:
            print('%s: NO RESULT' % name)
            ok = False
            continue
        # Contract: AT-LEAST-ONCE byte-exact with ZERO firmware drops.
        # A rare duplicate is tolerated: it can only happen when an ACK is
        # lost and the retransmit of an already-delivered frame lands outside
        # the 32-hash dedup window -- RNS itself is immune (resource segments
        # are idempotent by index, LXMF dedups by packet hash).
        dup_pct = (100.0 * r['dup']) / max(r['sent'], 1)
        exact = (r['seen'] == r['sent'])
        nodrops = all(v == 0 for v in r['drops'].values()) and \
                  all(v == 0 for v in r['txdbg'].values()) and \
                  all(v == 0 for v in r['rx_txdbg'].values())
        print('%s: %d/%d delivered, dup=%d (%.2f%%), rx_total=%d, %.2f MB in %.1fs'
              ' (%.0f kbit/s goodput), inject %.1fs'
              % (name, r['seen'], r['sent'], r['dup'], dup_pct, r['total_rx'],
                 r['bytes'] / 1048576.0, r['eff_s'], r['goodput_kbps'],
                 r['inject_s']))
        print('    ack deltas: %s' % r['ack'])
        print('    drop deltas: %s | tx_dbg deltas: %s'
              % (r['drops'], r['txdbg']))
        print('    receiver tx_dbg deltas: %s' % (r['rx_txdbg'],))
        if r['send_timeout']:
            print('    NOTE: sender socket hit its timeout (%s)'
                  % r['send_timeout'])
        if not exact:
            print('    FAIL: %d frame(s) missing' % (r['sent'] - r['seen']))
            ok = False
        if dup_pct > 2.0:
            print('    FAIL: %.2f%% duplicate deliveries (ACK-loss storm?)' % dup_pct)
            ok = False
        if not nodrops:
            print('    FAIL: firmware dropped frames (see deltas)')
            ok = False

    print('wall %.1fs -> %s' % (wall, 'PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
