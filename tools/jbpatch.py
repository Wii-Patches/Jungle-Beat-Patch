#!/usr/bin/env python3
"""Patch a clean Donkey Kong Jungle Beat main.dol (USA R49E01, Europe R49P01
or Japan R49J01) with Classic Controller and/or GameCube controller + DK
Bongos support.

Everything is injected as one extra DOL text section at 0x80001820 and
reached through direct branches, so no Gecko code handler is needed:

  Classic Controller  Vague Rant's / crediar's six C2 hooks and one 04 write
                      (codes/<disc id>.ini), applied as branches to the
                      same bodies.
  GameCube / Bongos   an SI poller hooked into KPADReadEx and a feeder hooked
                      into the KPAD library's pointer routine that rewrites
                      each Wii Remote sample as Wii Remote + Nunchuk (src/).
"""
import json
import os
import struct
import sys

from dol import Dol
from gecko import parse
from regions import KPAD_READ_PREIMAGE, REGIONS, SCRATCH, SCRATCH_BYTES, TEXT_LIMIT

if getattr(sys, 'frozen', False):
    HERE = os.path.join(sys._MEIPASS, 'tools')
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
CODES = os.path.join(HERE, '..', 'codes')
BLOBS = os.path.join(HERE, 'prebuilt', 'blobs.json')

# Instruction each Gecko hook replaces, in the order they appear in the .ini.
GECKO_HOOKS = ('nunchuk_check', 'nunchuk_acc', 'nunchuk_stick', 'acc', 'dpd', 'buttons')
GECKO_PREIMAGES = (0x28000001, 0xD001001C, 0xEC261828, 0x9421FFB0, 0x9421FFC0, 0x90030068)
GECKO_WRITE_PREIMAGE = 0x3BE00000       # li r31,0  (04 code: skip the "insert a Nunchuk" error)
NOP = 0x60000000


def word(dol, address):
    raw = dol.read(address, 4)
    return None if raw is None else struct.unpack('>I', raw)[0]


def branch(src, dst, link=False):
    off = dst - src
    if off & 3 or not -0x2000000 <= off < 0x2000000:
        raise ValueError(f'branch 0x{src:08X} -> 0x{dst:08X} is out of range/alignment')
    return 0x48000000 | (off & 0x03FFFFFC) | (1 if link else 0)


def load_gecko(region):
    writes, hooks = parse(os.path.join(CODES, region.gecko))
    if len(writes) != 1 or len(hooks) != len(GECKO_HOOKS):
        raise AssertionError(f'{region.gecko}: expected 1 write and {len(GECKO_HOOKS)} C2 codes')
    return writes[0], dict(zip(GECKO_HOOKS, hooks.items()))


def load_blob(region):
    data = json.load(open(BLOBS))
    entry = data['regions'][region.disc_id]
    return ([int(w, 16) for w in entry['words']], entry['symbols'], data['base'])


def detect_region(dol, disc_id=None):
    """The region whose hook sites in `dol` all hold the retail instructions."""
    candidates = [REGIONS[disc_id]] if disc_id else list(REGIONS.values())
    problems = []
    for region in candidates:
        (waddr, _), hooks = load_gecko(region)
        checks = [(waddr, GECKO_WRITE_PREIMAGE), (region.kpad_read, KPAD_READ_PREIMAGE)]
        checks += [(hooks[n][0], p) for n, p in zip(GECKO_HOOKS, GECKO_PREIMAGES)]
        bad = [(a, word(dol, a), want) for a, want in checks if word(dol, a) != want]
        if not bad:
            return region
        a, got, want = bad[0]
        problems.append(f'{region}: 0x{a:08X} holds '
                        + ('nothing' if got is None else f'0x{got:08X}')
                        + f', expected 0x{want:08X}')
    raise AssertionError('not a clean retail ' + '/'.join(r.disc_id for r in candidates)
                         + ' main.dol (other revision, or already patched): '
                         + '; '.join(problems))


