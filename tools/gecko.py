"""Minimal Gecko code-list parser (04 writes and C2 insertions)."""
import re


def parse(path):
    """Return ([(addr, word)], {hook_addr: [words]}) from a Gecko .ini."""
    lines = [l.strip() for l in open(path).read().splitlines()]
    writes, hooks, i = [], {}, 0
    while i < len(lines):
        m = re.fullmatch(r'([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})', lines[i])
        i += 1
        if not m:
            continue
        op, arg = int(m.group(1), 16), int(m.group(2), 16)
        kind, addr = op >> 24, 0x80000000 | (op & 0x01FFFFFF)
        if kind == 0x04:
            writes.append((addr, arg))
        elif kind == 0xC2:
            words = []
            for line in lines[i:i + arg]:
                words.extend(int(f, 16) for f in line.split())
            hooks[addr] = words
            i += arg
        else:
            raise ValueError(f'unsupported Gecko code type {kind:02X}')
    return writes, hooks
