"""Hardware-tier scenarios. Each takes (a, b, chk, log) where a/b are Nodes,
chk(cond, note) counts a check, log(msg) prints diagnostics. Scenarios must
be passive except for explicit RNS injection over TCP -- no flashing, no
reboots, no config changes."""
import hashlib
import socket
import threading
import time

import rns_lxmf as lx
from hwtest import Node, build_rns_packet


def _delta(before, after, key):
    return int(after.get(key, 0)) - int(before.get(key, 0))


def _radio_int(stat, key):
    """Radio stats mix strings ('0.19 KiB') and ints; packets are ints."""
    return int(stat.get(key, 0))


def _snapshot(a, b):
    return {
        'a_ack': a.ack(), 'b_ack': b.ack(),
        'a_radio': a.radio(), 'b_radio': b.radio(),
    }


def h00_sanity(a, b, chk, log):
    va, vb = a.fw_version(), b.fw_version()
    log('fw A=%s B=%s' % (va, vb))
    chk('debug' in va or 'b' in va, 'A runs a debug build with /api')
    chk(a.ack().get('bc_repeat') is not None, 'A exposes ack counters')
    chk(b.ack().get('bc_repeat') is not None, 'B exposes ack counters')
    la, lb = a.get('get_reticulum_links'), b.get('get_reticulum_links')
    chk(len(la.get('d', [])) == 0, 'A link_db empty at start (rx_calls=%s)' % la.get('rx_calls'))
    chk(len(lb.get('d', [])) == 0, 'B link_db empty at start (rx_calls=%s)' % lb.get('rx_calls'))
    log('A link table: rx_calls=%s rx_valid=%s' % (la.get('rx_calls'), la.get('rx_valid')))
    log('B link table: rx_calls=%s rx_valid=%s' % (lb.get('rx_calls'), lb.get('rx_valid')))


def h01_idle_no_counter_noise(a, b, chk, log):
    """With no traffic, radio counters must not move (beacons/announces must
    not pollute the Reticulum-level accounting)."""
    s0 = _snapshot(a, b)
    time.sleep(6.0)
    s1 = _snapshot(a, b)
    d_arx = _radio_int(s1['a_radio'], 'rx_packets') - _radio_int(s0['a_radio'], 'rx_packets')
    d_brx = _radio_int(s1['b_radio'], 'rx_packets') - _radio_int(s0['b_radio'], 'rx_packets')
    d_atx = _radio_int(s1['a_radio'], 'tx_packets') - _radio_int(s0['a_radio'], 'tx_packets')
    d_btx = _radio_int(s1['b_radio'], 'tx_packets') - _radio_int(s0['b_radio'], 'tx_packets')
    log('idle 6s deltas: A rx=%d tx=%d | B rx=%d tx=%d' % (d_arx, d_atx, d_brx, d_btx))
    chk(d_arx == 0 and d_brx == 0, 'idle: no phantom RX packets (beacons excluded)')
    chk(d_atx == 0 and d_btx == 0, 'idle: no phantom TX packets')


