#include "dhcpd_core.h"

#include <string.h>

/* ---- wire constants ---- */
#define DHCP_OP_REQUEST  1u
#define DHCP_OP_REPLY    2u
#define DHCP_MAGIC0      0x63u
#define DHCP_MAGIC1      0x82u
#define DHCP_MAGIC2      0x53u
#define DHCP_MAGIC3      0x63u

#define OPT_END        255u
#define OPT_SUBNET     1u
#define OPT_ROUTER     3u
#define OPT_DNS        6u
#define OPT_LEASE      51u
#define OPT_MSGTYPE    53u
#define OPT_SERVERID   54u
#define OPT_REQ_IP     50u

/* fixed part of a DHCP message up to magic cookie */
#define DHCP_FIXED     240u

static void put32( uint8_t *p, uint32_t v ){
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);  p[3] = (uint8_t)v;
}

static uint32_t get32( const uint8_t *p ){
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

/* ---------------- state ---------------- */

void dhcpd_state_init( dhcpd_state_t *st, uint32_t server_ip, uint32_t netmask,
                       uint32_t pool_base, uint8_t pool_size )
{
    if( st == NULL ) return;
    memset(st, 0, sizeof(*st));
    st->server_ip = server_ip;
    st->netmask   = netmask;
    st->pool_base = pool_base;
    st->pool_size = pool_size;
    st->lease_ms  = 12u * 60u * 60u * 1000u;   /* 12 h default */
}

/* ---------------- parse ---------------- */

int dhcpd_parse( const uint8_t *pkt, uint16_t len, dhcpd_req_t *q )
{
    uint16_t i;
    uint8_t msgtype = 0u;
    int have_type = 0;

    if( pkt == NULL || q == NULL || len < DHCP_FIXED ) return -1;
    if( pkt[0] != DHCP_OP_REQUEST ) return -1;              /* we answer clients only */
    if( pkt[1] != 1u || pkt[2] != 6u ) return -1;           /* htype=ethernet hlen=6 */
    if( pkt[236] != DHCP_MAGIC0 || pkt[237] != DHCP_MAGIC1 ||
        pkt[238] != DHCP_MAGIC2 || pkt[239] != DHCP_MAGIC3 ) return -1;

    memset(q, 0, sizeof(*q));
    q->xid = get32(&pkt[4]);
    memcpy(q->mac, &pkt[28], 6);

    /* options start at 240; tolerate missing END and pad bytes */
    for( i = 240u; i < len; ){
        uint8_t opt = pkt[i];
        if( opt == OPT_END ) break;
        if( opt == 0u ){ i++; continue; }
        if( (uint16_t)(i + 1u) >= len ) return -1;
        {
            uint8_t olen = pkt[i + 1u];
            if( (uint16_t)(i + 2u + olen) > len ) return -1;
            if( opt == OPT_MSGTYPE && olen == 1u && !have_type ){
                msgtype = pkt[i + 2u];
                have_type = 1;
            }else if( opt == OPT_REQ_IP && olen == 4u ){
                q->req_ip = get32(&pkt[i + 2u]);
            }
            i = (uint16_t)(i + 2u + olen);
        }
    }

    switch( msgtype ){
        case DHCPD_DISCOVER:
        case DHCPD_REQUEST:
        case DHCPD_DECLINE:
        case DHCPD_RELEASE:
            q->msgtype = msgtype;
            return 0;
        default:
            return -1;
    }
}

/* ---------------- leases ---------------- */

static dhcpd_lease_t *lease_by_mac( dhcpd_state_t *st, const uint8_t mac[6] )
{
    for( uint8_t k = 0u; k < DHCPD_LEASES; k++ ){
        if( st->leases[k].used &&
            memcmp(st->leases[k].mac, mac, 6) == 0 ){
            return &st->leases[k];
        }
    }
    return NULL;
}

static dhcpd_lease_t *lease_free_pick( dhcpd_state_t *st, uint64_t now_ms )
{
    /* prefer a free slot; else an expired one */
    for( uint8_t k = 0u; k < st->pool_size && k < DHCPD_LEASES; k++ ){
        dhcpd_lease_t *l = &st->leases[k];
        if( !l->used ) return l;
    }
    for( uint8_t k = 0u; k < st->pool_size && k < DHCPD_LEASES; k++ ){
        dhcpd_lease_t *l = &st->leases[k];
        if( l->expires_ms <= now_ms ) return l;
    }
    return NULL;
}

static uint32_t pool_ip( const dhcpd_state_t *st, uint8_t slot )
{
    return st->pool_base + (uint32_t)slot;
}

/* ---------------- decide ---------------- */

int dhcpd_decide( dhcpd_state_t *st, const dhcpd_req_t *q, uint64_t now_ms,
                  int *reply_type, uint32_t *offer_ip )
{
    dhcpd_lease_t *l;

    if( st == NULL || q == NULL || reply_type == NULL || offer_ip == NULL ){
        return 0;
    }
    *reply_type = 0;
    *offer_ip   = 0u;

    switch( q->msgtype ){

    case DHCPD_RELEASE:
        l = lease_by_mac(st, q->mac);
        if( l != NULL ){
            l->used = 0u;
        }
        return 0;                                   /* RFC: no reply */

    case DHCPD_DECLINE:
        /* the client says this address is in use: drop OUR lease so it is
         * not offered again until it expires */
        l = lease_by_mac(st, q->mac);
        if( l != NULL ){
            l->used = 0u;
        }
        return 0;

    case DHCPD_DISCOVER:
        l = lease_by_mac(st, q->mac);
        if( l == NULL || l->expires_ms <= now_ms ){
            l = lease_free_pick(st, now_ms);
            if( l == NULL ) return 0;               /* pool dry: stay silent */
            memset(l, 0, sizeof(*l));
            memcpy(l->mac, q->mac, 6);
            /* keep the slot's natural address */
            l->ip       = pool_ip(st, (uint8_t)(l - st->leases));
            l->used     = 1u;
            l->expires_ms = now_ms + st->lease_ms;   /* tentative */
        }
        *reply_type = DHCPD_OFFER;
        *offer_ip   = l->ip;
        return DHCPD_OFFER;

    case DHCPD_REQUEST:
        l = lease_by_mac(st, q->mac);
        if( l != NULL ){
            /* re-requesting its known address: accept if consistent */
            uint32_t ip = ( q->req_ip != 0u ) ? q->req_ip : l->ip;
            if( ip != l->ip ){
                *reply_type = DHCPD_NAK;
                return DHCPD_NAK;
            }
            l->expires_ms = now_ms + st->lease_ms;
            *reply_type = DHCPD_ACK;
            *offer_ip   = l->ip;
            return DHCPD_ACK;
        }

        /* new client committing: must name a free/expired pool address.
         * Prefer the slot whose natural address IS the request -- that
         * keeps every slot's address stable across churn. */
        if( q->req_ip != 0u &&
            q->req_ip >= st->pool_base &&
            q->req_ip < pool_ip(st, st->pool_size) &&
            ((q->req_ip & st->netmask) == (st->server_ip & st->netmask)) ){
            dhcpd_lease_t *f = NULL;

            for( uint8_t k = 0u; k < st->pool_size && k < DHCPD_LEASES; k++ ){
                dhcpd_lease_t *c = &st->leases[k];
                if( pool_ip(st, k) == q->req_ip &&
                    ( !c->used || c->expires_ms <= now_ms ) ){
                    f = c;
                    break;
                }
            }
            /* f == NULL -> that address is live-owned by someone else:
             * never hand it out twice */
            if( f != NULL ){
                memset(f, 0, sizeof(*f));
                memcpy(f->mac, q->mac, 6);
                f->ip         = q->req_ip;
                f->used       = 1u;
                f->expires_ms = now_ms + st->lease_ms;
                *reply_type = DHCPD_ACK;
                *offer_ip   = q->req_ip;
                return DHCPD_ACK;
            }
        }
        *reply_type = DHCPD_NAK;                    /* asking for what we cannot give */
        return DHCPD_NAK;

    default:
        return 0;
    }
}

/* ---------------- build ---------------- */

static uint8_t *opt4( uint8_t *p, uint8_t code, uint32_t v ){
    *p++ = code; *p++ = 4u; put32(p, v); return p + 4u;
}

uint16_t dhcpd_build( uint8_t *buf, uint16_t buflen,
                      int reply_type, uint32_t xid, const uint8_t mac[6],
                      uint32_t yiaddr, uint32_t server_ip, uint32_t netmask,
                      uint32_t lease_s )
{
    uint8_t *p;

    if( buf == NULL || mac == NULL || buflen < (DHCP_FIXED + 40u) ) return 0u;

    memset(buf, 0, buflen);
    buf[0] = DHCP_OP_REPLY;
    buf[1] = 1u;                       /* htype ethernet */
    buf[2] = 6u;                       /* hlen */
    buf[3] = 0u;                       /* hops */
    put32(&buf[4], xid);
    /* flags: leave 0 -- we always broadcast replies, so broadcast flag is
     * irrelevant; yiaddr is filled either way */
    put32(&buf[16], yiaddr);           /* yiaddr */
    put32(&buf[20], server_ip);        /* siaddr (next server) */
    memcpy(&buf[28], mac, 6);
    buf[236] = DHCP_MAGIC0;
    buf[237] = DHCP_MAGIC1;
    buf[238] = DHCP_MAGIC2;
    buf[239] = DHCP_MAGIC3;

    p = &buf[240];
    *p++ = OPT_MSGTYPE; *p++ = 1u; *p++ = (uint8_t)reply_type;
    p = opt4(p, OPT_SERVERID, server_ip);
    if( reply_type == DHCPD_ACK ){
        p = opt4(p, OPT_LEASE, lease_s);
        p = opt4(p, OPT_SUBNET, netmask);
        p = opt4(p, OPT_ROUTER, server_ip);
        p = opt4(p, OPT_DNS, server_ip);
    }
    *p++ = OPT_END;

    return (uint16_t)(p - buf);
}
