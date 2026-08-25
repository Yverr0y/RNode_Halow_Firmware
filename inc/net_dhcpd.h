#ifndef __NET_DHCPD_H_
#define __NET_DHCPD_H_

#include <stdint.h>

/* Minimal DHCP server for direct phone/tablet connection (USB-Ethernet or
 * Ethernet dongle on the client side): hands out up to 4 leases from the
 * tail of the device's own static subnet, router/DNS = the device itself.
 *
 * SAFETY CONTRACT:
 *  - compiled in but DISABLED by default; nothing starts unless
 *    configdb "net.dhs" is 1 AND the device runs a STATIC address;
 *  - enabling it while the DHCP CLIENT is active is refused (a client-mode
 *    device sits inside somebody else's network -- usually the home LAN --
 *    and a rogue server there can poison it);
 *  - start/stop are idempotent. */

void     net_dhcpd_start( void );
void     net_dhcpd_stop( void );
uint8_t  net_dhcpd_running( void );   /* 1 when the UDP socket is open */
uint8_t  net_dhcpd_enabled_cfg( void ); /* persisted enable flag */
void     net_dhcpd_set_enabled( int32_t en ); /* persist + caller triggers on_netcfg */
/* Re-evaluate server presence against current config/mode; safe to call
 * on every config change and at boot. Starts only if enabled+static. */
void     net_dhcpd_on_netcfg( void );

#endif // __NET_DHCPD_H_
