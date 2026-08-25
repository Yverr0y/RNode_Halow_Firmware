/* Statistics module + air-vs-app accounting contracts.
 *
 * Pins the dashboard semantics that were ambiguous on the device:
 *  - TX radio stats count every AIR copy (broadcast originals AND repeats),
 *    but NOT unicast retransmissions (those go through buf_tx_send only)
 *  - RX radio stats count every AIR frame at the callback level, BEFORE the
 *    ACK-layer dedup collapses repeats, and internal (ACK) frames are
 *    excluded by the caller (main.c contract)
 *  - speed = IIR over per-second byte deltas, decays in silence
 *  - unknown-link frames go out broadcast; once the peer MAC is learned,
 *    the same destination goes unicast
 */
#include "sys_config.h"
#define LOG_LOCAL_LEVEL LOG_LEVEL_HALOW_PKG_HANDLER
#include "basic_include.h"
#include "lib/logc/log.h"
#include "halow.h"
#include "utils.h"
#include "halow_ack.h"
#include "halow_pkg_handler.h"
#include "statistics.h"
#include "rns/link_db.h"
#include "rns/link_parser.h"
#include "rns/stream_parser.h"
#include "tcp_server.h"
#include "harness.h"
#include "helpers.h"
#include "test_fw.h"

#include <stdio.h>
#include <string.h>

static uint16_t stats_build_rns( uint8_t *out, uint8_t seed ){
    return rns_pkt_build(out, seed, seed + 1u, 100u, 0u /*DATA*/, 0u /*SINGLE*/);
}

static uint32_t bc_count( void ){
    halow_ack_stats_t st;
    halow_ack_stats_get(&st);
    return st.bc_repeats;
}

void t_stats_counters_exact( void ){
    statistics_radio_t st;
    statistics_radio_reset();
    statistics_radio_register_rx_package(100u);
    statistics_radio_register_rx_package(200u);
    statistics_radio_register_tx_package(50u);
    statistics_radio_register_tx_package(150u);
    st = statistics_radio_get();
    CHECK( st.rx_packets == 2 );
    CHECK( st.tx_packets == 2 );
    CHECK( st.rx_bytes == 300u );
    CHECK( st.tx_bytes == 200u );
    statistics_radio_reset();
    st = statistics_radio_get();
    CHECK( st.rx_packets == 0 && st.tx_packets == 0 );
    CHECK( st.rx_bytes == 0 && st.tx_bytes == 0 );
    CHECK( st.rx_bitps == 0 && st.tx_bitps == 0 );
}

void t_stats_rate_iir_decay( void ){
    statistics_radio_t st;
    statistics_radio_reset();
    /* 1000 B in the first window -> bitps = (0*3 + 1000*8)/4 = 2000 */
    statistics_radio_register_tx_package(1000u);
    statistics_radio_rate_tick();
    st = statistics_radio_get();
    CHECK( st.tx_bitps == 2000u );
    /* silence: y = (3y)/4 each second */
    statistics_radio_rate_tick();
    CHECK( statistics_radio_get().tx_bitps == 1500u );
    statistics_radio_rate_tick();
    CHECK( statistics_radio_get().tx_bitps == 1125u );
    /* new traffic: y = (3*1125 + 400*8)/4 = 6575/4 = 1643 */
    statistics_radio_register_rx_package(0u);  /* rx stays quiet */
    statistics_radio_register_tx_package(400u);
    statistics_radio_rate_tick();
    st = statistics_radio_get();
    CHECK( st.tx_bitps == 1643u );
    CHECK( st.rx_bitps == 0u );
}

void t_stats_rate_idle_clamp_zero( void ){
    statistics_radio_reset();
    statistics_radio_register_rx_package(500u);
    statistics_radio_rate_tick();
    CHECK( statistics_radio_get().rx_bitps == 1000u );
    for( int i = 0; i < 9; i++ ) statistics_radio_rate_tick();
    CHECK( statistics_radio_get().rx_bitps != 0 );   /* still decaying */
    statistics_radio_rate_tick();                    /* 10th idle second */
    CHECK( statistics_radio_get().rx_bitps == 0 );
    /* traffic revives the estimate from zero */
    statistics_radio_register_rx_package(125u);
    statistics_radio_rate_tick();
    CHECK( statistics_radio_get().rx_bitps == 250u );
}

