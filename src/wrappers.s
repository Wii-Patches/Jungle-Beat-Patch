    # Hook wrappers. Each saves every register the hooked code might still
    # need (r3-r31, CR, CTR, LR), points r31 at the patch's scratch area, calls
    # into the patch, restores everything, then runs the instruction the hook
    # replaced and jumps back into the game.
    #
    # Position independent: everything is relative to the wrapper itself, so
    # the same words work in a DOL section (tools/jbpatch.py) and as a Gecko
    # C2 body wherever the code handler puts it. The patcher fills the *_orig
    # word (the replaced instruction, or a nop when another body further down
    # the chain repeats it) and the *_hi / *_lo halves of the return address;
    # the jump goes through r0 and CTR, both dead at a function's first
    # instruction.

    .text
    .globl  poller_entry, poller_orig, poller_hi, poller_lo
    .globl  feeder_entry, feeder_orig, feeder_hi, feeder_lo, scratch

poller_entry:
    stwu    1, -0xA0(1)
    stmw    3, 0x10(1)
    mflr    0
    stw     0, 0x88(1)
    mfcr    0
    stw     0, 0x8C(1)
    mfctr   0
    stw     0, 0x90(1)
    bl      1f
1:  mflr    31
    addi    31, 31, scratch - 1b
    bl      poller_body
    lwz     0, 0x90(1)
    mtctr   0
    lwz     0, 0x8C(1)
    mtcr    0
    lwz     0, 0x88(1)
    mtlr    0
    lmw     3, 0x10(1)
    addi    1, 1, 0xA0
poller_orig:
    nop
poller_hi:
    lis     0, 0
poller_lo:
    ori     0, 0, 0
    mtctr   0
    bctr

feeder_entry:
    stwu    1, -0xA0(1)
    stmw    3, 0x10(1)
    mflr    0
    stw     0, 0x88(1)
    mfcr    0
    stw     0, 0x8C(1)
    mfctr   0
    stw     0, 0x90(1)
    bl      1f
1:  mflr    31
    addi    4, 31, scratch - 1b
    bl      gc_feed
    lwz     0, 0x90(1)
    mtctr   0
    lwz     0, 0x8C(1)
    mtcr    0
    lwz     0, 0x88(1)
    mtlr    0
    lmw     3, 0x10(1)
    addi    1, 1, 0xA0
feeder_orig:
    nop
feeder_hi:
    lis     0, 0
feeder_lo:
    ori     0, 0, 0
    mtctr   0
    bctr

    # Scratch area, zeroed. Offsets (mirrored in gc_feed.c):
    #   +0x00  per-channel last-probe time base      +0x18  channel being read
    #   +0x14  per-channel consecutive-NOREP counts  +0x1C  KPADRead call counter
    #   +0x20  SI watchdog time base                 +0x24  feeder hook installed
    #   +0x28  debug block                           +0x60  per-channel feeder state
    .balign 4
scratch:
    .space  0x180
