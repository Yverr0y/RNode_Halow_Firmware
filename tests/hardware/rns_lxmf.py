"""Reticulum/LXMF wire-format builders for the hardware (Large-tier) tests.

Replicates the byte-level traffic that real LXMF clients (Sideband, MeshChat,
Columba -- all build on the reference markqvist/LXMF stack) put on an RNS TCP
interface, so the modem RF path is exercised exactly like in production.

Verified against upstream sources (2026-08-21):
  LXMF/LXMessage.py -- packed LXM = dest16 | source16 | sig64 |
                             msgpack([ts_float, title, content, fields_map])
  LXMF/LXMF.py      -- FIELD_FILE_ATTACHMENTS=0x05, FIELD_IMAGE=0x06,
                       FIELD_AUDIO=0x07
  RNS/Packet.py     -- flags layout: [ht:1|ctx_flag:1|tt:1|dtype:2|ptype:2],
                       wire = flags | hops | dest16 | context | payload
  RNS/Link.py       -- LINKREQUEST payload = pub32 | sigpub32 | signalling3
                       (21-bit MTU | 3-bit mode); link_id =
                       truncated_hash(flags&0x0F + raw[2 : 2+16+1+64]);
                       LRPROOF = PROOF packet on the link, payload
                       sig64 | pub32 | signalling3
  RNS/Resource.py   -- large LXMF delivery = stream of RESOURCE-context
                       DATA packets, each <= link MDU (431 B at MTU 500)
  meshchat.py       -- fields[FIELD_IMAGE]=[image_type_str, image_bytes],
                       fields[FIELD_FILE_ATTACHMENTS]=[[name, bytes], ...];
                       no application-level chunking (the router switches to
                       a RNS resource above 319 B of content)

Deliberate approximations (opaque to the modem, documented for honesty):
  - signatures/public keys are deterministic pseudo-random bytes (the modem
    never validates crypto; only sizes and header fields matter on this path)
  - RESOURCE_ADV / LRRTT inner payloads are structural placeholders; packet
    sizes, contexts, header layout, direction and per-transfer packet counts
    match production traffic
"""
import hashlib
import struct
import time

# ---- RNS header constants (RNS/Packet.py) ----
PT_DATA, PT_ANNOUNCE, PT_LINKREQUEST, PT_PROOF = 0, 1, 2, 3
DT_SINGLE, DT_GROUP, DT_PLAIN, DT_LINK = 0, 1, 2, 3

CTX_NONE = 0x00
CTX_RESOURCE = 0x01
CTX_RESOURCE_ADV = 0x02
CTX_RESOURCE_REQ = 0x03
CTX_RESOURCE_HMU = 0x04
CTX_RESOURCE_PRF = 0x05
CTX_KEEPALIVE = 0xFA
CTX_LINKCLOSE = 0xFC
CTX_LRPROOF = 0xFF

RNS_MTU = 500          # modem-side link MTU ceiling (RNS_LINK_MTU_FIXED)
LINK_MDU = 431         # RNS.Link.MDU at MTU 500 (LXMF uses this as seg size)
LXMF_OVERHEAD = 112    # 16 dest + 16 src + 64 sig + 8 ts + 8 msgpack
LXMF_LINK_MAX_CONTENT = LINK_MDU - LXMF_OVERHEAD   # 319 B: single-packet limit

# ---- LXMF field tags (LXMF/LXMF.py) ----
FIELD_FILE_ATTACHMENTS = 0x05
FIELD_IMAGE = 0x06
FIELD_AUDIO = 0x07


# ---- minimal msgpack packer (u-msgpack compatible for this value domain) ----
def mp(v):
    """Pack ints/floats/str/bytes/lists/dicts like umsgpack.packb does."""
    if v is True:
        return b'\xc3'
    if v is False:
        return b'\xc2'
    if v is None:
        return b'\xc0'
    if isinstance(v, int):
        if 0 <= v < 128:
            return bytes([v])
        if 0 <= v < 256:
            return b'\xcc' + bytes([v])
        if 0 <= v < 65536:
            return b'\xcd' + struct.pack('>H', v)
        return b'\xce' + struct.pack('>I', v)
    if isinstance(v, float):
        return b'\xcb' + struct.pack('>d', v)
    if isinstance(v, str):
        b = v.encode('utf-8')
        n = len(b)
        if n < 32:
            return bytes([0xA0 | n]) + b
        if n < 256:
            return b'\xd9' + bytes([n]) + b
        return b'\xda' + struct.pack('>H', n) + b
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        n = len(b)
        if n < 256:
            return b'\xc4' + bytes([n]) + b
        if n < 65536:
            return b'\xc5' + struct.pack('>H', n) + b
        raise ValueError('bin too large for this packer: %d' % n)
    if isinstance(v, (list, tuple)):
        n = len(v)
        head = bytes([0x90 | n]) if n < 16 else b'\xdc' + struct.pack('>H', n)
        return head + b''.join(mp(x) for x in v)
    if isinstance(v, dict):
        n = len(v)
        head = bytes([0x80 | n]) if n < 16 else b'\xde' + struct.pack('>H', n)
        return head + b''.join(mp(k) + mp(x) for k, x in v.items())
    raise TypeError('cannot msgpack %r' % (type(v),))


