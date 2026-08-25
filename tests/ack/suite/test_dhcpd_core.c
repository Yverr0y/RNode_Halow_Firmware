/* DHCP-server core (dhcpd_core): pure packet/lease logic for the direct
 * phone-connection DHCP server. The lwIP shell is NOT exercised here. */

#include <string.h>
#include "test_fw.h"
#include "dhcpd_core.h"

#define SRV_IP   0xC0A8012Au   /* 192.168.1.42 */
#define MASK     0xFFFFFF00u
#define POOL     0xC0A801C8u   /* 192.168.1.200 */

/* Build a minimal client request the way a phone would. */
static uint16_t mk_req( uint8_t *buf, uint8_t type, uint32_t xid,
                        const uint8_t mac[6], uint32_t req_ip ){
    memset(buf, 0, 240u + 8u);
    buf[0] = 1u; buf[1] = 1u; buf[2] = 6u;
    buf[4] = (uint8_t)(xid >> 24); buf[5] = (uint8_t)(xid >> 16);
    buf[6] = (uint8_t)(xid >> 8);  buf[7] = (uint8_t)xid;
    memcpy(&buf[28], mac, 6);
    buf[236] = 0x63; buf[237] = 0x82; buf[238] = 0x53; buf[239] = 0x63;
    {
        uint8_t *p = &buf[240];
        *p++ = 53u; *p++ = 1u; *p++ = type;
        if( req_ip != 0u ){
            *p++ = 50u; *p++ = 4u;
            p[0] = (uint8_t)(req_ip >> 24); p[1] = (uint8_t)(req_ip >> 16);
            p[2] = (uint8_t)(req_ip >> 8);  p[3] = (uint8_t)req_ip;
            p += 4u;
        }
        *p++ = 255u;
        return (uint16_t)(p - buf);
    }
}

static const uint8_t MAC_A[6] = {0xAA,0xAA,0xAA,0xAA,0xAA,0xAA};
static const uint8_t MAC_B[6] = {0xBB,0xBB,0xBB,0xBB,0xBB,0xBB};
static const uint8_t MAC_C[6] = {0xCC,0xCC,0xCC,0xCC,0xCC,0xCC};
static const uint8_t MAC_D[6] = {0xDD,0xDD,0xDD,0xDD,0xDD,0xDD};
static const uint8_t MAC_E[6] = {0xEE,0xEE,0xEE,0xEE,0xEE,0xEE};

/* Full happy path for one client: discover -> offer(pool ip), request ->
 * ack, re-request -> ack same ip, release -> lease freed. */
void t_dhcpd_happy_path( void ){
    dhcpd_state_t s;
    dhcpd_req_t q;
    uint8_t buf[300];
    int rt;
    uint32_t ip;
    uint16_t n;

    dhcpd_state_init(&s, SRV_IP, MASK, POOL, 4);

    n = mk_req(buf, DHCPD_DISCOVER, 0x1111, MAC_A, 0);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( q.msgtype == DHCPD_DISCOVER && q.xid == 0x1111 &&
           memcmp(q.mac, MAC_A, 6) == 0 && q.req_ip == 0 );
    CHECK( dhcpd_decide(&s, &q, 1000, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_OFFER && ip == POOL );

    n = mk_req(buf, DHCPD_REQUEST, 0x1112, MAC_A, POOL);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 1100, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_ACK && ip == POOL );

    /* re-request without option 50 keeps the same address */
    n = mk_req(buf, DHCPD_REQUEST, 0x1113, MAC_A, 0);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 1200, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_ACK && ip == POOL );

    n = mk_req(buf, DHCPD_RELEASE, 0x1114, MAC_A, 0);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 1300, &rt, &ip) == 0 );   /* silent */
    CHECK( rt == 0 );

    /* freed: a discover again gets the same slot address */
    n = mk_req(buf, DHCPD_DISCOVER, 0x1115, MAC_A, 0);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 1400, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_OFFER && ip == POOL );
}

/* Distinct clients get distinct pool addresses; the pool dries up after 4
 * and the server stays silent for the fifth. */
void t_dhcpd_pool_exhaust( void ){
    dhcpd_state_t s;
    dhcpd_req_t q;
    uint8_t buf[300];
    int rt; uint32_t ip;
    const uint8_t *macs[5] = {MAC_A, MAC_B, MAC_C, MAC_D, MAC_E};

    dhcpd_state_init(&s, SRV_IP, MASK, POOL, 4);
    for( int i = 0; i < 5; i++ ){
        uint16_t n = mk_req(buf, DHCPD_DISCOVER, (uint32_t)(0x2000 + i), macs[i], 0);
        int ret;
        CHECK( dhcpd_parse(buf, n, &q) == 0 );
        ret = dhcpd_decide(&s, &q, 2000, &rt, &ip);
        CHECK( ret == rt );
        if( i < 4 ){
            CHECK( rt == DHCPD_OFFER );
            CHECK( ip == (uint32_t)(POOL + i) );
        }else{
            CHECK( rt == 0 );   /* pool dry: silence, never NAK a discover */
        }
    }

    /* after the 12 h lease everything is offerable again */
    {
        uint16_t n = mk_req(buf, DHCPD_DISCOVER, 0x2099, MAC_E, 0);
        int ret;
        CHECK( dhcpd_parse(buf, n, &q) == 0 );
        ret = dhcpd_decide(&s, &q, 2000 + 12ull*60*60*1000 + 1000, &rt, &ip);
        CHECK( ret == rt );
        CHECK( rt == DHCPD_OFFER );
    }
}

/* REQUEST for something we cannot give is a NAK; out-of-pool, wrong
 * subnet, and unknown-client requests all refuse. */