def inject(src, dst, disc_id=None, classic=True, gamecube=True):
    """Patch `src` into `dst`. Returns (text section index, {site: target}, size, region)."""
    if not (classic or gamecube):
        raise ValueError('nothing to do: enable Classic Controller and/or GameCube support')
    dol = Dol(src)
    region = detect_region(dol, disc_id)
    (waddr, wvalue), gecko = load_gecko(region)

    blob = bytearray(SCRATCH_BYTES)
    sites = {}          # hook site -> where it now branches

    def here():
        return SCRATCH + len(blob)

    def emit(words):
        at = here()
        blob.extend(struct.pack('>%dI' % len(words), *words))
        return at

    dpd_site = gecko['dpd'][0]
    dpd_gecko_at = None
    gecko_blocks = []
    if classic:
        # place the Gecko bodies after the GC half so its size is known first
        pass

    gc_words = None
    if gamecube:
        gc_words, sym, base = load_blob(region)
        assert base == SCRATCH + SCRATCH_BYTES
        gc_words = list(gc_words)
        at = emit(gc_words)
        assert at == base

    # Classic Controller bodies
    gecko_at = {}
    if classic:
        for name in GECKO_HOOKS:
            site, body = gecko[name]
            body = list(body)
            if body[-1] != 0:
                raise AssertionError(f'{name}: C2 body does not end with its branch slot')
            location = here()
            body[-1] = branch(location + 4 * (len(body) - 1), site + 4)
            gecko_at[name] = emit(body)

    # wire up
    if gamecube:
        def idx(name):
            return (sym[name] - base) // 4
        # KPADReadEx entry -> poller
        gc_words[idx('poller_orig')] = KPAD_READ_PREIMAGE
        gc_words[idx('poller_ret')] = branch(sym['poller_ret'], region.kpad_read + 4)
        sites[region.kpad_read] = sym['poller_entry']
        # pointer routine -> feeder (-> Classic Controller body -> game)
        if classic:
            gc_words[idx('feeder_orig')] = NOP
            gc_words[idx('feeder_ret')] = branch(sym['feeder_ret'], gecko_at['dpd'])
        else:
            gc_words[idx('feeder_orig')] = GECKO_PREIMAGES[GECKO_HOOKS.index('dpd')]
            gc_words[idx('feeder_ret')] = branch(sym['feeder_ret'], dpd_site + 4)
        sites[dpd_site] = sym['feeder_entry']
        blob[SCRATCH_BYTES:SCRATCH_BYTES + 4 * len(gc_words)] = struct.pack(
            '>%dI' % len(gc_words), *gc_words)
    if classic:
        for name in GECKO_HOOKS:
            site = gecko[name][0]
            if name == 'dpd' and gamecube:
                continue
            sites[site] = gecko_at[name]
        dol.write(waddr, struct.pack('>I', wvalue))

    if SCRATCH + len(blob) > TEXT_LIMIT:
        raise AssertionError(f'injected section ends at 0x{SCRATCH + len(blob):08X}, past '
                             f'0x{TEXT_LIMIT:08X} (OS low-memory globals)')
    section = dol.add_text_section(SCRATCH, bytes(blob))
    for site, target in sites.items():
        dol.write(site, struct.pack('>I', branch(site, target)))
    dol.save(dst)
    return section, sites, len(blob), region


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--no-classic', action='store_true', help='skip Classic Controller support')
    ap.add_argument('--no-gamecube', action='store_true',
                    help='skip GameCube controller / DK Bongos support')
    ap.add_argument('--region', choices=sorted(REGIONS),
                    help='refuse a DOL from any other region (default: detect)')
    args = ap.parse_args()
    section, sites, size, region = inject(args.src, args.dst, args.region,
                                          not args.no_classic, not args.no_gamecube)
    print(f'{region}: injected {len(sites)} hooks into text section {section} at '
          f'0x{SCRATCH:08X} ({size} bytes)')
    for site, target in sorted(sites.items()):
        print(f'  0x{site:08X} -> 0x{target:08X}')
