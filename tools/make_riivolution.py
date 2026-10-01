#!/usr/bin/env python3
"""Generate the Riivolution patches (riivolution/sd/) from the same code the
DOL patcher injects.

Copy the contents of riivolution/sd/ to the root of the SD card. There is one
XML per region, each with a choice of controller set; the patch's code is
written to 0x80001820 from a file in /JungleBeatPatch, and the game's hook
sites are overwritten with branches into it, each checked against the retail
word first.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jbpatch                                      # noqa: E402
from regions import REGIONS, TEXT_ADDRESS           # noqa: E402

ROOT = os.path.join(HERE, '..')
FOLDER = 'JungleBeatPatch'
GAME_REGION = {'R49E01': 'E', 'R49P01': 'P', 'R49J01': 'J'}
FILE_NAME = {'R49E01': 'USA', 'R49P01': 'Europe', 'R49J01': 'Japan'}
CHOICES = (
    ('GameCube / Bongos + Classic Controller', 'both', True, True),
    ('GameCube controller / DK Bongos', 'gc', False, True),
    ('Classic Controller', 'cc', True, False),
)


def originals(region):
    """Retail word at every address the patch overwrites."""
    (waddr, _), hooks = jbpatch.load_gecko(region)
    out = {waddr: jbpatch.GECKO_WRITE_PREIMAGE, region.kpad_read: jbpatch.KPAD_READ_PREIMAGE}
    for name, pre in zip(jbpatch.GECKO_HOOKS, jbpatch.GECKO_PREIMAGES):
        out[hooks[name][0]] = pre
    return out


def write_region(region, sd):
    folder = os.path.join(sd, FOLDER)
    os.makedirs(folder, exist_ok=True)
    retail = originals(region)
    patches = []
    for title, key, classic, gamecube in CHOICES:
        patch = jbpatch.make_patch(region, classic, gamecube)
        assert patch.base == TEXT_ADDRESS
        name = f'{region.disc_id}_{key}.bin'
        open(os.path.join(folder, name), 'wb').write(patch.blob)
        mem = [f'    <memory offset="0x{patch.base:08X}" valuefile="/{FOLDER}/{name}" />']
        words = dict(patch.site_words())
        words.update(patch.writes)
        for addr, value in sorted(words.items()):
            mem.append(f'    <memory offset="0x{addr:08X}" value="{value:08X}" '
                       f'original="{retail[addr]:08X}" />')
        patches.append((title, key, mem))
    xml = ['<wiidisc version="1">',
           f'  <id game="R49"><region type="{GAME_REGION[region.disc_id]}" /></id>',
           '  <options>',
           '    <section name="Donkey Kong Jungle Beat">',
           '      <option name="Controllers" default="1">']
    for title, key, _ in patches:
        xml.append(f'        <choice name="{title}"><patch id="jb_{key}" /></choice>')
    xml += ['      </option>', '    </section>', '  </options>']
    for _, key, mem in patches:
        xml.append(f'  <patch id="jb_{key}">')
        xml += mem
        xml.append('  </patch>')
    xml.append('</wiidisc>')
    path = os.path.join(sd, 'riivolution', f'JungleBeatPatch_{FILE_NAME[region.disc_id]}.xml')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w').write('\n'.join(xml) + '\n')
    return path


def main():
    sd = os.path.join(ROOT, 'riivolution', 'sd')
    shutil.rmtree(os.path.join(ROOT, 'riivolution'), ignore_errors=True)
    for region in REGIONS.values():
        print(os.path.relpath(write_region(region, sd), ROOT))


if __name__ == '__main__':
    main()