void t_stats_rate_wrap_guard( void ){
    /* byte counter going backwards (reset mid-flight) must not spike the
     * rate: delta clamps to 0 */
    statistics_radio_reset();
    statistics_radio_register_tx_package(1000u);
    statistics_radio_rate_tick();
    CHECK( statistics_radio_get().tx_bitps == 2000u );
    g_stat_radio.tx_bytes = 0;   /* reset without baselines re-zeroed */
    statistics_radio_rate_tick();
    CHECK( statistics_radio_get().tx_bitps == 1500u );   /* decay, no spike */
}

void t_stats_bc_repeat_air_counting( void ){
    halow_ack_config_t cfg;
    statistics_radio_t st;
    uint8_t pkt[294];
    fill_payload(pkt, sizeof(pkt), 7);

    cfg_base(&cfg);
    cfg.bc_repeat = 2u;
    node_start(&cfg);
    statistics_radio_reset();

    CHECK( halow_ack_tx(pkt, sizeof(pkt), mac_broadcast) == 0 );
    CHECK( halow_ack_tx(pkt, sizeof(pkt), mac_broadcast) == 0 );

    /* contract: 2 app frames -> 4 AIR frames (test_tx capture, 1+1 repeat
     * each), but radio stats count RETICULUM frames: exactly 2, one per app
     * frame, regardless of air repeats */
    CHECK( test_tx_count() == 4 );
    CHECK( bc_count() == 2 );
    st = statistics_radio_get();
    CHECK( st.tx_packets == 2 );
    CHECK( st.tx_bytes == 2u * sizeof(pkt) );
    /* dashboard speed one second later: (0*3 + 2*294*8)/4 bit/s */
    statistics_radio_rate_tick();
    CHECK( statistics_radio_get().tx_bitps == 2u * 294u * 2u );
}

void t_stats_bc_repeat_vacancy_break( void ){
    halow_ack_config_t cfg;
    statistics_radio_t st;
    uint8_t pkt[294];
    fill_payload(pkt, sizeof(pkt), 9);

    cfg_base(&cfg);
    cfg.bc_repeat = 2u;
    node_start(&cfg);
    statistics_radio_reset();
    /* below the len+64 repeat gate: repeats are silently skipped */
    test_vacancy_set((uint32_t)sizeof(pkt) + 63u);

    CHECK( halow_ack_tx(pkt, sizeof(pkt), mac_broadcast) == 0 );
    CHECK( halow_ack_tx(pkt, sizeof(pkt), mac_broadcast) == 0 );

    CHECK( test_tx_count() == 2 );
    CHECK( bc_count() == 0 );
    st = statistics_radio_get();
    CHECK( st.tx_packets == 2 );
    CHECK( st.tx_bytes == 2u * sizeof(pkt) );
}

void t_stats_rx_air_pre_dedup( void ){
    statistics_radio_t st;
    uint8_t pkt[140];
    uint8_t ack[5];
    uint16_t plen = stats_build_rns(pkt, 0x33);
    uint16_t alen = build_legacy_ack(ack, EVM_M10, 0x1234);

    node_start(NULL);
    statistics_radio_reset();
    test_tcp_reset();

    /* four AIR copies of the same frame arrive (broadcast repeats /
     * retransmits); radio stats are registered inside deliver_rns_frame,
     * so only the copy that survives dedup is counted */
    for( int i = 0; i < 4; i++ ){
        CHECK( !halow_ack_is_internal_frame(pkt, plen) );
        halow_pkg_handler_rf_to_tcp(pkt, plen, PEER_A, MAC_ME, EVM_M10);
    }
    /* an ACK frame arrives too: internal, consumed before deliver_rns_frame,
     * must NOT enter radio stats */
    CHECK( halow_ack_is_internal_frame(ack, alen) );
    halow_pkg_handler_rf_to_tcp(ack, alen, PEER_A, MAC_ME, 0);

    st = statistics_radio_get();
    CHECK( st.rx_packets == 1 );           /* reticulum frames: deduped */
    CHECK( st.rx_bytes == (uint64_t)plen );
    CHECK( test_tcp_count() == 1 );        /* dedup collapsed 4 -> 1 */
    /* dashboard speed after one second: (0*3 + plen*8)/4 = 2*plen */
    statistics_radio_rate_tick();
    CHECK( statistics_radio_get().rx_bitps == 2u * plen );
}