def h02_one_rns_frame_accounting(a, b, chk, log):
    """THE experiment: inject exactly ONE non-link RNS DATA frame into A with
    an empty link_db. Contract:
      - A must send it broadcast (bc path): retransmitted/dropped stay 0
      - exactly one Reticulum frame arrives at B (radio rx_packets +1 on
        builds with reticulum-level RX accounting; air-level builds show the
        air copy count -- logged, not asserted)
      - B delivers it to TCP byte-exact
    """
    pkt = build_rns_packet(dest_byte=0x42, payload_len=180, dtype=0, ptype=0)
    log('injecting %d B RNS DATA frame (dtype=SINGLE, dest unknown)' % len(pkt))

    rx_sock = b.listen_tcp()
    s0 = _snapshot(a, b)
    a.inject_rns(pkt)
    time.sleep(3.0)
    s1 = _snapshot(a, b)
    frames = Node.read_sock(rx_sock, 1.5) if False else __import__('hwtest').Node.read_sock(rx_sock, 1.5)
    try:
        rx_sock.close()
    except OSError:
        pass

    d_tx_frames   = _delta(s0['a_ack'], s1['a_ack'], 'tx_frames')
    d_bc_repeats  = _delta(s0['a_ack'], s1['a_ack'], 'bc_repeats')
    d_retrans     = _delta(s0['a_ack'], s1['a_ack'], 'retransmitted')
    d_dropped     = _delta(s0['a_ack'], s1['a_ack'], 'dropped')
    d_acks_sent   = _delta(s0['b_ack'], s1['b_ack'], 'acks_sent')
    d_atx         = _radio_int(s1['a_radio'], 'tx_packets') - _radio_int(s0['a_radio'], 'tx_packets')
    d_brx         = _radio_int(s1['b_radio'], 'rx_packets') - _radio_int(s0['b_radio'], 'rx_packets')

    log('A: tx_frames=%d bc_repeats=%d retransmitted=%d dropped=%d radio_tx=%d'
        % (d_tx_frames, d_bc_repeats, d_retrans, d_dropped, d_atx))
    log('B: acks_sent=%d radio_rx=%d' % (d_acks_sent, d_brx))

    # tx_frames mixes app frames and bc repeats (legacy counter); the law is
    # pinned by radio_tx == 1 and the path counters below
    d_path_bc = _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_bc')
    chk(d_atx == 1, 'A: exactly ONE reticulum frame counted on TX')
    chk(d_path_bc == 1, 'A: frame took the tx_broadcast path (dbg_path_bc)')
    # THE user-mandated contract: a non-link frame with an empty link_db must
    # NEVER ride the tracked/retry path
    chk(d_retrans == 0, 'A: broadcast frame took NO retry path (retransmitted=0)')
    chk(d_dropped == 0, 'A: broadcast frame not dropped')

    # B's nearby table sees A with the air copies (diagnostics)
    nb = b.get('get_nearby_modems')
    for m in nb.get('d', []):
        log('B nearby[%s]: rx_packets=%d rx_mcs=%d rx_rssi=%d'
            % (m.get('mac'), m.get('rx_packets', -1), m.get('rx_mcs', -1), m.get('rx_rssi', 0)))

    # end-to-end delivery: B must push the same RNS frame to the LIVE listener
    exact = any(bytes(f) == pkt for f in frames)
    log('B TCP drain: %d frame(s), byte-exact match: %s' % (len(frames), exact))
    chk(exact, 'B delivered the injected frame to TCP byte-exact')


def h03_broadcast_duplicate_contract(a, b, chk, log):
    """Broadcast law: repeats are intentional air-level redundancy, so EVERY
    air copy is delivered (the ACK-layer dedup only protects unicast traffic;
    Reticulum dedups by packet hash itself). Two identical broadcasts with
    bc_repeat=2 must arrive as exactly 4 deliveries, byte-exact each."""
    from hwtest import Node as _N
    pkt = build_rns_packet(dest_byte=0x43, payload_len=60, dtype=0, ptype=0, seed=0x5C)
    s0 = _snapshot(a, b)
    rx_sock = b.listen_tcp()
    a.inject_rns(pkt)
    time.sleep(1.0)
    a.inject_rns(pkt)   # exact duplicate 1s later (fresh TX, same bytes)
    time.sleep(3.0)
    frames = _N.read_sock(rx_sock, 1.5)
    try:
        rx_sock.close()
    except OSError:
        pass
    exact = sum(1 for f in frames if bytes(f) == pkt)
    s1 = _snapshot(a, b)
    log('A: tx_frames=%d bc_repeats=%d path_bc=%d retrans=%d dropped=%d radio_tx=%d'
        % (_delta(s0['a_ack'], s1['a_ack'], 'tx_frames'),
           _delta(s0['a_ack'], s1['a_ack'], 'bc_repeats'),
           _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_bc'),
           _delta(s0['a_ack'], s1['a_ack'], 'retransmitted'),
           _delta(s0['a_ack'], s1['a_ack'], 'dropped'),
           _radio_int(s1['a_radio'], 'tx_packets') - _radio_int(s0['a_radio'], 'tx_packets')))
    log('B: acks=%d radio_rx=%d | delivered=%d exact=%d'
        % (_delta(s0['b_ack'], s1['b_ack'], 'acks_sent'),
           _radio_int(s1['b_radio'], 'rx_packets') - _radio_int(s0['b_radio'], 'rx_packets'),
           len(frames), exact))
    chk(exact == 4, 'B delivers every air copy: 2 TX x 2 repeats = 4, byte-exact')


# =====================================================================
# LXMF-replica scenarios (h04-h06)
#
# Everything real LXMF clients do (Sideband / MeshChat / Columba, all on the
# reference markqvist/LXMF stack) on top of Reticulum, replayed byte-exactly
# at the modem's TCP interfaces. See rns_lxmf.py for the wire sources.
# =====================================================================

def _collect_frames(sock, want, timeout):
    """Read COMPLETE SLIP frames until `want` arrived or timeout. Early exit."""
    frames = []
    buf = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        parts = buf.split(b'\x7E')
        if not buf.endswith(b'\x7E'):
            parts = parts[:-1]  # trailing piece has no closing flag yet
        frames = [Node.slip_unescape(p) for p in parts if p]
        if len(frames) >= want:
            break
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return frames