void t_dhcpd_nak_rules( void ){
    dhcpd_state_t s;
    dhcpd_req_t q;
    uint8_t buf[300];
    int rt; uint32_t ip;
    uint16_t n;

    dhcpd_state_init(&s, SRV_IP, MASK, POOL, 4);

    /* a fresh client committing to a FREE pool address gets it */
    n = mk_req(buf, DHCPD_REQUEST, 0x3001, MAC_B, POOL + 2);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 3000, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_ACK && ip == POOL + 2 );

    /* standard flow for MAC_A: discover -> offer -> request(that ip) -> ack */
    n = mk_req(buf, DHCPD_DISCOVER, 0x3002, MAC_A, 0);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 3000, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_OFFER && ip == POOL );
    n = mk_req(buf, DHCPD_REQUEST, 0x3003, MAC_A, POOL);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 3100, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_ACK && ip == POOL );

    /* MAC_A asking for a DIFFERENT address than its lease -> NAK */
    n = mk_req(buf, DHCPD_REQUEST, 0x3004, MAC_A, POOL + 1);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 3200, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_NAK );

    /* someone else asking for MAC_A's LIVE lease -> refused */
    n = mk_req(buf, DHCPD_REQUEST, 0x3005, MAC_C, POOL);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 3300, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_NAK );

    /* out of subnet: refused even for a fresh client */
    n = mk_req(buf, DHCPD_REQUEST, 0x3006, MAC_D, 0x0A000001u /*10.0.0.1*/);
    CHECK( dhcpd_parse(buf, n, &q) == 0 );
    CHECK( dhcpd_decide(&s, &q, 3400, &rt, &ip) != 0 );
    CHECK( rt == DHCPD_NAK );
}

/* Malformed datagrams are ignored silently: short, wrong op, wrong
 * htype/hlen, missing cookie, truncated option. */
void t_dhcpd_malformed( void ){
    dhcpd_req_t q;
    uint8_t buf[300];

    memset(buf, 0, sizeof(buf));
    CHECK( dhcpd_parse(buf, 100, &q) == -1 );          /* too short */

    mk_req(buf, DHCPD_DISCOVER, 1, MAC_A, 0);
    buf[0] = 2u;                                        /* BOOTREPLY */
    CHECK( dhcpd_parse(buf, 260, &q) == -1 );

    mk_req(buf, DHCPD_DISCOVER, 1, MAC_A, 0);
    buf[1] = 2u;                                        /* htype != 1 */
    CHECK( dhcpd_parse(buf, 260, &q) == -1 );

    mk_req(buf, DHCPD_DISCOVER, 1, MAC_A, 0);
    buf[236] = 0u;                                      /* broken cookie */
    CHECK( dhcpd_parse(buf, 260, &q) == -1 );

    mk_req(buf, DHCPD_DISCOVER, 1, MAC_A, 0);
    buf[241] = 200u;                                    /* option len overruns */
    CHECK( dhcpd_parse(buf, 243, &q) == -1 );

    /* unknown message type inside a well-formed packet: ignored */
    mk_req(buf, 9, 1, MAC_A, 0);
    CHECK( dhcpd_parse(buf, 260, &q) == -1 );
}

/* Reply builder: layout, fixed fields, option tail, and overflow guard. */
void t_dhcpd_build( void ){
    uint8_t b[300];
    uint16_t n;

    n = dhcpd_build(b, sizeof(b), DHCPD_OFFER, 0xBEEF, MAC_A,
                    POOL, SRV_IP, MASK, 43200u);
    CHECK( n > 240 );
    CHECK( b[0] == 2u && b[1] == 1u && b[2] == 6u );
    CHECK( b[4] == 0 && b[5] == 0 && b[6] == 0xBE && b[7] == 0xEF );
    CHECK( b[16] == 0xC0 && b[17] == 0xA8 && b[18] == 0x01 && b[19] == 0xC8 );
    CHECK( b[28] == 0xAA && b[33] == 0xAA );
    CHECK( b[236] == 0x63 && b[237] == 0x82 && b[238] == 0x53 && b[239] == 0x63 );
    CHECK( b[240] == 53u && b[242] == DHCPD_OFFER );
    /* server id option present */
    CHECK( b[243] == 54u && b[244] == 4u );
    CHECK( b[245] == 0xC0 && b[246] == 0xA8 && b[247] == 0x01 && b[248] == 0x2A );

    /* ACK carries lease/mask/router options; OFFER must not */
    memset(b, 0, sizeof(b));
    n = dhcpd_build(b, sizeof(b), DHCPD_ACK, 1, MAC_A, POOL, SRV_IP, MASK, 43200u);
    {
        int has_lease = 0, has_router = 0;
        for( uint16_t i = 240; i < n - 1; ){
            if( b[i] == 255u ) break;
            if( b[i] == 0u ){ i++; continue; }
            if( b[i] == 51u ) has_lease = 1;
            if( b[i] == 3u )  has_router = 1;
            i = (uint16_t)(i + 2u + b[i + 1u]);
        }
        CHECK( has_lease && has_router );
    }

    memset(b, 0, sizeof(b));
    n = dhcpd_build(b, sizeof(b), DHCPD_OFFER, 1, MAC_A, POOL, SRV_IP, MASK, 43200u);
    {
        int has_lease = 0;
        for( uint16_t i = 240; i < n - 1; ){
            if( b[i] == 255u ) break;
            if( b[i] == 0u ){ i++; continue; }
            if( b[i] == 51u ) has_lease = 1;
            i = (uint16_t)(i + 2u + b[i + 1u]);
        }
        CHECK( !has_lease );
    }

    CHECK( dhcpd_build(b, 40, DHCPD_ACK, 1, MAC_A, POOL, SRV_IP, MASK, 1) == 0 );
}
