"""Tests that need no game files: they build a skeleton DOL that holds only the
retail instructions the patcher checks, and patch that."""
import json
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))

import build_blobs                      # noqa: E402
import gecko                            # noqa: E402
import make_riivolution                 # noqa: E402
import jbpatch                          # noqa: E402
from dol import Dol                     # noqa: E402
from regions import REGIONS, TEXT_ADDRESS as SCRATCH, TEXT_LIMIT  # noqa: E402

TEXT_START, TEXT_END = 0x80004000, 0x80360000


def skeleton(region):
    """A DOL with one big text section and the retail words at every hook site."""
    size = TEXT_END - TEXT_START
    text = bytearray(size)
    (waddr, _), hooks = jbpatch.load_gecko(region)
    sites = [(waddr, jbpatch.GECKO_WRITE_PREIMAGE), (region.kpad_read, jbpatch.KPAD_READ_PREIMAGE),
             (region.wpad_probe, jbpatch.WPAD_PROBE_PREIMAGE)]
    sites += [(hooks[n][0], p) for n, p in zip(jbpatch.GECKO_HOOKS, jbpatch.GECKO_PREIMAGES)]
    for addr, word in sites:
        struct.pack_into('>I', text, addr - TEXT_START, word)
    header = bytearray(0x100)
    struct.pack_into('>I', header, 0x00, 0x100)
    struct.pack_into('>I', header, 0x48, TEXT_START)
    struct.pack_into('>I', header, 0x90, size)
    struct.pack_into('>I', header, 0xE0, TEXT_START)
    return bytes(header) + bytes(text)


class PatchTests(unittest.TestCase):
    def patch(self, region, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, 'in.dol'), os.path.join(tmp, 'out.dol')
            open(src, 'wb').write(skeleton(region))
            result = jbpatch.inject(src, dst, disc_id=region.disc_id, **kw)
            return result, Dol(dst), Dol(src)

    def test_blobs_match_sources(self):
        data = json.load(open(jbpatch.BLOBS))
        self.assertEqual(data['sha256'], build_blobs.digest(),
                         'tools/prebuilt/blobs.json is stale: run tools/build_blobs.py')

    def test_every_region_and_option_set(self):
        for region in REGIONS.values():
            for classic, gamecube, hooks in ((True, True, 8), (True, False, 6), (False, True, 3)):
                with self.subTest(region=region.disc_id, classic=classic, gamecube=gamecube):
                    (section, sites, size, found), out, _ = self.patch(
                        region, classic=classic, gamecube=gamecube)
                    self.assertIs(found, region)
                    self.assertEqual(len(sites), hooks)
                    self.assertLessEqual(SCRATCH + size, TEXT_LIMIT)
                    self.assertEqual(out.addr[section], SCRATCH)
                    for site, target in sites.items():
                        word = struct.unpack('>I', out.read(site, 4))[0]
                        self.assertEqual(word >> 26, 18)            # b
                        off = word & 0x03FFFFFC
                        off -= 0x4000000 if off & 0x2000000 else 0
                        self.assertEqual(site + off, target)
                        self.assertTrue(SCRATCH <= target < SCRATCH + size)

    def test_gecko_bodies_return_to_the_game(self):
        region = REGIONS['R49E01']
        (_, sites, size, _), out, _ = self.patch(region, classic=True, gamecube=False)
        (_, _), hooks = jbpatch.load_gecko(region)
        for name, (site, body) in hooks.items():
            at = sites[site]
            last = struct.unpack('>I', out.read(at + 4 * (len(body) - 1), 4))[0]
            off = last & 0x03FFFFFC
            off -= 0x4000000 if off & 0x2000000 else 0
            self.assertEqual(at + 4 * (len(body) - 1) + off, site + 4, name)

    def test_refuses_patched_or_foreign_dols(self):
        region = REGIONS['R49E01']
        with tempfile.TemporaryDirectory() as tmp:
            src, mid, dst = (os.path.join(tmp, n) for n in ('a.dol', 'b.dol', 'c.dol'))
            open(src, 'wb').write(skeleton(region))
            jbpatch.inject(src, mid, disc_id='R49E01')
            with self.assertRaises(AssertionError):
                jbpatch.inject(mid, dst, disc_id='R49E01')          # already patched
            with self.assertRaises(AssertionError):
                jbpatch.inject(src, dst, disc_id='R49P01')          # wrong region
            with self.assertRaises(ValueError):
                jbpatch.inject(src, dst, classic=False, gamecube=False)

    def test_riivolution_files_are_current(self):
        sd = os.path.join(HERE, '..', 'riivolution', 'sd')
        with tempfile.TemporaryDirectory() as tmp:
            for region in REGIONS.values():
                make_riivolution.write_region(region, tmp)
            for root, _, files in os.walk(tmp):
                for name in files:
                    fresh = os.path.join(root, name)
                    shipped = os.path.join(sd, os.path.relpath(fresh, tmp))
                    self.assertEqual(open(fresh, 'rb').read(), open(shipped, 'rb').read(),
                                     f'{shipped} is stale: run tools/make_riivolution.py')

    def test_gecko_files(self):
        for region in REGIONS.values():
            writes, hooks = gecko.parse(os.path.join(jbpatch.CODES, region.gecko))
            self.assertEqual(len(writes), 1)
            self.assertEqual(len(hooks), len(jbpatch.GECKO_HOOKS))


if __name__ == '__main__':
    unittest.main()