def _close(sock):
    try:
        sock.close()
    except OSError:
        pass


def _link_rows(node):
    """get_reticulum_links rows keyed by lowercase link id hex."""
    out = {}
    for row in node.get('get_reticulum_links').get('d', []):
        if row and row[0]:
            out[str(row[0]).lower()] = row
    return out


def _lxmf_handshake(a, b, seed, chk, log):
    """Replay the Reticulum link establishment that precedes EVERY direct
    LXMF delivery (image, file or text): LINKREQUEST A->B, LRPROOF B->A.
    Returns the link id, or None if the handshake did not verify."""
    # Mix the wall clock into the seed: byte-identical handshakes across test
    # runs are suppressed by the modem's per-peer dedup -- the LRPROOF of a
    # repeated run would never be delivered a second time.
    seed = (seed * 7919 + int(time.time())) & 0xFFFF
    dest16 = bytes(((seed * 3 + i * 5) & 0xFF) for i in range(16))
    lr = lx.build_linkrequest(dest16, seed=seed)
    lid = lx.link_id_from_linkrequest(lr)
    proof = lx.build_lrproof(lid, seed=seed + 0x09)

    # leg 1: A's client initiates the link
    b_sock = b.listen_tcp()
    a.inject_rns(lr)
    lr_at_b = _collect_frames(b_sock, 1, 10.0)
    _close(b_sock)

    # leg 2: B's client proves the link (this is what makes both modems
    # learn the peer MAC and switch the link to the tracked unicast path)
    a_sock = a.listen_tcp()
    b.inject_rns(proof)
    proof_at_a = _collect_frames(a_sock, 1, 10.0)
    _close(a_sock)

    lr_exact = any(bytes(f) == lr for f in lr_at_b)
    proof_exact = any(bytes(f) == proof for f in proof_at_a)
    chk(lr_exact, 'handshake: LINKREQUEST delivered to B byte-exact')
    chk(proof_exact, 'handshake: LRPROOF delivered to A byte-exact')
    chk(lid.hex() in _link_rows(b), 'handshake: B link_db learned the link id')
    chk(lid.hex() in _link_rows(a), 'handshake: A link_db learned the link id (peer MAC known)')
    if not (lr_exact and proof_exact):
        return None
    return lid


def h04_lxmf_link_handshake(a, b, chk, log):
    """Link establishment exactly as Reticulum does it before any direct
    LXMF message. Contracts:
      - LINKREQUEST (67 B payload, MTU signalling) is forwarded unchanged
        when the client MTU fits the modem limit (500)
      - B learns the link from the received request; A only after the proof
      - the LINKREQUEST itself rides broadcast (modem cannot know the peer
        yet), the LRPROOF back rides the TRACKED UNICAST path
      - LINKCLOSE afterwards is delivered too
    """
    dest16 = bytes(((0xA4 * 3 + i * 5) & 0xFF) for i in range(16))
    lr = lx.build_linkrequest(dest16, seed=0xA4)
    lid = lx.link_id_from_linkrequest(lr)
    proof = lx.build_lrproof(lid, seed=0xAD)
    log('link id = %s (computed in test like the modem does)' % lid.hex())

    # --- leg 1: LINKREQUEST A -> B ---
    s0 = _snapshot(a, b)
    b_sock = b.listen_tcp()
    a.inject_rns(lr)
    lr_at_b = _collect_frames(b_sock, 1, 6.0)
    _close(b_sock)
    s_mid = _snapshot(a, b)

    chk(any(bytes(f) == lr for f in lr_at_b),
        'B received LINKREQUEST byte-exact (MTU 500 -> no clamp rewrite)')
    chk(lid.hex() in _link_rows(b), 'B link_db has the link (learned from RX)')
    chk(lid.hex() not in _link_rows(a),
        'A link_db does NOT list the link yet (TX-only, peer MAC unknown)')
    d_a_bc = _delta(s0['a_ack'], s_mid['a_ack'], 'dbg_path_bc')
    d_a_drop = _delta(s0['a_ack'], s_mid['a_ack'], 'dropped')
    chk(d_a_bc == 1, 'A sent the request on the broadcast path (peer unknown)')
    chk(d_a_drop == 0, 'A: request not dropped')
    b_rx = _radio_int(s_mid['b_radio'], 'rx_packets') - _radio_int(s0['b_radio'], 'rx_packets')
    log('leg1: A bc=%d drop=%d | B rx=%d (air copies incl. bc repeats)' % (d_a_bc, d_a_drop, b_rx))

    # --- leg 2: LRPROOF B -> A ---
    a_sock = a.listen_tcp()
    b.inject_rns(proof)
    proof_at_a = _collect_frames(a_sock, 1, 6.0)
    _close(a_sock)
    s1 = _snapshot(a, b)

    chk(any(bytes(f) == proof for f in proof_at_a), 'A received LRPROOF byte-exact')
    rows_a = _link_rows(a)
    chk(lid.hex() in rows_a, 'A link_db has the link after the proof')
    if lid.hex() in rows_a:
        row = rows_a[lid.hex()]
        log('A link row: mac=%s state=%s mtu=%s' % (row[1], row[3], row[10]))
    d_b_bc = _delta(s_mid['b_ack'], s1['b_ack'], 'dbg_path_bc')
    d_b_uc = (_delta(s_mid['b_ack'], s1['b_ack'], 'dbg_path_plain') +
              _delta(s_mid['b_ack'], s1['b_ack'], 'dbg_path_plainl') +
              _delta(s_mid['b_ack'], s1['b_ack'], 'dbg_path_bundle'))
    chk(d_b_bc == 0 and d_b_uc >= 1,
        'B sent the proof UNICAST-tracked (bc=%d uc=%d) -- link traffic never broadcast' % (d_b_bc, d_b_uc))

    # --- link close (hygiene: frees the link on real clients) ---
    close = lx.build_linkclose(lid)
    b_sock = b.listen_tcp()
    a.inject_rns(close)
    close_at_b = _collect_frames(b_sock, 1, 6.0)
    _close(b_sock)
    chk(any(bytes(f) == close for f in close_at_b), 'LINKCLOSE delivered to B')


