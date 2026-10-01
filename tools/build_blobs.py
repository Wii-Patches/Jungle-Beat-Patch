#!/usr/bin/env python3
"""Assemble/compile the GameCube-pad half of the patch for each region and
write tools/prebuilt/blobs.json. Needs devkitPPC; end users never do, the
patcher just reads the JSON. Run after touching anything in src/.

The code is position independent (linked at 0, every reference relative), so
the same words serve the DOL patch and the Riivolution patch."""
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from regions import REGIONS  # noqa: E402

SRC = os.path.join(HERE, '..', 'src')
OUT = os.path.join(HERE, 'prebuilt', 'blobs.json')
BIN = os.environ.get('DEVKITPPC', '/opt/devkitpro/devkitPPC') + '/bin/powerpc-eabi-'
SOURCES = ('poller.s', 'wrappers.s', 'gc_feed.c')


def hi_lo(v):
    return v >> 16, v & 0xFFFF


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
                       ('SI_BUSY', r.si_state)):
        hi, lo = hi_lo(addr)
        subs[name + '_HI'], subs[name + '_LO'] = '0x%04X' % hi, '0x%04X' % lo
    shadow = r.si_state + 4                         # lwz/stw take a signed low half
    lo = shadow & 0xFFFF
    subs['SI_SHADOW_HA'] = '0x%04X' % ((((shadow >> 16) + (1 if lo & 0x8000 else 0))) & 0xFFFF)
    subs['SI_SHADOW_LO'] = str(lo - 0x10000 if lo & 0x8000 else lo)
    poller = os.path.join(tmp, 'poller.s')
    open(poller, 'w').write(open(os.path.join(SRC, 'poller.s')).read().format(**subs))
    objs = {}
    for name, src in (('poller', poller), ('wrappers', os.path.join(SRC, 'wrappers.s'))):
        o = os.path.join(tmp, name + '.o')
        run(BIN + 'as', '-mbig', '-mgekko', '-o', o, src)
        objs[name] = o
    o = os.path.join(tmp, 'gc_feed.o')
    run(BIN + 'gcc', '-O2', '-mcpu=750', '-meabi', '-msoft-float', '-mno-sdata',
        '-ffreestanding', '-fno-builtin', '-fno-common', '-fno-asynchronous-unwind-tables',
        '-fno-exceptions', '-DSI_TYPE=0x%08X' % (r.si_state + 0x18),
        '-DBUBBLE_PTR=0x%08X' % r.bubble_ptr, '-DWPAD_TBL=0x%08X' % r.wpad_tbl,
        '-DCONNECT_CB=0x%08X' % r.connect_cb, '-DKPAD_BASE=0x%08X' % r.kpad_base, '-c', os.path.join(SRC, 'gc_feed.c'), '-o', o)
    objs['feed'] = o

    elf, binf = os.path.join(tmp, 'gc.elf'), os.path.join(tmp, 'gc.bin')
    run(BIN + 'ld', '-Ttext=0', '-e', 'poller_entry', '-o', elf,
        objs['wrappers'], objs['poller'], objs['feed'])
    run(BIN + 'objcopy', '-O', 'binary', '-j', '.text', elf, binf)
    data = open(binf, 'rb').read()
    reloc = subprocess.run([BIN + 'objdump', '-r', objs['feed']], capture_output=True,
                           text=True, check=True).stdout
    if 'R_PPC' in reloc:
        raise AssertionError('gc_feed.c must build without relocations:\n' + reloc)
    syms = {}
    for line in subprocess.run([BIN + 'nm', elf], capture_output=True, text=True,
                               check=True).stdout.splitlines():
        value, _, name = line.split()
        syms[name] = int(value, 16)
    need = ('poller_entry', 'poller_orig', 'poller_hi', 'poller_lo', 'feeder_entry',
            'feeder_orig', 'feeder_hi', 'feeder_lo', 'probe_entry', 'probe_orig', 'probe_hi',
            'probe_lo', 'scratch')
    return {
        'words': ['%08X' % w for w in struct.unpack('>%dI' % (len(data) // 4), data)],
        'symbols': {n: syms[n] for n in need},
    }


def main():
    out = {'sha256': digest(), 'regions': {}}
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
