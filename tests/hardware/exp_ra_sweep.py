#!/usr/bin/env python3
"""RA-effectiveness experiment on the live (degraded) channel.

For one channel condition (TX power setting) this measures 1 MB goodput of
the AUTO rate adapter and of EVERY FIXED MCS, so "does auto find the optimum"
is answered by direct comparison, not by trust. Restores auto + power after.

Usage: python exp_ra_sweep.py [--mb 1] [--powers 1,5,9,15]
"""
import argparse
import json
import re
import subprocess
import sys
import time

from hwtest import Node

A_IP = '192.168.1.43'          # sender (its power shapes the DATA direction)
B_IP = '192.168.1.42'


def cfg(node, power=None, mcs=None):
    body = {}
    if power is not None:
        body['power_dbm'] = power
    if mcs is not None:
        body['mcs_index'] = 'MCS%d' % mcs
    node.post('halow_cfg', body)


def ra(node, mode):
    node.post('ack_cfg', {'rate_adapt': mode})


def set_mode(a, b, mcs=None):
    """mcs=None -> AUTO everywhere; else fixed MCS on both ends."""
    if mcs is None:
        ra(a, 1)
        ra(b, 1)
    else:
        ra(a, 0)
        ra(b, 0)
        time.sleep(0.3)
        cfg(a, mcs=mcs)
        cfg(b, mcs=mcs)


def run_transfer(mb):
    p = subprocess.run(
        [sys.executable, 'rns_throughput.py',
         '--a', A_IP, '--b', B_IP, '--mb', str(mb)],
        capture_output=True, text=True, timeout=900)
    out = p.stdout
    m = re.search(r'A->B: (\d+)/(\d+) delivered, dup=(\d+) \([\d.]+%\), '
                  r'rx_total=\d+, [\d.]+ MB in ([\d.]+)s \((\d+) kbit/s', out)
    drops = re.search(r'drop deltas: (\{[^}]*\})', out)
    row = {
        'rc': p.returncode,
        'delivered': int(m.group(1)) if m else -1,
        'sent': int(m.group(2)) if m else -1,
        'dup': int(m.group(3)) if m else -1,
        'sec': float(m.group(4)) if m else -1,
        'kbps': int(m.group(5)) if m else 0,
        'drops': drops.group(1) if drops else '?',
    }
    return row


def wait_drained(a, b, timeout=30.0):
    """Let the previous case fully drain: pending ACK slots must hit zero on
    both nodes, else leftovers jam the next case's handshake."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        oa = a.ack().get('outstanding', 0)
        ob = b.ack().get('outstanding', 0)
        if oa == 0 and ob == 0:
            time.sleep(3.0)
            return True
        time.sleep(1.0)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mb', type=float, default=1.0)
    ap.add_argument('--powers', default='1')
    ap.add_argument('--auto-only', action='store_true')
    args = ap.parse_args()

    a, b = Node(A_IP, 'A'), Node(B_IP, 'B')
    powers = [int(x) for x in args.powers.split(',')]

    rows = []
    try:
        for pw in powers:
            cfg(a, power=pw)
            wait_drained(a, b)

            set_mode(a, b, None)
            time.sleep(1.0)
            r = run_transfer(args.mb)
            rows.append(('P%d AUTO' % pw, r))
            print('%-12s %s' % ('P%d AUTO' % pw, json.dumps(r)), flush=True)
            wait_drained(a, b)
            if args.auto_only:
                continue

            for mcs in range(0, 8):
                set_mode(a, b, mcs)
                time.sleep(0.5)
                r = run_transfer(args.mb)
                rows.append(('P%d MCS%d' % (pw, mcs), r))
                print('%-12s %s' % ('P%d MCS%d' % (pw, mcs), json.dumps(r)),
                      flush=True)
                wait_drained(a, b)
    finally:
        set_mode(a, b, None)
        # mcs_index must go back to the baseline too: the fixed-MCS cases
        # rewrite it, and a leftover MCS7 config makes every later handshake
        # ride an undecodable rate.
        cfg(a, power=powers[0], mcs=0)
        cfg(b, power=None, mcs=0)

    print('\n%-12s %8s %7s %6s %8s  %s'
          % ('case', 'deliv', 'kbps', 'dup', 'sec', 'drops'))
    for name, r in rows:
        print('%-12s %8s %7s %6s %8s  %s'
              % (name,
                 '%d/%d' % (r['delivered'], r['sent']) if r['sent'] > 0 else 'FAIL',
                 r['kbps'], r['dup'], r['sec'], r['drops']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
