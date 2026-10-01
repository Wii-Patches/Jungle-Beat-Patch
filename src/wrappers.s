    # Hook wrappers. Each saves every register the hooked code might still
    # need (r3-r31, CR, CTR, LR), calls into the patch, restores them, then
    # runs the instruction the hook replaced and returns to the game.
    # tools/jbpatch.py fills the *_orig word (the replaced instruction, or a
    # nop when a Gecko body further down the chain repeats it) and the *_ret
    # branch.

    .text
    .globl  poller_entry, poller_orig, poller_ret
    .globl  feeder_entry, feeder_orig, feeder_ret

poller_entry:
    stwu    1, -0xA0(1)
    stmw    3, 0x10(1)
    mflr    0
    stw     0, 0x88(1)
    mfcr    0
    stw     0, 0x8C(1)
    mfctr   0
    stw     0, 0x90(1)
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
poller_ret:
    b       poller_ret

feeder_entry:
    stwu    1, -0xA0(1)
    stmw    3, 0x10(1)
    mflr    0
    stw     0, 0x88(1)
    mfcr    0
    stw     0, 0x8C(1)
    mfctr   0
    stw     0, 0x90(1)
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
feeder_ret:
    b       feeder_ret
