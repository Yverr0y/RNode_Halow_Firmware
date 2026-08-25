/* Channel-load meter (halow_chload): utilization must reflect REAL carried
 * traffic -- own TX airtime plus estimated RX airtime of decoded frames --
 * never energy detect. Regression root: users saw "utilisation 100%" on a
 * quiet but noisy channel (-70 dBm from a bad antenna / interference),
 * because the old metric counted ED samples above a threshold, and on such
 * a channel EVERY sample is above it. */

#include <stdlib.h>
#include "test_fw.h"
#include "halow_chload.h"

static void chload_windows( int n ){
    for( int i = 0; i < n; i++ ){
        halow_chload_cycle(100000u);
    }
}

/* A silent noisy channel reports ZERO load: no TX, no decoded frames. */
void t_chload_silent( void ){
    halow_chload_reset();
    /* even if someone notes nothing at all for a long time */
    chload_windows(25);
    CHECK( halow_chload_percent() == 0 );
}

/* TX duty cycle translates into the same percent (10 x 100 ms windows,
 * 30 ms busy each -> ~30%). */
void t_chload_tx_duty( void ){
    halow_chload_reset();
    for( int w = 0; w < 10; w++ ){
        halow_chload_note_tx_us(30000u);
        halow_chload_cycle(100000u);
    }
    int pct = halow_chload_percent();
    CHECK( pct >= 28 && pct <= 32 );
}

/* The same frame count costs far more airtime at MCS0 than at MCS7:
 * the meter must follow the modulation, not just count frames.
 * 2 x 400 B frames per 100 ms window: MCS0 ~19%, MCS7 ~2%. */
void t_chload_rx_mcs( void ){
    halow_chload_reset();
    for( int w = 0; w < 10; w++ ){
        for( int f = 0; f < 2; f++ ) halow_chload_note_rx(400u, 0);
        halow_chload_cycle(100000u);
    }
    int slow = halow_chload_percent();

    halow_chload_reset();
    for( int w = 0; w < 10; w++ ){
        for( int f = 0; f < 2; f++ ) halow_chload_note_rx(400u, 7);
        halow_chload_cycle(100000u);
    }
    int fast = halow_chload_percent();

    CHECK( slow >= 15 && slow <= 25 );   /* ~19% */
    CHECK( fast <= 5 );                  /* ~2% */
    CHECK( slow > fast * 3 );
}

/* Overlapping directions take the MAX: 50% TX + 80% RX reads ~80%. */
void t_chload_max( void ){
    halow_chload_reset();
    for( int w = 0; w < 10; w++ ){
        halow_chload_note_tx_us(50000u);
        for( int f = 0; f < 8; f++ ) halow_chload_note_rx(400u, 0);  /* ~78ms */
        halow_chload_cycle(100000u);
    }
    int pct = halow_chload_percent();
    CHECK( pct >= 75 && pct <= 85 );
}

/* A single fully-busy window among nine idle ones averages to ~10%. */
void t_chload_avg( void ){
    halow_chload_reset();
    halow_chload_note_tx_us(100000u);   /* saturated window */
    halow_chload_cycle(100000u);
    chload_windows(9);
    int pct = halow_chload_percent();
    CHECK( pct >= 8 && pct <= 12 );
}

/* Saturating loads clamp at exactly 100% (the whole 10-window ring full
 * of over-budget windows), and reset clears everything. */
void t_chload_sat_reset( void ){
    halow_chload_reset();
    for( int w = 0; w < 12; w++ ){      /* > ring depth: every kept window full */
        halow_chload_note_tx_us(150000u);   /* over the window */
        halow_chload_note_tx_us(150000u);
        halow_chload_cycle(100000u);
    }
    CHECK( halow_chload_percent() == 100 );

    halow_chload_reset();
    chload_windows(3);
    CHECK( halow_chload_percent() == 0 );
}
