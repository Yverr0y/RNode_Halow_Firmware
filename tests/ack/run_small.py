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
import tempfile
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
    found = []
    for prefix in prefixes:
        for sub in ('ucrt64', 'mingw64', 'clang64', 'mingw32'):
            d = os.path.join(prefix, sub, 'bin')
            make, gcc = has_make_gcc(d)
            if make and gcc:
                found.append(d)
    return found


def gcc_works(gcc):
    """A toolchain can be present but broken (AV-killed cc1 dies with rc=1
    and no output -- seen live with ucrt64 gcc 15.2). Verify with a trivial
    compile instead of trusting existence."""
    try:
        with tempfile.TemporaryDirectory(prefix='gccprobe-') as td:
            src = os.path.join(td, 'p.c')
            out = os.path.join(td, 'p.o')
            with open(src, 'w') as f:
                f.write('int main(void){return 0;}\n')
            env = dict(os.environ)
            env['TMP'] = td
            env['TEMP'] = td
            r = subprocess.run([gcc, '-c', src, '-o', out], capture_output=True,
                               timeout=60, env=env)
            return r.returncode == 0 and os.path.isfile(out)
    except Exception:
        return False


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

    def accept(bindir, make, gcc):
        """Prepend a verified toolchain dir to PATH; returns (make, gcc)."""
        extra.append(bindir)
        return make, gcc

    make, gcc = has_make_gcc()
    if make and gcc and gcc_works(gcc):
        pass                                    # already resolvable and alive
    else:
        override = os.environ.get('MSYS2_HOST_BIN')
        if override and os.path.isdir(override):
            m, g = has_make_gcc(override)
            if m and g and gcc_works(g):
                make, gcc = accept(override, m, g)
        if not (make and gcc and gcc_works(gcc)):
            for d in probe_msys2():
                m, g = has_make_gcc(d)
                if m and g and gcc_works(g):
                    make, gcc = accept(d, m, g)
                    break

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

    # EIDE task environments may lack a usable TMP/TEMP (or point somewhere
    # unwritable): cc1 then dies silently mid-compile ("Error 1" with no
    # diagnostics). Always hand the build a guaranteed-writable temp dir.
    try:
        tmpdir = tempfile.mkdtemp(prefix='smalltests-')
        env['TMP'] = tmpdir
        env['TEMP'] = tmpdir
    except OSError:
        # every standard temp candidate failed: fall back to a local dir
        try:
            fallback = os.path.join(HERE, 'build_tmp')
            os.makedirs(fallback, exist_ok=True)
            probe = os.path.join(fallback, '.w')
            with open(probe, 'w') as f:
                f.write('x')
            os.remove(probe)
            env['TMP'] = fallback
            env['TEMP'] = fallback
        except OSError:
            pass

    print('[small-tests] running host suite via %s' % make)

    def diagnose(tag):
        """Compile a trivial file with the SAME gcc in the SAME environment:
        whatever kills the real build (missing DLL, blocked cc1, dead TMP)
        shows its actual error message here instead of dying silently."""
        gcc = shutil.which('gcc', path=env.get('PATH', ''))
        print('[small-tests] %s diagnostics:' % tag)
        print('  TMP=%r TEMP=%r' % (env.get('TMP'), env.get('TEMP')))
        print('  gcc=%r' % gcc)
        if gcc is None:
            return
        src = os.path.join(HERE, 'probe_gate.c')
        try:
            with open(src, 'w') as f:
                f.write('int probe_gate(void){return 0;}\n')
            r = subprocess.run([gcc, '-c', src, '-o', src + '.o'],
                               capture_output=True, text=True, env=env,
                               timeout=60)
            print('  probe rc=%d' % r.returncode)
            for name in ('stdout', 'stderr'):
                txt = (r.stdout if name == 'stdout' else r.stderr) or ''
                txt = txt.strip()
                if txt:
                    print('  %s: %s' % (name, txt[:600]))
        finally:
            for p in (src, src + '.o'):
                try:
                    os.remove(p)
                except OSError:
                    pass

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
        diagnose('first failure')
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
