#!/usr/bin/env python3
"""Large tier: scenarios against real nodes running the debug firmware.

Usage:
    python run.py --a 192.168.1.42 --b 192.168.1.43 [scenarios...]

The two nodes must already run the debug build (with /api counters) and be
RF-linked to each other. Nothing here flashes, reboots or reconfigures: the
scenarios read counters, inject RNS packets over TCP and check deltas.
"""
import argparse
import sys
import time

from hwtest import Node
from scenarios import SCENARIOS


def wait_drained(a, b, timeout=10.0):
    """Let the PREVIOUS scenario fully drain: pending ACK slots must hit zero
    or the next handshake queues behind a saturated window and misses its
    collect deadline."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            oa = a.ack().get('outstanding', 0)
            ob = b.ack().get('outstanding', 0)
        except Exception:
            return
        if oa == 0 and ob == 0:
            time.sleep(1.5)
            return
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--a', required=True, help='node A ip (transmitter side)')
    ap.add_argument('--b', required=True, help='node B ip (receiver side)')
    ap.add_argument('only', nargs='*', help='scenario names (default: all)')
    args = ap.parse_args()

    a = Node(args.a, 'A')
    b = Node(args.b, 'B')

    passed, failed = 0, 0

    def chk(cond, note=''):
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print('    FAIL: %s' % note)

    def log(msg):
        print('    ' + msg)

    names = args.only or [n for n, _ in SCENARIOS]
    print('hardware tests: %d scenario(s), A=%s B=%s' % (len(names), a.ip, b.ip))
    for name, fn in SCENARIOS:
        if name not in names:
            continue
        wait_drained(a, b)
        p0, f0 = passed, failed
        print('  %-34s' % name, end='')
        t0 = time.time()
        try:
            fn(a, b, chk, log)
        except Exception as e:              # noqa: BLE001 - report and continue
            failed += 1
            print('\n    FAIL: exception: %r' % (e,))
        dt = time.time() - t0
        if passed == p0 and failed == f0:
            print('[no checks] %.1fs' % dt)
        elif failed == f0:
            print('ok %.1fs' % dt)
        else:
            print('FAIL %.1fs' % dt)

    print('\n%d checks passed, %d failed' % (passed, failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