# ---- RNS packet assembly (RNS/Packet.py wire layout) ----
def rns_packet(dest16, context=CTX_NONE, payload=b'', ptype=PT_DATA,
               dtype=DT_SINGLE, hops=0, header_type=0):
    b0 = ((header_type & 1) << 6) | ((dtype & 3) << 2) | (ptype & 3)
    pkt = bytes([b0, hops]) + bytes(dest16[:16]) + bytes([context]) + bytes(payload)
    assert len(pkt) <= RNS_MTU, 'RNS packet %d B exceeds MTU %d' % (len(pkt), RNS_MTU)
    return pkt


def _signalling(mtu, mode):
    # Link.signalling_bytes: 21-bit MTU | 3-bit mode, big endian, 3 bytes
    return struct.pack('>I', ((mtu & 0x1FFFFF) | ((mode & 7) << 21)))[1:]


def _pseudo(n, seed):
    return bytes(((seed + i * 13) & 0xFF) for i in range(n))


# ---- link establishment (RNS/Link.py) ----
def build_linkrequest(dest_id16, mtu=RNS_MTU, mode=0, seed=0x51):
    """LINKREQUEST: payload = pub32 | sigpub32 | signalling3 (67 B core)."""
    payload = _pseudo(32, seed) + _pseudo(32, seed + 0x41) + _signalling(mtu, mode)
    return rns_packet(dest_id16, CTX_NONE, payload,
                      ptype=PT_LINKREQUEST, dtype=DT_SINGLE)


def link_id_from_linkrequest(lr):
    """Mirror of the modem derivation (rns_link_parser_calc_link_id):
    sha256(flags & 0x0F || raw[2 : 2+16+1+64]) truncated to 16 B."""
    core_end = 2 + 16 + 1 + 64
    assert len(lr) >= core_end, 'linkrequest too short'
    return hashlib.sha256(bytes([lr[0] & 0x0F]) + lr[2:core_end]).digest()[:16]


def build_lrproof(link_id16, mtu=RNS_MTU, mode=0, seed=0x62):
    """LRPROOF: PROOF packet addressed to the link id (dtype=LINK, ctx 0xFF),
    payload = sig64 | pub32 | signalling3."""
    payload = _pseudo(64, seed) + _pseudo(32, seed + 0x55) + _signalling(mtu, mode)
    return rns_packet(link_id16, CTX_LRPROOF, payload,
                      ptype=PT_PROOF, dtype=DT_LINK)


def build_keepalive(link_id16):
    return rns_packet(link_id16, CTX_KEEPALIVE, b'', ptype=PT_DATA, dtype=DT_LINK)


def build_linkclose(link_id16):
    return rns_packet(link_id16, CTX_LINKCLOSE, b'', ptype=PT_DATA, dtype=DT_LINK)


# ---- LXMF messages (LXMF/LXMessage.py + meshchat field usage) ----
def lxm_pack(dest16, src16, content, fields, title='', timestamp=None, seed=0x77):
    """packed = dest16 | source16 | sig64 | msgpack([ts, title, content, fields])."""
    payload = [time.time() if timestamp is None else timestamp, title, content, fields]
    return (bytes(dest16[:16]) + bytes(src16[:16]) + _pseudo(64, seed) + mp(payload))


def lxm_content_size(lxm):
    """LXMF-canonical content size = packed_payload - ts - msgpack struct."""
    return len(lxm) - 2 * 16 - 64 - 8


def lxm_image(dest16, src16, image_bytes, image_type='image/png', text='photo'):
    """MeshChat/Sideband image message: fields[0x06] = [type_str, bytes]."""
    return lxm_pack(dest16, src16, text, {FIELD_IMAGE: [image_type, image_bytes]})


def lxm_files(dest16, src16, files, text='files'):
    """MeshChat/Sideband attachments: fields[0x05] = [[name, bytes], ...]."""
    return lxm_pack(dest16, src16, text,
                    {FIELD_FILE_ATTACHMENTS: [[name, data] for name, data in files]})


def build_lxm_link_packet(link_id16, lxm):
    """Direct delivery of a single-packet LXM over an established link:
    DATA packet, dtype=LINK, dest=link_id, ctx=NONE."""
    return rns_packet(link_id16, CTX_NONE, lxm, ptype=PT_DATA, dtype=DT_LINK)


# ---- RNS resource transfer (LXMF large-message representation) ----
def build_resource_adv(link_id16, resource_hash16, size_bytes):
    """RESOURCE_ADV: small DATA packet on the link advertising the transfer."""
    adv = {0: bytes(resource_hash16[:16]), 1: size_bytes, 2: LINK_MDU}
    return rns_packet(link_id16, CTX_RESOURCE_ADV, mp(adv),
                      ptype=PT_DATA, dtype=DT_LINK)