void t_stats_dest_broadcast_vs_unicast( void ){
    halow_ack_config_t cfg;
    uint8_t lr[160];
    uint16_t lrlen = rns_lr_build(lr, 0xC7u, 500u);  /* seed unused by earlier scenarios */
    const test_tx_cap_t *cap;

    cfg_base(&cfg);
    fp_node_start(&cfg);   /* full pipeline init incl. link_db */

    /* unknown destination link -> must go out broadcast (staging flushes on tick) */
    CHECK( halow_pkg_handler_tcp_to_rf(lr, lrlen) >= 0 );
    run_ticks(1, 5);
    CHECK( test_tx_count() == 1 );
    cap = test_tx_at(0);
    CHECK( cap != NULL );
    CHECK( memcmp(cap->mac, mac_broadcast, 6) == 0 );

    /* peer announces itself: RX a LINKREQUEST carrying its MAC */
    halow_pkg_handler_rf_to_tcp(lr, lrlen, PEER_A, MAC_ME, EVM_M10);

    /* same destination, now learned -> unicast to PEER_A */
    CHECK( halow_pkg_handler_tcp_to_rf(lr, lrlen) >= 0 );
    run_ticks(1, 5);
    cap = test_tx_at(1);
    CHECK( cap != NULL );
    CHECK( memcmp(cap->mac, PEER_A, 6) == 0 );
}

void t_stats_roundtrip_symmetry( void ){
    halow_ack_config_t cfg;
    statistics_radio_t st;
    uint8_t pkt[3][140];
    uint16_t plen[3];

    /* full round trip with AIR amplification on both legs: every frame is
     * "sent" with repeats and received with duplicate copies. The dashboard
     * invariant: radio TX and RX both count RETICULUM frames, so after the
     * round trip tx_packets == rx_packets == frames actually handed in --
     * regardless of air repeats, retransmits or acks. A double registration
     * anywhere in the pipeline (the class of bug that showed 1 TX / 4 RX on
     * the nodes) breaks this equality. */
    cfg_base(&cfg);
    cfg.bc_repeat = 3u;
    fp_node_start(&cfg);
    statistics_radio_reset();
    test_tcp_reset();

    for( int i = 0; i < 3; i++ ){
        plen[i] = rns_pkt_build(pkt[i], (uint8_t)(0x50 + i), (uint8_t)(0x60 + i),
                                100u, 0u, 0u);
        CHECK( halow_pkg_handler_tcp_to_rf(pkt[i], plen[i]) >= 0 );
    }
    run_ticks(2, 5);
    st = statistics_radio_get();
    CHECK( st.tx_packets == 3 );      /* 3 app frames, not 3 x bc_repeat */
    CHECK( st.tx_bytes == (uint64_t)(plen[0] + plen[1] + plen[2]) );

    /* RF loopback: each frame arrives twice (repeat copy + original),
     * interleaved with peer ACK frames that must not count anywhere */
    for( int rep = 0; rep < 2; rep++ ){
        for( int i = 0; i < 3; i++ ){
            halow_pkg_handler_rf_to_tcp(pkt[i], plen[i], PEER_A, MAC_ME, EVM_M10);
        }
    }
    run_ticks(2, 5);   /* let acks/retries settle */
    st = statistics_radio_get();
    CHECK( st.rx_packets == 3 );      /* deduped to the 3 unique frames */
    CHECK( st.rx_bytes == st.tx_bytes );   /* SYMMETRY: the dashboard law */
    CHECK( st.tx_packets == 3 );      /* acks/retries did not touch TX stats */
    CHECK( test_tcp_count() == 3 );   /* exactly the 3 frames delivered */
}
