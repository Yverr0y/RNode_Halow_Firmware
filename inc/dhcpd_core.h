#ifndef __DHCPD_CORE_H_
#define __DHCPD_CORE_H_

#include <stdint.h>

/* PURE DHCP-server core: packet parsing/reply building and lease bookkeeping
 * over plain byte buffers. No lwIP, no OS -- fully host-testable.
 * The lwIP shell lives in src/net_dhcpd.c. */

#define DHCPD_LEASES        4u
/* RFC 2131: clients may send DHCP messages up to 576 IP bytes; real phone
 * DISCOVERs run ~280-345 bytes (parameter-request list + client-id +
 * hostname) -- 300 was too small and silently dropped them. */
#define DHCPD_PKT_MAX       548u

enum {
    DHCPD_DISCOVER = 1,
    DHCPD_OFFER    = 2,
    DHCPD_REQUEST  = 3,
    DHCPD_DECLINE  = 4,
    DHCPD_ACK      = 5,
    DHCPD_NAK      = 7,
    DHCPD_RELEASE  = 8
};

typedef struct {
    uint8_t  msgtype;                 /* DISCOVER / REQUEST / DECLINE / RELEASE */
    uint32_t xid;
    uint8_t  mac[6];
    uint32_t req_ip;                  /* option 50, 0 = none */
} dhcpd_req_t;

typedef struct {
    uint8_t  used;
    uint8_t  mac[6];
    uint32_t ip;
    uint64_t expires_ms;
} dhcpd_lease_t;

typedef struct {
    uint32_t       server_ip;         /* our address on that segment */
    uint32_t       netmask;
    uint32_t       pool_base;         /* first offerable address */
    uint8_t        pool_size;         /* <= DHCPD_LEASES */
    uint32_t       lease_ms;
    dhcpd_lease_t  leases[DHCPD_LEASES];
} dhcpd_state_t;

void dhcpd_state_init( dhcpd_state_t *st, uint32_t server_ip, uint32_t netmask,
                       uint32_t pool_base, uint8_t pool_size );

/* Parse a received UDP payload. Returns 0 and fills q when the datagram is a
 * BOOTREQUEST worth answering; -1 when it must be ignored silently. */
int      dhcpd_parse( const uint8_t *pkt, uint16_t len, dhcpd_req_t *q );

/* Decide and mutate state. Returns the reply message type
 * (OFFER / ACK / NAK) or 0 = send nothing. On OFFER/ACK, *offer_ip holds
 * the address to hand out. */
int      dhcpd_decide( dhcpd_state_t *st, const dhcpd_req_t *q, uint64_t now_ms,
                       int *reply_type, uint32_t *offer_ip );

/* Build a reply packet into buf; returns length or 0 on overflow. */
uint16_t dhcpd_build( uint8_t *buf, uint16_t buflen,
                      int reply_type, uint32_t xid, const uint8_t mac[6],
                      uint32_t yiaddr, uint32_t server_ip, uint32_t netmask,
                      uint32_t lease_s );

#endif // __DHCPD_CORE_H_
