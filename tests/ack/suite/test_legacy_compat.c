#include "sys_config.h"
#define LOG_LOCAL_LEVEL LOG_LEVEL_HALOW_PKG_HANDLER
#include "basic_include.h"
#include "lib/logc/log.h"
#include "halow.h"
#include "utils.h"
#include "halow_ack.h"
#include "tcp_server.h"
#include "harness.h"
#include "halow_pkg_handler.h"
#include "rns/stream_parser.h"
#include "rns/link_db.h"
#include "rns/link_parser.h"
#include "rns/link_utils.h"

#include <stdio.h>
#include <string.h>
#include "helpers.h"
#include "test_fw.h"

uint16_t rns_real_build( uint8_t *out, uint8_t header, uint8_t cflag,
                                uint8_t ttype, uint8_t dtype, uint8_t ptype,
                                uint8_t hops, uint8_t context, uint8_t seed,
                                uint16_t plen ){
    uint16_t o = 0;
    out[o++] = (uint8_t)((header << 6) | (cflag << 5) | (ttype << 4) |
                         (dtype << 2) | ptype);
    out[o++] = hops;
    if( header == 1u ){ memset(&out[o], 0x42, 16); o += 16; }        /* transport id */
    memset(&out[o], 0x55, 16); o += 16;                              /* one shared dest */
    out[o++] = context;
    fill_payload(&out[o], plen, seed);
    o += plen;
    return o;
}

/* scenario 1: NO false positive over the full real flags space, and every
 * parseable real frame reaches TCP byte-exact */
void t_legacy_detector_rns_space( void ){
    static uint8_t pkt[600];
    static uint8_t wire[1300];
    halow_ack_stats_t st;
    uint32_t pf0, calls0, unk0, arx0;
    const uint8_t peer[6] = {0x33,0x33,0x33,0x33,0x33,0x33};
    const uint8_t me[6]   = {0x00,0x00,0x00,0x00,0x00,0x01};
    fp_node_start(NULL);

    /* 1a. is_internal_frame over all 128 valid flag bytes x all 256
     * second bytes (hops and adversarial): must NEVER fire (bit7 = 0) */
    for( uint32_t f = 0; f < 128; f++ ){
        pkt[0] = (uint8_t)f;
        for( uint32_t s = 0; s < 256; s++ ){
            pkt[1] = (uint8_t)s;
            pkt[2] = 0x5A; pkt[3] = 0xAD; pkt[4] = 0xF6;   /* magic-ish body */
            CHECK( !halow_ack_is_internal_frame(pkt, 40) );
        }
    }

    /* 1b. all 128 flag combos x hops 0..15 through the FULL pipe:
     * exactly one TCP capture each, byte-exact SLIP, no swallow counters */
    halow_ack_stats_get(&st);
    unk0  = st.rx_env_unk;
    arx0  = st.acks_rx_frames;
    pf0   = g_dbg_rns_rx_parse_fail;
    calls0 = g_dbg_rns_rx_calls;
    {
        int delivered = 0;
        for( uint32_t f = 0; f < 128; f++ ){
            uint8_t header = (uint8_t)(f >> 6);
            uint8_t cflag  = (uint8_t)((f >> 5) & 1u);
            uint8_t ttype  = (uint8_t)((f >> 4) & 1u);
            uint8_t dtype  = (uint8_t)((f >> 2) & 3u);
            uint8_t ptype  = (uint8_t)(f & 3u);
            for( uint32_t hops = 0; hops < 16; hops++ ){
                uint16_t plen = (uint16_t)(20 + (f & 7));
                /* payload seed depends on ptype ONLY: LINKREQUEST link ids
                 * are sha(body) and must repeat, else the 255-link table
                 * (persistent across scenarios) fills up; frame-level fnv
                 * dedup still sees unique frames via flags/hops bytes */
                uint16_t len = rns_real_build(pkt, header, cflag, ttype,
                                              dtype, ptype, (uint8_t)hops,
                                              (uint8_t)(hops ? RNS_CONTEXT_PATH_RESPONSE
                                                             : RNS_CONTEXT_NONE),
                                              (uint8_t)(0x60 + ptype), plen);
                test_tcp_reset();
                halow_pkg_handler_rf_to_tcp(pkt, len, peer, me, -10);
                CHECK( test_tcp_count() == 1 );
                uint16_t wl = (uint16_t)rns_stream_encode_frame(wire, sizeof(wire), pkt, len);
                CHECK( test_tcp_at(0)->len == wl );
                CHECK( memcmp(test_tcp_at(0)->buf, wire, wl) == 0 );
                delivered++;
            }
        }
        CHECK( delivered == 128 * 16 );
    }
    halow_ack_stats_get(&st);
    CHECK( st.rx_env_unk == unk0 );
    CHECK( st.acks_rx_frames == arx0 );
    CHECK( g_dbg_rns_rx_parse_fail == pf0 );
    CHECK( g_dbg_rns_rx_calls == calls0 + (uint32_t)(128u * 16u) );

    /* 1c. first byte with bit7 set that is NOT one of our magics: the
     * parser masks header type to ONE bit and never checks bit7, so such
     * frames parse (GIGO) and are FORWARDED to TCP -- real Reticulum never
     * sets bit7, this is lenient transparency, and it proves the detectors
     * are the ONLY swallow path */
    {
        static const uint8_t bad_first[] = {0x80, 0xA4, 0xA6, 0xC3, 0xFF, 0xBF};
        pf0 = g_dbg_rns_rx_parse_fail;
        for( uint32_t i = 0; i < sizeof(bad_first); i++ ){
            uint16_t len = rns_real_build(pkt, 0, 0, 0, 0, 0, 3,
                                          RNS_CONTEXT_NONE, (uint8_t)(0xBB + i), 30);
            pkt[0] = bad_first[i];
            test_tcp_reset();
            halow_pkg_handler_rf_to_tcp(pkt, len, peer, me, -10);
            CHECK( test_tcp_count() == 1 );
        }
        CHECK( g_dbg_rns_rx_parse_fail == pf0 );
    }
}