def build_resource_segment(link_id16, seg_index, chunk):
    """One RESOURCE segment: payload = idx32 | chunk, capped at link MDU.
    The receiver reassembles by index, so delivery order does not matter."""
    payload = struct.pack('>I', seg_index) + bytes(chunk)
    assert len(payload) <= LINK_MDU, 'segment payload %d > MDU' % len(payload)
    return rns_packet(link_id16, CTX_RESOURCE, payload, ptype=PT_DATA, dtype=DT_LINK)


def build_resource_proof(link_id16, resource_hash16):
    """RESOURCE_PRF: PROOF packet on the link closing the transfer."""
    return rns_packet(link_id16, CTX_RESOURCE_PRF, bytes(resource_hash16[:16]) + _pseudo(48, 0x33),
                      ptype=PT_PROOF, dtype=DT_LINK)


def split_file(data, seg_payload=LINK_MDU - 4):
    """Split a file into resource segments (idx header 4 B + chunk)."""
    segs = []
    off = 0
    idx = 0
    while off < len(data):
        chunk = data[off:off + seg_payload]
        segs.append((idx, chunk))
        off += seg_payload
        idx += 1
    return segs


def reassemble_segments(frames):
    """Extract {idx: chunk} from delivered RESOURCE frames (or None on junk)."""
    out = {}
    for f in frames:
        f = bytes(f)
        if len(f) < 2 + 16 + 1 + 4:
            continue
        b0, hops = f[0], f[1]
        if (b0 & 3) != PT_DATA or (((b0 >> 2) & 3) != DT_LINK):
            continue
        context = f[2 + 16]
        if context != CTX_RESOURCE:
            continue
        payload = f[2 + 16 + 1:]
        if len(payload) < 4:
            continue
        idx = struct.unpack('>I', payload[:4])[0]
        out[idx] = payload[4:]
    return out


# ---- self-test: python rns_lxmf.py ----
def _selftest():
    # msgpack vectors (u-msgpack compatible encodings)
    assert mp(0) == b'\x00' and mp(5) == b'\x05'
    assert mp(127) == b'\x7f' and mp(200) == b'\xcc\xc8'
    assert mp(70000) == b'\xce\x00\x01\x11\x70'
    assert mp(3.5) == b'\xcb' + struct.pack('>d', 3.5)
    assert mp('') == b'\xa0' and mp('abc') == b'\xa3abc'
    assert mp('x' * 40) == b'\xd9\x28' + b'x' * 40
    assert mp(b'') == b'\xc4\x00' and mp(b'ab') == b'\xc4\x02ab'
    assert mp(b'z' * 300) == b'\xc5\x01\x2c' + b'z' * 300
    assert mp([1, 'a']) == b'\x92\x01\xa1a'
    assert mp({5: [['f.bin', b'xy']]}) == b'\x81\x05\x91\x92\xa5f.bin\xc4\x02xy'
    # LXMF packed-message structure
    lxm = lxm_pack(bytes(16), bytes(16), 'hi', {FIELD_IMAGE: ['image/png', b'ab']},
                   timestamp=1700000000.5)
    assert lxm[:32] == bytes(32)                      # dest + source
    assert len(lxm[32:96]) == 64                      # signature slot
    body = mp([1700000000.5, '', 'hi', {FIELD_IMAGE: ['image/png', b'ab']}])
    assert lxm[96:] == body
    # linkrequest / link_id mirror of the modem derivation
    lr = build_linkrequest(bytes([0x9A] * 16), seed=0x51)
    assert len(lr) == 2 + 16 + 1 + 67 and lr[0] == ((DT_SINGLE << 2) | PT_LINKREQUEST)
    assert lr[-3:] == _signalling(RNS_MTU, 0)
    lid = link_id_from_linkrequest(lr)
    import hashlib as _h
    expect = _h.sha256(bytes([lr[0] & 0x0F]) + lr[2:2 + 16 + 1 + 64]).digest()[:16]
    assert lid == expect
    # proof / resource shapes
    proof = build_lrproof(lid)
    assert proof[0] == ((DT_LINK << 2) | PT_PROOF) and proof[18] == CTX_LRPROOF
    assert len(proof) == 2 + 16 + 1 + 64 + 32 + 3
    seg = build_resource_segment(lid, 7, b'q' * 427)
    assert len(seg) == 2 + 16 + 1 + LINK_MDU and seg[18] == CTX_RESOURCE
    assert reassemble_segments([seg])[7] == b'q' * 427
    # single-packet LXM budget (h05 sizing)
    img120 = bytes(120)
    lxm2 = lxm_image(bytes(16), bytes(16), img120)
    assert lxm_content_size(lxm2) <= LXMF_LINK_MAX_CONTENT
    assert len(build_lxm_link_packet(lid, lxm2)) <= RNS_MTU
    print('rns_lxmf selftest: OK')


if __name__ == '__main__':
    _selftest()
