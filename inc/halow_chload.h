#ifndef __HALOW_CHLOAD_H_
#define __HALOW_CHLOAD_H_

#include <stdint.h>

/* Channel load = share of time the radio actually CARRIES traffic: our own
 * TX airtime plus the estimated airtime of decoded RX frames. Energy-detect
 * (RSSI sampling) is deliberately NOT an input -- a noisy antenna at
 * -70 dBm with zero stations on air must read 0%, not 100%. The ED-based
 * "busy" fraction stays available separately for LBT tuning diagnostics
 * (halow_lbt_ed_busy_pct_get). */

void     halow_chload_reset(void);
void     halow_chload_note_tx_us(uint32_t us);        /* TX busy in this window */
void     halow_chload_note_rx(uint16_t len, uint8_t mcs);  /* decoded frame */
void     halow_chload_cycle(uint32_t cycle_us);       /* close current window */
uint8_t  halow_chload_percent(void);                  /* int percent, window-avg */

#endif // __HALOW_CHLOAD_H_
