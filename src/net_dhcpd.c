#include "sys_config.h"
#define LOG_LOCAL_LEVEL LOG_LEVEL_NET_IP
#include "basic_include.h"
#include "lib/logc/log.h"

#include "net_dhcpd.h"
#include "dhcpd_core.h"
#include "net_ip.h"
#include "configdb.h"
#include "utils.h"
#include "lwip/udp.h"
#include "lwip/ip_addr.h"
#include "lwip/tcpip.h"

#define NET_DHCPD_CFG        CONFIGDB_ADD_MODULE("net")
#define NET_DHCPD_CFG_EN     NET_DHCPD_CFG ".dhs"

#define DHCPD_SERVER_PORT    67u
#define DHCPD_CLIENT_PORT    68u
#define DHCPD_LEASE_MS       (12u * 60u * 60u * 1000u)   /* 12 h */
#define DHCPD_POOL_SIZE      4u
#define DHCPD_POOL_TAIL      200u   /* offers x.x.x.200 .. .200+size-1 */

static struct udp_pcb *g_dhcpd_pcb;
static dhcpd_state_t   g_dhcpd_state;
static uint8_t         g_dhcpd_running;
/* recv buffers: touched only from the tcpip thread (udp_recv cb) */
static uint8_t         g_dhcpd_req[DHCPD_PKT_MAX];
static uint8_t         g_dhcpd_rep[DHCPD_PKT_MAX];

uint8_t net_dhcpd_enabled_cfg( void ){
    int32_t en = 0;
    configdb_get_i32(NET_DHCPD_CFG_EN, &en);
    return ( en != 0u ) ? 1u : 0u;
}

void net_dhcpd_set_enabled( int32_t en ){
    configdb_set_i32(NET_DHCPD_CFG_EN, &en);
}

/* Build the pool from the address the interface holds RIGHT NOW: same
 * subnet, tail addresses. net_ip_config_load() overwrites ip/mask with the
 * SAVED config, so capture runtime values first and serve those. Refuses
 * when the device is a DHCP client (unstable identity, usually the home
 * LAN) or has no address yet. */
static bool net_dhcpd_fill_state( void ){
    net_ip_config_t cfg;
    uint32_t rt_ip, rt_mask;

    memset(&cfg, 0, sizeof(cfg));
    net_ip_config_fill_runtime_addrs(&cfg);
    rt_ip   = cfg.ip.addr;
    rt_mask = cfg.mask.addr;

    if( rt_ip == 0u || rt_mask == 0u ){
        log_warn("dhcpd: no address on the interface");
        return false;
    }

    net_ip_config_load(&cfg);
    if( cfg.mode != NET_IP_MODE_STATIC ){
        log_warn("dhcpd: refused, device is a DHCP client");
        return false;
    }
    if( net_dhcpd_enabled_cfg() == 0u ){
        log_warn("dhcpd: refused, not enabled in config");
        return false;
    }

    g_dhcpd_state.server_ip = rt_ip;
    g_dhcpd_state.netmask   = rt_mask;
    g_dhcpd_state.pool_base = ( rt_ip & rt_mask ) | lwip_htonl(DHCPD_POOL_TAIL);
    g_dhcpd_state.pool_size = DHCPD_POOL_SIZE;
    g_dhcpd_state.lease_ms  = DHCPD_LEASE_MS;
    memset(g_dhcpd_state.leases, 0, sizeof(g_dhcpd_state.leases));
    return true;
}

/* Runs in the tcpip thread (udp_recv callback): single-threaded by
 * construction, no locking needed around g_dhcpd_state. */
