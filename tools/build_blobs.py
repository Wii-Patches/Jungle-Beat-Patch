#!/usr/bin/env python3
"""Assemble/compile the GameCube-pad half of the patch for each region and
write tools/prebuilt/blobs.json. Needs devkitPPC; end users never do, the
patcher just reads the JSON. Run after touching anything in src/."""
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from regions import REGIONS, SCRATCH, SCRATCH_BYTES  # noqa: E402

SRC = os.path.join(HERE, '..', 'src')
OUT = os.path.join(HERE, 'prebuilt', 'blobs.json')
BIN = os.environ.get('DEVKITPPC', '/opt/devkitpro/devkitPPC') + '/bin/powerpc-eabi-'
GC_BASE = SCRATCH + SCRATCH_BYTES
SOURCES = ('poller.s', 'wrappers.s', 'gc_feed.c')

# scratch layout (mirrored in gc_feed.c)
SCR_PROBE, SCR_NOREP, SCR_CHAN, SCR_SEQ, SCR_WD = 0x00, 0x14, 0x18, 0x1C, 0x20


def hi_lo(v):
    return v >> 16, v & 0xFFFF


def ha_lo(v):
    lo = v & 0xFFFF
    return ((v >> 16) + (1 if lo & 0x8000 else 0)) & 0xFFFF, lo - 0x10000 if lo & 0x8000 else lo


def run(*cmd):
    subprocess.run(cmd, check=True)


def digest():
    h = hashlib.sha256()
    for name in SOURCES + ('build_blobs.py', 'regions.py'):
        base = HERE if name.endswith(('build_blobs.py', 'regions.py')) else SRC
        h.update(open(os.path.join(base, name), 'rb').read().replace(b'\r\n', b'\n'))
    return h.hexdigest()


def build(region, tmp):
    r = region
    subs = {}
    for name, addr in (('SI_TYPE', r.si_state + 0x18), ('SIGETTYPE', r.si_gettype),
                       ('OSDISABLE', r.os_disable), ('OSRESTORE', r.os_restore),
                       ('SI_BUSY', r.si_state), ('SCR_PROBE', SCRATCH + SCR_PROBE),
                       ('SCR_NOREP', SCRATCH + SCR_NOREP), ('SCR_WD', SCRATCH + SCR_WD)):
        hi, lo = hi_lo(addr)
        subs[name + '_HI'], subs[name + '_LO'] = '0x%04X' % hi, '0x%04X' % lo
    ha, lo = ha_lo(r.si_state + 4)
    subs['SI_SHADOW_HA'], subs['SI_SHADOW_LO'] = '0x%04X' % ha, str(lo)
    ha, lo = ha_lo(SCRATCH + SCR_CHAN)       # chan and seq share one 'lis'
    subs['SCR_CHAN_HA'], subs['SCR_CHAN_LO'] = '0x%04X' % ha, str(lo)
    subs['SCR_SEQ_LO'] = str(lo + (SCR_SEQ - SCR_CHAN))
    poller = os.path.join(tmp, 'poller.s')
    open(poller, 'w').write(open(os.path.join(SRC, 'poller.s')).read().format(**subs))
    objs = []
    for src in (poller, os.path.join(SRC, 'wrappers.s')):
        o = os.path.join(tmp, os.path.basename(src) + '.o')
        run(BIN + 'as', '-mbig', '-mgekko', '-o', o, src)
        objs.append(o)
    o = os.path.join(tmp, 'gc_feed.o')
    run(BIN + 'gcc', '-O2', '-mcpu=750', '-meabi', '-msoft-float', '-mno-sdata',
        '-ffreestanding', '-fno-builtin', '-fno-common', '-fno-asynchronous-unwind-tables',
        '-fno-exceptions', '-DSCRATCH=0x%08X' % SCRATCH, '-DSI_TYPE=0x%08X' % (r.si_state + 0x18),
        '-DBUBBLE_PTR=0x%08X' % r.bubble_ptr, '-c', os.path.join(SRC, 'gc_feed.c'), '-o', o)
    objs.append(o)
    elf, binf = os.path.join(tmp, 'gc.elf'), os.path.join(tmp, 'gc.bin')
    # wrappers first so poller_entry sits at GC_BASE
    run(BIN + 'ld', '-Ttext=0x%08X' % GC_BASE, '-e', 'poller_entry', '-o', elf,
        objs[1], objs[0], objs[2])
    run(BIN + 'objcopy', '-O', 'binary', '-j', '.text', elf, binf)
    data = open(binf, 'rb').read()
    syms = {}
    for line in subprocess.run([BIN + 'nm', elf], capture_output=True, text=True,
                               check=True).stdout.splitlines():
        value, _, name = line.split()
        syms[name] = int(value, 16)
    need = ('poller_entry', 'poller_orig', 'poller_ret', 'feeder_entry', 'feeder_orig',
            'feeder_ret')
    return {
        'words': ['%08X' % w for w in struct.unpack('>%dI' % (len(data) // 4), data)],
        'symbols': {n: syms[n] for n in need},
    }


def main():
    out = {'sha256': digest(), 'base': GC_BASE, 'regions': {}}
    with tempfile.TemporaryDirectory() as tmp:
        for disc_id, region in REGIONS.items():
            out['regions'][disc_id] = build(region, tmp)
            print(disc_id, len(out['regions'][disc_id]['words']) * 4, 'bytes')
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as f:
        json.dump(out, f, indent=0)
        f.write('\n')


if __name__ == '__main__':
    main()
