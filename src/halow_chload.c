#include "halow_chload.h"

#include <string.h>

/* Rolling window of HALOW_CHLOAD_WINDOWS cycles (the LBT task closes one
 * every 100 ms). Each window accumulates TX-busy time and the estimated
 * RX airtime of decoded frames; the reported load is the larger of the
 * two averages -- TX and RX can overlap in a half-duplex radio only in
 * the sense that whichever direction is busier defines occupancy. */

#define CHLOAD_WINDOWS       10u
#define CHLOAD_SYM_US        36u   /* S1G 1 MHz OFDM symbol */
#define CHLOAD_SIFS_US       64u   /* per-frame overhead, rough */

/* NDBPS for S1G 1 MHz, MCS0..7; MCS10 = 1 MHz duplicate. */
static const uint16_t chload_ndbps[9] = {
    12u, 24u, 36u, 48u, 72u, 96u, 108u, 120u, 6u /* idx 8 == MCS10 */
};

typedef struct {
    uint32_t tx_us;
    uint32_t rx_us;
} chload_win_t;

static chload_win_t chload_ring[CHLOAD_WINDOWS];
static uint8_t      chload_idx;
static uint32_t     chload_tx_open_us;
static uint32_t     chload_rx_open_us;

static uint32_t chload_rx_frame_us( uint16_t len, uint8_t mcs )
{
    uint8_t  idx;
    uint16_t ndbps;
    uint32_t bits;
    uint32_t syms;

    idx = ( mcs == 10u ) ? 8u : mcs;
    if( idx > 8u ) idx = 0u;
    ndbps = chload_ndbps[idx];

    bits = ((uint32_t)len * 8u) + 14u;
    syms = (bits + ndbps - 1u) / ndbps;
    if( syms == 0u ) syms = 1u;

    return syms * CHLOAD_SYM_US + CHLOAD_SIFS_US;
}

void halow_chload_reset( void )
{
    memset(chload_ring, 0, sizeof(chload_ring));
    chload_idx        = 0u;
    chload_tx_open_us = 0u;
    chload_rx_open_us = 0u;
}

void halow_chload_note_tx_us( uint32_t us )
{
    chload_tx_open_us += us;
    if( chload_tx_open_us < us ) chload_tx_open_us = 0xFFFFFFFFu;
}

void halow_chload_note_rx( uint16_t len, uint8_t mcs )
{
    uint32_t us = chload_rx_frame_us(len, mcs);
    chload_rx_open_us += us;
    if( chload_rx_open_us < us ) chload_rx_open_us = 0xFFFFFFFFu;
}

void halow_chload_cycle( uint32_t cycle_us )
{
    if( cycle_us == 0u ) cycle_us = 1u;

    chload_ring[chload_idx].tx_us = chload_tx_open_us;
    chload_ring[chload_idx].rx_us = chload_rx_open_us;
    chload_idx = (uint8_t)((chload_idx + 1u) % CHLOAD_WINDOWS);

    chload_tx_open_us = 0u;
    chload_rx_open_us = 0u;
    (void)cycle_us;
}

uint8_t halow_chload_percent( void )
{
    uint64_t tx = 0u;
    uint64_t rx = 0u;
    uint64_t big;
    /* window length is supplied by the caller of cycle(); assume the
     * production cadence (100 ms) for the percent conversion */
    const uint64_t win_us = 100000u * CHLOAD_WINDOWS;

    for( uint8_t i = 0u; i < CHLOAD_WINDOWS; i++ ){
        tx += chload_ring[i].tx_us;
        rx += chload_ring[i].rx_us;
    }
    big = ( tx > rx ) ? tx : rx;
    if( big == 0u ) return 0u;
    if( big >= win_us ) return 100u;

    /* round-half-up to an integer percent */
    return (uint8_t)(((big * 100u) + (win_us / 2u)) / win_us);
}