/* scenario 2: every INTERNAL frame variant is recognized and consumed
 * (never leaks to TCP), malformed variants behave documented-way */
void t_legacy_detector_internal( void ){
    static uint8_t f[200];
    halow_ack_stats_t st;
    const uint8_t peer[6] = {0x33,0x33,0x33,0x33,0x33,0x33};
    const uint8_t me[6]   = {0x00,0x00,0x00,0x00,0x00,0x01};

    fp_node_start(NULL);

    /* 2a. fid ACK of every legal length 5..35 (n = 1..16), evm edges */
    halow_ack_stats_get(&st);
    uint32_t arx0 = st.acks_rx_frames;
    for( uint32_t n = 1; n <= HALOW_ACK_ACK_FIDS_MAX; n++ ){
        for( uint32_t e = 0; e < 3; e++ ){
            f[0] = 0xA5; f[1] = 0x5A;
            f[2] = (e == 0) ? 0x80u : (e == 1 ? 0xF6u : 0xFFu);
            for( uint32_t k = 0; k < n; k++ ){
                f[3 + 2*k] = (uint8_t)(0x11 + k);
                f[4 + 2*k] = (uint8_t)(0x22 + k);
            }
            CHECK( halow_ack_is_internal_frame(f, (uint16_t)(3 + 2*n)) );
            test_tcp_reset();
            halow_pkg_handler_rf_to_tcp(f, (uint16_t)(3 + 2*n), peer, me, -10);
            CHECK( test_tcp_count() == 0 );          /* consumed, not delivered */
        }
    }
    halow_ack_stats_get(&st);
    CHECK( st.acks_rx_frames == arx0 + 3u * HALOW_ACK_ACK_FIDS_MAX );

    /* 2b. length edges of the ack detector */
    f[0] = 0xA5; f[1] = 0x5A; f[2] = 0x80;
    CHECK( !halow_ack_is_internal_frame(f, 4) );      /* < LEN_MIN */
    CHECK( !halow_ack_is_internal_frame(f, 2) );      /* below any magic */
    CHECK( !halow_ack_is_internal_frame(f, 0) );
    CHECK( halow_ack_is_internal_frame(f, 5) );
    CHECK( halow_ack_is_internal_frame(f, HALOW_ACK_ACK_LEN_MAX) );
    CHECK( !halow_ack_is_internal_frame(f, (uint16_t)(HALOW_ACK_ACK_LEN_MAX + 2)) ); /* > max */
    CHECK( !halow_ack_is_internal_frame(f, 6) );      /* even len: (6-3)&1 != 0 */

    /* 2c. even-length "ack" (malformed): ack parity fails AND byte2 >= 0x80
     * fails the env check -> data path -> FORWARDED (GIGO transparency) */
    halow_ack_stats_get(&st);
    uint32_t unk0 = st.rx_env_unk;
    f[0] = 0xA5; f[1] = 0x5A; f[2] = 0x80; f[3] = 1; f[4] = 2; f[5] = 3;
    test_tcp_reset();
    halow_pkg_handler_rf_to_tcp(f, 6, peer, me, -10);
    CHECK( test_tcp_count() == 0 );   /* 6 B < RNS header: parse-fail drop */
    halow_ack_stats_get(&st);
    CHECK( st.rx_env_unk == unk0 );

    /* 2d. documented limitation: evm in 1..127 (positive, physically
     * impossible) routes the ack to the env parser -> swallowed as unknown,
     * the ack is lost. Old firmware has the same behavior (same sender
     * code), so this cannot break the migration. */
    f[0] = 0xA5; f[1] = 0x5A; f[2] = 0x05; f[3] = 1; f[4] = 2;
    CHECK( !halow_ack_is_internal_frame(f, 5) );

    /* 2e. env ACK (14 B exact): recognized internal, consumed */
    halow_ack_stats_get(&st);
    arx0 = st.acks_rx_frames;
    f[0] = 0xA5; f[1] = 0x5A; f[2] = (uint8_t)((HALOW_ENV_VER << 4) | HALOW_ENV_TYPE_ACK);
    for( int i = 3; i < 14; i++ ) f[i] = 0;
    CHECK( halow_ack_is_internal_frame(f, HALOW_ENV_ACK_LEN) );
    test_tcp_reset();
    halow_pkg_handler_rf_to_tcp(f, HALOW_ENV_ACK_LEN, peer, me, -10);
    CHECK( test_tcp_count() == 0 );
    halow_ack_stats_get(&st);
    CHECK( st.acks_rx_frames == arx0 + 1 );  /* env-ack counts as an ack frame */
    CHECK( st.env_rx_acks == 1 );            /* counted before the peer lookup */

    /* 2f. env BUNDLE is NOT "internal" (it carries data): subs delivered
     * byte-exact (subs must be real RNS packets >= 19 B; shorter subs
     * parse-fail inside deliver_rns_frame and are not forwarded) */
    {
        static uint8_t sub[2][64];
        static uint8_t wire[140];
        uint16_t sl[2];
        uint16_t o = 0;
        sl[0] = rns_real_build(sub[0], 0, 0, 0, RNS_DESTINATION_TYPE_LINK,
                               RNS_PACKET_TYPE_DATA, 0, RNS_CONTEXT_NONE, 0x51, 30);
        sl[1] = rns_real_build(sub[1], 0, 0, 0, RNS_DESTINATION_TYPE_LINK,
                               RNS_PACKET_TYPE_DATA, 0, RNS_CONTEXT_KEEPALIVE, 0x52, 25);
        f[o++] = 0xA5; f[o++] = 0x5A;
        f[o++] = (uint8_t)((HALOW_ENV_VER << 4) | HALOW_ENV_TYPE_BUNDLE);
        f[o++] = 0x34; f[o++] = 0x12;        /* seq */
        f[o++] = 2;                          /* nsub */
        for( int k = 0; k < 2; k++ ){
            f[o++] = (uint8_t)(sl[k] & 0xFF);
            f[o++] = (uint8_t)(sl[k] >> 8);
            memcpy(&f[o], sub[k], sl[k]);
            o += sl[k];
        }
        CHECK( !halow_ack_is_internal_frame(f, o) );
        test_tcp_reset();
        halow_pkg_handler_rf_to_tcp(f, o, peer, me, -10);
        CHECK( test_tcp_count() == 2 );
        for( int k = 0; k < 2; k++ ){
            uint16_t wl = (uint16_t)rns_stream_encode_frame(wire, sizeof(wire),
                                                            sub[k], sl[k]);
            CHECK( test_tcp_at(k)->len == wl );
            if( memcmp(test_tcp_at(k)->buf, wire, wl) != 0 ){
                for( uint32_t q = 0; q < wl; q++ ){
                    if( test_tcp_at(k)->buf[q] != wire[q] ){
                        printf("    dbg envsub k=%d diff @%u cap=%02x wire=%02x len=%u wl=%u\n",
                               k, q, test_tcp_at(k)->buf[q], wire[q],
                               test_tcp_at(k)->len, wl);
                        break;
                    }
                }
                CHECK( 0 );
            }
        }
    }

    /* 2g. short A5 5A frames: len>=3 swallowed as env-unknown, len 2 falls
     * through to the RNS parser (garbage-in-garbage-out, documented) */
    halow_ack_stats_get(&st);
    unk0 = st.rx_env_unk;
    f[0] = 0xA5; f[1] = 0x5A; f[2] = 0x10;
    test_tcp_reset();
    halow_pkg_handler_rf_to_tcp(f, 3, peer, me, -10);
    CHECK( test_tcp_count() == 0 );
    test_tcp_reset();
    halow_pkg_handler_rf_to_tcp(f, 2, peer, me, -10);
    CHECK( test_tcp_count() == 0 );          /* len 2 < RNS header size: dropped */
    halow_ack_stats_get(&st);
    CHECK( st.rx_env_unk == unk0 + 1 );
}

/* Regression: an envelope peer fed plain frames (agg off / oversize) used to
 * starve -- plain frames carry seq 0xFFFF and never match the env bitmap
 * ACK. Env peers must always ride seq'd bundles. */
