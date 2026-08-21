#!/usr/bin/env python3
"""Small (host) test gate for EIDE beforeBuildTasks and the root Makefile.

Machine-independent: no hardcoded toolchain locations. Resolution order:
  1. MSYS2_HOST_BIN env var (explicit override, any machine)
  2. make + gcc already resolvable from the current PATH (Linux/mac/CI,
     or Windows with the toolchain configured)
  3. MSYS2 default install roots probed on every local drive
     (<drive>:/msys64[/mingw32|64]/ucrt64|mingw64/bin)
Unix tools (rm, mkdir) for the Makefile recipes come along with the MSYS2
install; additionally, if `git` is on PATH, its usr/bin is prepended
(Git-for-Windows layout). Non-zero exit aborts the firmware build
(stopBuildAfterFailed: true in eide.yml).
"""
import os
import time
import shutil
import string
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def has_make_gcc(path=None):
    make = shutil.which('mingw32-make', path=path) or shutil.which('make', path=path)
    gcc = shutil.which('gcc', path=path)
    return make, gcc

def probe_msys2():
    """Find a make+gcc pair in conventional MSYS2 install roots on any drive."""
    prefixes = []
    for letter in string.ascii_uppercase:
        for root in ('msys64', 'msys32'):
            prefixes.append('%s:\\%s' % (letter, root))
    for prefix in prefixes:
        for sub in ('ucrt64', 'mingw64', 'clang64', 'mingw32'):
            d = os.path.join(prefix, sub, 'bin')
            make, gcc = has_make_gcc(d)
            if make and gcc:
                return d
    return None

def git_usr_bin():
    """Git-for-Windows ships unix coreutils in <install>/usr/bin."""
    git = shutil.which('git')
    if not git:
        return None
    cand = os.path.join(os.path.dirname(os.path.dirname(git)), 'usr', 'bin')
    return cand if os.path.isdir(cand) else None

def main():
    env = dict(os.environ)
    extra = []

    make, gcc = has_make_gcc()
    if not (make and gcc):
        override = os.environ.get('MSYS2_HOST_BIN')
        if override and os.path.isdir(override):
            m, g = has_make_gcc(override)
            if m and g:
                extra.append(override)
                make = m
        if not make:
            found = probe_msys2()
            if found:
                extra.append(found)
                make = shutil.which('mingw32-make', path=found) or \
                       shutil.which('make', path=found)

    gub = git_usr_bin()
    if gub:
        extra.append(gub)

    if extra:
        env['PATH'] = os.pathsep.join(extra) + os.pathsep + env.get('PATH', '')
        if not make:
            make = shutil.which('mingw32-make', path=env['PATH']) or \
                   shutil.which('make', path=env['PATH'])

    if make is None:
        print('[small-tests] ERROR: host make/gcc toolchain not found.\n'
              '  Fix: install MSYS2 (ucrt64) or put gcc + make on PATH,\n'
              '  or set MSYS2_HOST_BIN to the toolchain bin directory.')
        return 1

    print('[small-tests] running host suite via %s' % make)

    def run_make(*args):
        proc = subprocess.Popen([make] + list(args), cwd=HERE, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors='replace')
        lines = []
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)
        return proc.wait(), lines

    rc, lines = run_make('run')
    if rc != 0:
        # incremental-build artifacts can be locked by AV scanners or stale
        # processes on Windows right after a big build; pause, then one clean
        # retry separates that from a real regression before we abort
        print('[small-tests] run failed (rc=%d) - tail of the build log:' % rc)
        sys.stdout.writelines(lines[-30:])
        print('[small-tests] one clean rebuild attempt in 3 s ...')
        time.sleep(3)
        subprocess.call([make, 'clean'], cwd=HERE, env=env)
        rc, lines = run_make('run')
    if rc != 0:
        print('[small-tests] FAILED (rc=%d) - tail of the build log:' % rc)
        sys.stdout.writelines(lines[-30:])
        print('[small-tests] firmware build aborted')
    return rc

if __name__ == '__main__':
    sys.exit(main())
