"""Per-region addresses for the USA, European and Japanese releases.

Everything here was found by reading the USA main.dol in Ghidra and then
matching the surrounding code (branch targets and immediates masked out)
against the other two DOLs; each match was unique. The Classic Controller
hook sites come straight from Vague Rant's per-region Gecko codes.
"""

TEXT_ADDRESS = 0x80001820   # start of the patch's DOL text section
TEXT_LIMIT = 0x80003000     # the OS's low-memory globals start here


class Region:
    def __init__(self, disc_id, name, gecko, **addrs):
        self.disc_id = disc_id
        self.name = name
        self.gecko = gecko              # codes/<gecko>.ini
        self.__dict__.update(addrs)

    def __repr__(self):
        return f'{self.name} ({self.disc_id})'


REGIONS = {
    'R49E01': Region(
        'R49E01', 'USA', 'R49E01.ini',
        kpad_read=0x8034DDE4,
        si_state=0x804770A0,            # si:: busy flag, SIPOLL shadow, types at +0x18
        si_gettype=0x8031B940,
        os_disable=0x803118F8,
        os_restore=0x80311920,
        wpad_probe=0x80341B88,          # WPADProbe
        wpad_tbl=0x804C1AB0,            # per-channel WPAD control block pointers
        connect_cb=0x802398C0,          # the game's WPAD connect callback (chan, result)
        kpad_base=0x804C5248,           # KPAD library state, 0x5C0 per channel
        bubble_ptr=0x80C21040,          # EventDirector data pointer (Vague Rant)
    ),
    'R49P01': Region(
        'R49P01', 'Europe', 'R49P01.ini',
        kpad_read=0x8034E4A4,
        si_state=0x8047ABA0,
        si_gettype=0x8031C000,
        os_disable=0x80311FB8,
        os_restore=0x80311FE0,
        wpad_probe=0x80342248,
        wpad_tbl=0x804C55B0,
        connect_cb=0x80239F00,
        kpad_base=0x804C8D48,
        bubble_ptr=0x80C24B80,
    ),
    'R49J01': Region(
        'R49J01', 'Japan', 'R49J01.ini',
        kpad_read=0x8034CCA4,
        si_state=0x804763E0,
        si_gettype=0x8031A800,
        os_disable=0x803107B8,
        os_restore=0x803107E0,
        wpad_probe=0x80340A48,
        wpad_tbl=0x804C0DF0,
        connect_cb=0x802388F0,
        kpad_base=0x804C4588,
        bubble_ptr=0x80C20360,
    ),
}

KPAD_READ_PREIMAGE = 0x9421FEF0         # stwu r1,-0x110(r1)
WPAD_PROBE_PREIMAGE = 0x9421FFF0        # stwu r1,-0x10(r1)
