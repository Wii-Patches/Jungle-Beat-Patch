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
from regions import KPAD_READ_PREIMAGE, REGIONS, TEXT_ADDRESS, TEXT_LIMIT

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
    entry = json.load(open(BLOBS))['regions'][region.disc_id]
    return [int(w, 16) for w in entry['words']], entry['symbols']


def lis_ori(value):
    """`lis r0,hi` / `ori r0,r0,lo` words that load `value` into r0."""
    return 0x3C000000 | value >> 16, 0x60000000 | value & 0xFFFF


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


class Patch:
    """Everything the patch puts in memory: a block of code and data at `base`,
    the instructions that now branch into it, and plain word writes."""

    def __init__(self, base, blob, sites, writes):
        self.base, self.blob, self.sites, self.writes = base, bytes(blob), sites, writes

    def site_words(self):
        return {site: branch(site, target) for site, target in self.sites.items()}


def make_patch(region, classic=True, gamecube=True, base=TEXT_ADDRESS):
    """Build the patch for `region` to be placed at `base`."""
    if not (classic or gamecube):
        raise ValueError('nothing to do: enable Classic Controller and/or GameCube support')
    (waddr, wvalue), gecko = load_gecko(region)
    dpd_site = gecko['dpd'][0]
    blob = bytearray()
    sites, writes = {}, []

    gc_words, sym = load_blob(region) if gamecube else ([], {})
    gc_words = list(gc_words)
    gc_size = len(gc_words) * 4

    # Classic Controller bodies follow the GameCube half
    gecko_at = {}
    if classic:
        at = base + gc_size
        for name in GECKO_HOOKS:
            site, body = gecko[name]
            body = list(body)
            if body[-1] != 0:
                raise AssertionError(f'{name}: C2 body does not end with its branch slot')
            body[-1] = branch(at + 4 * (len(body) - 1), site + 4)
            gecko_at[name] = (at, body)
            at += 4 * len(body)
        writes.append((waddr, wvalue))

    if gamecube:
        def idx(name):
            return sym[name] // 4
        gc_words[idx('poller_orig')] = KPAD_READ_PREIMAGE
        gc_words[idx('poller_hi')], gc_words[idx('poller_lo')] = lis_ori(region.kpad_read + 4)
        sites[region.kpad_read] = base + sym['poller_entry']
        if classic:
            gc_words[idx('feeder_orig')] = NOP
            target = gecko_at['dpd'][0]
        else:
            gc_words[idx('feeder_orig')] = GECKO_PREIMAGES[GECKO_HOOKS.index('dpd')]
            target = dpd_site + 4
        gc_words[idx('feeder_hi')], gc_words[idx('feeder_lo')] = lis_ori(target)
        sites[dpd_site] = base + sym['feeder_entry']
        blob.extend(struct.pack('>%dI' % len(gc_words), *gc_words))
    if classic:
        for name in GECKO_HOOKS:
            at, body = gecko_at[name]
            blob.extend(struct.pack('>%dI' % len(body), *body))
            if not (name == 'dpd' and gamecube):
                sites[gecko[name][0]] = at
    return Patch(base, blob, sites, writes)


def inject(src, dst, disc_id=None, classic=True, gamecube=True):
    """Patch `src` into `dst`. Returns (text section index, {site: target}, size, region)."""
    dol = Dol(src)
    region = detect_region(dol, disc_id)
    patch = make_patch(region, classic, gamecube)
    if patch.base + len(patch.blob) > TEXT_LIMIT:
        raise AssertionError(f'injected section ends at 0x{patch.base + len(patch.blob):08X}, past '
                             f'0x{TEXT_LIMIT:08X} (OS low-memory globals)')
    section = dol.add_text_section(patch.base, patch.blob)
    for addr, value in patch.writes:
        dol.write(addr, struct.pack('>I', value))
    for site, value in patch.site_words().items():
        dol.write(site, struct.pack('>I', value))
    dol.save(dst)
    return section, patch.sites, len(patch.blob), region


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
          f'0x{TEXT_ADDRESS:08X} ({size} bytes)')
    for site, target in sorted(sites.items()):
        print(f'  0x{site:08X} -> 0x{target:08X}')