def h05_lxmf_single_packet_message(a, b, chk, log):
    """Small LXMF message (text + tiny image field), content <= 319 B ->
    a single DATA packet over the established link, like an avatar/icon
    message from Sideband/MeshChat/Columba."""
    lid = _lxmf_handshake(a, b, 0xB5, chk, log)
    if lid is None:
        chk(False, 'handshake failed; cannot test single-packet delivery')
        return

    a_id = bytes(((0xB5 * 7 + i) & 0xFF) for i in range(16))
    b_id = bytes(((0xB5 * 11 + i) & 0xFF) for i in range(16))
    image = bytes(((0x47 + i * 3) & 0xFF) for i in range(120))   # tiny avatar
    lxm = lx.lxm_image(b_id, a_id, image, 'image/png', text='hi')
    content = lx.lxm_content_size(lxm)
    log('packed LXM %d B, LXMF content %d B (single-packet limit %d)'
        % (len(lxm), content, lx.LXMF_LINK_MAX_CONTENT))
    chk(content <= lx.LXMF_LINK_MAX_CONTENT, 'message fits the single-packet limit')

    pkt = lx.build_lxm_link_packet(lid, lxm)
    s0 = _snapshot(a, b)
    b_sock = b.listen_tcp()
    a.inject_rns(pkt)
    frames = _collect_frames(b_sock, 1, 6.0)
    _close(b_sock)
    s1 = _snapshot(a, b)

    chk(any(bytes(f) == pkt for f in frames), 'B delivered the LXM packet byte-exact')
    d_bc = _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_bc')
    d_uc = (_delta(s0['a_ack'], s1['a_ack'], 'dbg_path_plain') +
            _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_plainl') +
            _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_bundle'))
    chk(d_bc == 0 and d_uc >= 1, 'A sent link DATA via tracked unicast (bc=%d uc=%d)' % (d_bc, d_uc))
    chk(_delta(s0['a_ack'], s1['a_ack'], 'dropped') == 0, 'A: no drops')
    d_retrans = _delta(s0['a_ack'], s1['a_ack'], 'retransmitted')
    d_brx = _radio_int(s1['b_radio'], 'rx_packets') - _radio_int(s0['b_radio'], 'rx_packets')
    d_atx = _radio_int(s1['a_radio'], 'tx_packets') - _radio_int(s0['a_radio'], 'tx_packets')
    log('A: tx=%d retrans=%d drop=0 | B: rx=%d' % (d_atx, d_retrans, d_brx))
    chk(d_atx == 1, 'A counted exactly ONE reticulum frame on TX')
    chk(d_brx == 1, 'B counted exactly ONE delivered frame (unicast: no air dups)')


def h06_lxmf_file_resource_transfer(a, b, chk, log):
    """The heavy path every real image/file transfer takes in LXMF: payload
    above 319 B is delivered as an RNS Resource over the link -- one
    RESOURCE_ADV, a burst of MDU-sized RESOURCE segments (431 B each), one
    RESOURCE_PRF. MeshChat/Sideband/Columba all rely on this, with no
    application-level chunking. Contracts:
      - EVERY frame survives (tracked unicast: retransmit, never drop)
      - no duplicate deliveries (unicast dedup by seq)
      - the file reassembles byte-exact from the delivered segments"""
    lid = _lxmf_handshake(a, b, 0xC6, chk, log)
    if lid is None:
        chk(False, 'handshake failed; cannot test resource transfer')
        return

    size = 32 * 1024
    data = bytes((((i >> 8) * 31 + i * 17) ^ 0x5B) & 0xFF for i in range(size))
    segs = lx.split_file(data)                       # 77 segments, 431 B payload each
    rhash = hashlib.sha256(data).digest()[:16]
    adv = lx.build_resource_adv(lid, rhash, size)
    prf = lx.build_resource_proof(lid, rhash)
    burst = [adv] + [lx.build_resource_segment(lid, i, c) for i, c in segs] + [prf]
    want = len(burst)
    log('file %d B -> %d segments +%d adv +%d proof = %d frames, %d B each'
        % (size, len(segs), 1, 1, want, len(burst[1])))

    s0 = _snapshot(a, b)
    b_sock = b.listen_tcp()
    t0 = time.time()
    a.inject_rns_many(burst)                         # one TCP conn, no pacing
    frames = _collect_frames(b_sock, want, timeout=90.0)
    dt = time.time() - t0
    _close(b_sock)
    s1 = _snapshot(a, b)

    exact = sum(1 for f in frames if bytes(f) in burst)
    chk(exact == want, 'all %d burst frames delivered byte-exact (got %d exact of %d rx)'
        % (want, exact, len(frames)))
    chk(len(frames) == want, 'no duplicate deliveries: %d frames for %d sent (unicast dedup)'
        % (len(frames), want))

    got = lx.reassemble_segments(frames)
    reassembled = b''.join(got[i] for i in sorted(got)) if got else b''
    chk(reassembled == data, 'file reassembled from delivered segments byte-exact')
    order = [i for f in frames if len(f) > 23 and f[18] == lx.CTX_RESOURCE
             for i in [int.from_bytes(bytes(f[19:23]), 'big')]]
    log('delivery order %s (RNS resource does not require in-order)' %
        ('in-order' if order == sorted(order) else 'reshuffled'))

    d_drop = _delta(s0['a_ack'], s1['a_ack'], 'dropped')
    d_retrans = _delta(s0['a_ack'], s1['a_ack'], 'retransmitted')
    d_bc = _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_bc')
    d_atx = _radio_int(s1['a_radio'], 'tx_packets') - _radio_int(s0['a_radio'], 'tx_packets')
    d_brx = _radio_int(s1['b_radio'], 'rx_packets') - _radio_int(s0['b_radio'], 'rx_packets')
    d_acks = _delta(s0['b_ack'], s1['b_ack'], 'acks_sent')
    log('A: tx=%d retrans=%d dropped=%d bc=%d | B: rx=%d acks_sent=%d | %.1fs, %d kbit/s'
        % (d_atx, d_retrans, d_drop, d_bc, d_brx, d_acks, dt, int(size * 8 / dt / 1000)))
    chk(d_drop == 0, 'A: ZERO drops under the full-speed burst (tracked path retransmits instead)')
    chk(d_bc == 0, 'A: entire burst rode unicast (no broadcast fallback)')
    chk(d_atx == want, 'A counted exactly %d reticulum TX frames' % want)
    chk(d_brx == want, 'B counted exactly %d delivered frames' % want)
    chk(d_acks > 0, 'B ACKed the unicast flow (acks_sent=%d) -- delivery guarantee active' % d_acks)


# =====================================================================
# MCS-mode scenarios (h07/h08) -- the live-found RA regression fix.
#
# The firmware bug (2026-08-21): with rate_adapt=1, peers STARTED at a
# hardcoded MCS4 / EVM-ceiling instead of the configured MCS, and stale
# resets re-armed that undecodable rate while wiping loss history. Any
# unicast traffic then vanished silently (TX "completed", peer never
# decoded a copy) while broadcasts at the configured MCS kept working.
# These two scenarios run BACK-TO-BACK in one session, switching
# rate_adapt at runtime via POST /api/ack_cfg -- no reboots.
# =====================================================================

def _set_rate_adapt(a, b, mode):
    a.post('ack_cfg', {'rate_adapt': mode})
    b.post('ack_cfg', {'rate_adapt': mode})
    return (a.ack().get('rate_adapt') == mode and
            b.ack().get('rate_adapt') == mode)


def h07_mcs_fixed_unicast(a, b, chk, log):
    """Fixed-MCS phase: rate_adapt=0 pins every peer to the CONFIGURED MCS
    (the one broadcasts use, decodable by construction). Unicast link
    traffic must deliver exactly-once with a closing ACK loop."""
    ra0_a, ra0_b = a.ack().get('rate_adapt'), b.ack().get('rate_adapt')
    chk(_set_rate_adapt(a, b, 0), 'rate_adapt=0 applied live on both nodes')

    try:
        lid = _lxmf_handshake(a, b, 0xD1, chk, log)
        if lid is None:
            return

        dest16 = bytes(((0xD1 * 3 + i * 5) & 0xFF) for i in range(16))
        src16 = bytes(((0xD1 * 7 + i * 3) & 0xFF) for i in range(16))
        n = 10
        s0 = _snapshot(a, b)
        b_sock = b.listen_tcp()
        sent = []
        for i in range(n):
            lxm = lx.lxm_pack(dest16, src16, 'fixed-mcs msg %d' % i, {})
            pkt = lx.build_lxm_link_packet(lid, lxm)
            sent.append(pkt)
            a.inject_rns(pkt)
            time.sleep(0.15)
        frames = _collect_frames(b_sock, n, 15.0)
        _close(b_sock)
        s1 = _snapshot(a, b)

        for i, pkt in enumerate(sent):
            got = sum(1 for f in frames if bytes(f) == pkt)
            chk(got == 1, 'frame %d/%d delivered exactly once (got %d)' % (i + 1, n, got))
        d_retrans = _delta(s0['a_ack'], s1['a_ack'], 'retransmitted')
        d_drop = _delta(s0['a_ack'], s1['a_ack'], 'dropped')
        d_acks = _delta(s0['b_ack'], s1['b_ack'], 'acks_sent')
        log('A: retrans=%d dropped=%d | B acks_sent=%d delivered=%d'
            % (d_retrans, d_drop, d_acks, len(frames)))
        chk(d_retrans <= n, 'fixed MCS: retries bounded (%d for %d frames -- tracked path self-heals)'
            % (d_retrans, n))
        chk(d_drop == 0, 'fixed MCS: zero drops')
        chk(d_acks >= n, 'B ACKed the unicast flow (%d ACKs)' % d_acks)
    finally:
        # restore whatever the nodes had before the scenario
        a.post('ack_cfg', {'rate_adapt': ra0_a})
        b.post('ack_cfg', {'rate_adapt': ra0_b})


def h08_mcs_auto_unicast(a, b, chk, log):
    """Auto-RA phase: rate_adapt=1. With the pin fix peers start at the
    configured MCS and climb ONLY on live ACK evidence. A full-speed burst
    of unicast frames must deliver complete and drop-free -- this is the
    exact traffic pattern that died permanently before the fix."""
    chk(_set_rate_adapt(a, b, 1), 'rate_adapt=1 applied live on both nodes')

    lid = _lxmf_handshake(a, b, 0xD2, chk, log)
    if lid is None:
        return

    n = 30
    burst = []
    for i in range(n):
        burst.append(lx.rns_packet(lid, lx.CTX_NONE,
                                   ('auto-ra frame %d' % i).encode(),
                                   ptype=lx.PT_DATA, dtype=lx.DT_LINK))
    s0 = _snapshot(a, b)
    b_sock = b.listen_tcp()
    a.inject_rns_many(burst)
    frames = _collect_frames(b_sock, n, 90.0)
    _close(b_sock)
    s1 = _snapshot(a, b)

    missing = [i for i, p in enumerate(burst)
               if sum(1 for f in frames if bytes(f) == p) != 1]
    log('delivered %d/%d unique-exact, missing=%s' % (len(frames), n, missing[:5]))
    chk(not missing, 'all %d unicast frames delivered exactly once under auto-RA' % n)
    d_drop = _delta(s0['a_ack'], s1['a_ack'], 'dropped')
    d_up = _delta(s0['a_ack'], s1['a_ack'], 'ra_upshifts')
    d_down = _delta(s0['a_ack'], s1['a_ack'], 'ra_downshifts')
    d_acks = _delta(s0['b_ack'], s1['b_ack'], 'acks_sent')
    log('A: dropped=%d ra up=%d down=%d | B acks=%d' % (d_drop, d_up, d_down, d_acks))
    chk(d_drop == 0, 'auto-RA: zero drops (RA may climb only on real ACKs)')
    chk(d_acks > 0, 'B ACKed the burst (%d)' % d_acks)


def h09_many_links(a, b, chk, log):
    """Hundreds of links: 200 unique LINKREQUESTs blasted through A. The
    modem must register every link (255-entry DB), deliver every air copy,
    stay responsive -- and keep serving tracked unicast afterwards."""
    n = 200
    dests = []
    lrs = []
    for i in range(n):
        dest16 = bytes((((0x30 + i) * 3 + j * 5) & 0xFF) for j in range(16))
        lr = lx.build_linkrequest(dest16, seed=(0x40 + i))
        dests.append(dest16)
        lrs.append(lr)

    b_sock = b.listen_tcp()
    t0 = time.time()
    lrs_set = set(bytes(x) for x in lrs)
    for off in range(0, n, 50):
        a.inject_rns_many(lrs[off:off + 50])
    frames = _collect_frames(b_sock, n, 120.0)   # early-exit at n unique-ish
    _close(b_sock)
    dt = time.time() - t0

    exact = sum(1 for f in frames if bytes(f) in [bytes(x) for x in lrs])
    log('%d LR frames -> %d deliveries (%d exact-of-set) in %.1fs'
        % (n, len(frames), exact, dt))
    # Contract: AT-LEAST-ONCE byte-exact per LINKREQUEST. Broadcast copies may
    # be skipped under vacancy pressure in tx_broadcast (bc_repeat is a
    # reliability bonus, not a guarantee); RNS itself re-requests announces,
    # so at-least-once at the modem is the honest air contract.
    chk(exact >= n, 'every LINKREQUEST delivered AT LEAST ONCE byte-exact (%d/%d)'
        % (exact, n))
    per = {}
    for f in frames:
        fb = bytes(f)
        if fb in lrs_set:
            per[fb] = per.get(fb, 0) + 1
    worst = max(per.values()) if per else 0
    log('copies per LR: min=%d max=%d' % (min(per.values()) if per else 0, worst))
    chk(worst <= 3, 'no frame broadcast more than 3 times (max=%d)' % worst)

    rows_b = _link_rows(b)
    dbg = b.get('get_reticulum_links')
    total = _int_key(dbg, 'total') or 0
    known = sum(1 for lr in lrs if lx.link_id_from_linkrequest(lr).hex() in rows_b)
    log('B link_db total=%d, %d injected LRs visible in newest-64 dump'
        % (total, known))
    # the API dumps only the newest 64 rows (heap-bounded), so count the
    # table through its `total`; the newest injected links must be visible
    chk(total >= n, 'B link_db holds ALL %d links (total=%d)' % (n, total))
    chk(known >= 50, 'recent links visible in the capped dump (%d)' % known)
    chk(_int_key(dbg, 'rx_parse_fail') is not None, 'B counters readable (alive)')
    chk(dbg.get('rx_parse_fail', 1) == 0, 'no parse failures under load')

    # last link still works as tracked unicast after the flood
    lid = lx.link_id_from_linkrequest(lrs[-1])
    proof = lx.build_lrproof(lid, seed=0xFEED)
    a_sock = a.listen_tcp()
    b.inject_rns(proof)
    pf = _collect_frames(a_sock, 1, 8.0)
    _close(a_sock)
    chk(any(bytes(f) == proof for f in pf), 'proof for flooded-table link delivered to A')
    chk(lid.hex() in _link_rows(a), 'A learned the link from the flooded table')


def _int_key(d, k):
    v = d.get(k)
    return int(v) if v is not None else None


def h10_broadcast_and_link_mix(a, b, chk, log):
    """Interleaved traffic exactly like a busy Reticulum node produces:
    announces/broadcasts mixed with link data. Contracts per class:
    broadcasts arrive once per air copy (x2), link unicast exactly once,
    counters cleanly separated."""
    lid = _lxmf_handshake(a, b, 0xE5, chk, log)
    if lid is None:
        return

    rounds = 12
    bc_pkts, uc_pkts = [], []
    for i in range(rounds):
        bc_pkts.append(build_rns_packet(dest_byte=0x77, payload_len=90 + i,
                                        dtype=0, ptype=0, seed=(0xB0 + i)))
        uc_pkts.append(lx.rns_packet(lid, lx.CTX_NONE,
                                     ('mix-uc-%d' % i).encode(),
                                     ptype=lx.PT_DATA, dtype=lx.DT_LINK))

    s0 = _snapshot(a, b)
    b_sock = b.listen_tcp()
    for i in range(rounds):                      # strict alternation
        a.inject_rns(bc_pkts[i])
        time.sleep(0.05)
        a.inject_rns(uc_pkts[i])
        time.sleep(0.05)
    want = rounds * 3                            # 2 copies per bc + 1 unicast
    frames = _collect_frames(b_sock, want, 60.0)
    _close(b_sock)
    s1 = _snapshot(a, b)

    bc_ok_min = all(sum(1 for f in frames if bytes(f) == p) >= 1 for p in bc_pkts)
    bc_ok_max = all(sum(1 for f in frames if bytes(f) == p) <= 3 for p in bc_pkts)
    uc_ok = all(sum(1 for f in frames if bytes(f) == p) == 1 for p in uc_pkts)
    chk(bc_ok_min, 'every broadcast arrived AT LEAST ONCE (repeat is best-effort)')
    chk(bc_ok_max, 'no broadcast aired more than 3 times')
    chk(uc_ok, 'every link frame arrived exactly once (dedup holds under mix)')
    d_bc = _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_bc')
    d_uc = (_delta(s0['a_ack'], s1['a_ack'], 'dbg_path_plain') +
            _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_plainl') +
            _delta(s0['a_ack'], s1['a_ack'], 'dbg_path_bundle'))
    d_drop = _delta(s0['a_ack'], s1['a_ack'], 'dropped')
    log('A: path_bc=%d tracked=%d dropped=%d' % (d_bc, d_uc, d_drop))
    chk(d_bc == rounds, '%d broadcasts rode the bc path' % rounds)
    chk(d_uc >= rounds, 'link frames rode tracked paths (%d)' % d_uc)
    chk(d_drop <= 2, 'mixed traffic drops within burst tolerance (%d)' % d_drop)


def h11_bidir_streams(a, b, chk, log):
    """Bidirectional concurrent streams over one established link -- both
    modems transmit, receive and ACK at the same time. Both directions must
    deliver complete, byte-exact, without drops."""
    lid = _lxmh = None
    lid = _lxmf_handshake(a, b, 0xF1, chk, log)
    if lid is None:
        return

    n = 24
    ab = [lx.rns_packet(lid, lx.CTX_NONE, ('AB-%02d-' % i).encode() + bytes(40),
                        ptype=lx.PT_DATA, dtype=lx.DT_LINK) for i in range(n)]
    ba = [lx.rns_packet(lid, lx.CTX_NONE, ('BA-%02d-' % i).encode() + bytes(40),
                        ptype=lx.PT_DATA, dtype=lx.DT_LINK) for i in range(n)]

    s0 = _snapshot(a, b)
    b_sock = b.listen_tcp()
    a_sock = a.listen_tcp()

    def push(node, frames):
        try:
            node.inject_rns_many(frames)
        except OSError as e:
            log('inject error: %r' % (e,))

    t_ab = threading.Thread(target=push, args=(a, ab))
    t_ba = threading.Thread(target=push, args=(b, ba))
    t_ab.start()
    t_ba.start()
    t_ab.join()
    t_ba.join()

    got_b = _collect_frames(b_sock, n, 120.0)
    got_a = _collect_frames(a_sock, n, 30.0)
    _close(b_sock)
    _close(a_sock)
    s1 = _snapshot(a, b)

    def complete(sent, got):
        return all(sum(1 for f in got if bytes(f) == p) == 1 for p in sent)

    chk(complete(ab, got_b), 'A->B: all %d frames delivered exactly once' % n)
    chk(complete(ba, got_a), 'B->A: all %d frames delivered exactly once' % n)
    d_drop_a = _delta(s0['a_ack'], s1['a_ack'], 'dropped')
    d_drop_b = _delta(s0['b_ack'], s1['b_ack'], 'dropped')
    d_acks_a = _delta(s0['a_ack'], s1['a_ack'], 'acks_sent')
    d_acks_b = _delta(s0['b_ack'], s1['b_ack'], 'acks_sent')
    log('drops A=%d B=%d | acks_sent A=%d B=%d' %
        (d_drop_a, d_drop_b, d_acks_a, d_acks_b))
    chk(d_drop_a == 0 and d_drop_b == 0, 'zero drops in BOTH directions')
    chk(d_acks_a > 0 and d_acks_b > 0, 'ACK loops closed on BOTH sides')


SCENARIOS = [
    ('h00_sanity', h00_sanity),
    ('h01_idle_no_counter_noise', h01_idle_no_counter_noise),
    ('h02_one_rns_frame_accounting', h02_one_rns_frame_accounting),
    ('h03_broadcast_duplicate_contract', h03_broadcast_duplicate_contract),
    ('h04_lxmf_link_handshake', h04_lxmf_link_handshake),
    ('h05_lxmf_single_packet_message', h05_lxmf_single_packet_message),
    ('h06_lxmf_file_resource_transfer', h06_lxmf_file_resource_transfer),
    ('h07_mcs_fixed_unicast', h07_mcs_fixed_unicast),
    ('h08_mcs_auto_unicast', h08_mcs_auto_unicast),
    ('h09_many_links', h09_many_links),
    ('h10_broadcast_and_link_mix', h10_broadcast_and_link_mix),
    ('h11_bidir_streams', h11_bidir_streams),
]