static void net_dhcpd_recv( void *arg, struct udp_pcb *pcb, struct pbuf *p,
                            const ip_addr_t *addr, u16_t port ){
    dhcpd_req_t q;
    uint16_t n;
    int reply_type;
    uint32_t offer_ip;
    uint64_t now_ms;
    struct pbuf *out;

    (void)arg; (void)addr; (void)port;

    /* tot_len: DHCP may arrive as a chained pbuf; copy across the chain */
    if( p == NULL || p->tot_len == 0 || p->tot_len > DHCPD_PKT_MAX ){
        if( p != NULL ) pbuf_free(p);
        return;
    }
    n = (uint16_t)p->tot_len;
    pbuf_copy_partial(p, g_dhcpd_req, n, 0);
    pbuf_free(p);

    if( dhcpd_parse(g_dhcpd_req, n, &q) != 0 ){
        return;
    }

    now_ms = (uint64_t)get_time_ms();
    if( dhcpd_decide(&g_dhcpd_state, &q, now_ms, &reply_type, &offer_ip) == 0 ||
        reply_type == 0 ){
        return;
    }

    n = dhcpd_build(g_dhcpd_rep, sizeof(g_dhcpd_rep), reply_type, q.xid, q.mac,
                    offer_ip, g_dhcpd_state.server_ip, g_dhcpd_state.netmask,
                    (uint32_t)(DHCPD_LEASE_MS / 1000u));
    if( n == 0u ){
        return;
    }

    /* DHCP replies to an unconfigured client go to the LAN broadcast */
    out = pbuf_alloc(PBUF_TRANSPORT, n, PBUF_RAM);
    if( out == NULL ){
        return;
    }
    if( pbuf_take(out, g_dhcpd_rep, n) == ERR_OK ){
        ip_addr_t bc;
        IP_ADDR4(&bc, 255, 255, 255, 255);
        (void)udp_sendto(pcb, out, &bc, DHCPD_CLIENT_PORT);
    }
    pbuf_free(out);
}

static void net_dhcpd_stop_cb( void *arg );

static void net_dhcpd_start_cb( void *arg ){
    (void)arg;

    /* Re-evaluate on every start: mode/address may have changed since the
     * socket opened. Invalid now (client mode / disabled / no address) ->
     * tear the running server down instead of serving a stale pool. */
    if( !net_dhcpd_fill_state() ){
        net_dhcpd_stop_cb(NULL);
        return;
    }
    if( g_dhcpd_pcb != NULL ){
        return;                       /* already serving, state refreshed */
    }

    g_dhcpd_pcb = udp_new();
    if( g_dhcpd_pcb == NULL ){
        log_error("dhcpd: udp_new failed");
        return;
    }
    if( udp_bind(g_dhcpd_pcb, IP_ADDR_ANY, DHCPD_SERVER_PORT) != ERR_OK ){
        udp_remove(g_dhcpd_pcb);
        g_dhcpd_pcb = NULL;
        log_error("dhcpd: bind :67 failed");
        return;
    }
    udp_recv(g_dhcpd_pcb, net_dhcpd_recv, NULL);
    g_dhcpd_running = 1u;
    log_info("dhcpd: serving pool base %s size %u",
             ip4addr_ntoa((ip4_addr_t *)&g_dhcpd_state.pool_base),
             (unsigned)g_dhcpd_state.pool_size);
}

static void net_dhcpd_stop_cb( void *arg ){
    (void)arg;
    if( g_dhcpd_pcb != NULL ){
        udp_remove(g_dhcpd_pcb);
        g_dhcpd_pcb = NULL;
    }
    g_dhcpd_running = 0u;
    log_info("dhcpd: stopped");
}

void net_dhcpd_start( void ){
    if( tcpip_try_callback(net_dhcpd_start_cb, NULL) != ERR_OK ){
        log_error("dhcpd: start callback failed");
    }
}

void net_dhcpd_stop( void ){
    if( tcpip_try_callback(net_dhcpd_stop_cb, NULL) != ERR_OK ){
        log_error("dhcpd: stop callback failed");
    }
}

uint8_t net_dhcpd_running( void ){
    return g_dhcpd_running;
}

/* Called from the network-config apply path: (re)evaluate server presence
 * against the CURRENT address/mode. Safe on every apply. */
void net_dhcpd_on_netcfg( void ){
    if( net_dhcpd_enabled_cfg() ){
        net_dhcpd_start();
    }else{
        net_dhcpd_stop();
    }
}
