    # SI poller -- called from KPADReadEx's entry, once per channel per frame
    # (r3 = channel), before the controller feeder reads SICnINBUFH/L.
    #
    # Jungle Beat links the Wii SDK's si:: library (SIInit runs at boot) but
    # not the PAD library, so nothing ever enables SI auto-polling and the
    # INBUF registers stay empty on a real console. This programs the
    # hardware itself: it writes the poll command into every channel's
    # SICnOUTBUF, latches it, and enables polling (plus copy-on-vblank) for
    # the channels whose cached si:: type is a confirmed GameCube device.
    #
    # Everything is a subroutine: the C2-style wrapper in jbpatch.py saves
    # r3-r31, CR, CTR and LR, calls this, restores them and runs the hooked
    # instruction. Placeholders in braces are filled in per region.
    #
    # SIGetType is called only for a channel whose cached type is not a
    # confirmed standard pad (8 = no response, 0x80 = probe pending, or
    # anything else). si:: never learns this game polls, so for a confirmed
    # pad it would re-probe every ~50 ms and flip the type to pending each
    # time; skipping the call keeps confirmed pads stable and polled.

    .globl  poller_body
poller_body:
    stwu    1, -0x20(1)
    stw     0, 0x1c(1)
    stw     3, 0x18(1)
    stw     4, 0x14(1)
    stw     5, 0x08(1)
    mflr    0
    stw     0, 0x0c(1)

    # remember which channel KPADRead is serving; the feeder reads it
    lis     12, {SCR_CHAN_HA}
    stw     3, {SCR_CHAN_LO}(12)
    # ... and count the call: the feeder derives trig/release once per batch
    # of samples, the way the KPAD library does, not once per sample
    lwz     11, {SCR_SEQ_LO}(12)
    addi    11, 11, 1
    stw     11, {SCR_SEQ_LO}(12)

    cmplwi  3, 3
    bgt     probe_done
    lis     12, {SI_TYPE_HI}
    ori     12, 12, {SI_TYPE_LO}    # si:: cached type per channel
    slwi    4, 3, 2
    lwzx    4, 12, 4
    andi.   12, 4, 0x80
    bne     do_probe                # pending: let SIGetType follow it up
    rlwinm  12, 4, 0, 3, 4          # & 0x18000000
    lis     4, 0x0800
    cmpw    12, 4
    beq     probe_done              # confirmed standard pad: leave it alone
do_probe:
    # At most one probe per channel every 0.25 s. Probing the empty ports
    # every frame collided with the pad's polling on real hardware (NOREP|COLL
    # on the polled channel while the other ports had a type transfer in
    # flight), which knocked a connected pad back to "no response".
    lis     12, {SCR_PROBE_HI}
    ori     12, 12, {SCR_PROBE_LO}  # per-channel last-probe time base
    slwi    4, 3, 2
    mftb    6
    lwzx    7, 12, 4
    subf    8, 7, 6
    lis     9, 0x00E7
    ori     9, 9, 0xBE2C            # 15,187,500 ticks = 0.25 s
    cmplw   8, 9
    blt     probe_done
    stwx    6, 12, 4
    lis     12, {SIGETTYPE_HI}
    ori     12, 12, {SIGETTYPE_LO}  # si::SIGetType(channel)
    mtctr   12
    bctrl
probe_done:
    lwz     0, 0x0c(1)
    mtlr    0

    lwz     3, 0x18(1)
    lis     3, 0xCD00

    # Re-probe after a disconnect. SIGetType only sends a fresh type command
    # when the SDK's cached type for the channel is 8 ("no response"), and
    # the only thing that sets 8 is the SDK reading NOREP (0x08 in the
    # channel's SISR byte) -- which this game never does, because it never
    # uses the PAD library. So copy a persistent NOREP into the cache
    # ourselves, *before* acknowledging it below.
    lwz     4, 0x6438(3)            # SISR
    lis     6, {SI_TYPE_HI}
    ori     6, 6, {SI_TYPE_LO}      # si:: cached type per channel (4 words)
    li      8, 0                    # channel
    li      9, 8                    # "no response" type
    lis     12, {SCR_NOREP_HI}
    ori     12, 12, {SCR_NOREP_LO}  # per-channel consecutive-NOREP counters
norep_loop:
    slwi    10, 8, 3
    lis     11, 0x0800              # channel 0's NOREP bit
    srw     11, 11, 10
    lbzx    7, 12, 8
    and.    11, 11, 4
    beq     norep_clear
    # Only a NOREP that persists means the pad is gone: a single one also
    # latches from a collision with a type probe on another channel.
    cmplwi  7, 40
    bge     norep_mark
    addi    7, 7, 1
    stbx    7, 12, 8
    b       norep_next
norep_mark:
    slwi    10, 8, 2
    stwx    9, 6, 10
    b       norep_next
norep_clear:
    li      7, 0
    stbx    7, 12, 8
norep_next:
    addi    8, 8, 1
    cmpwi   8, 4
    blt     norep_loop

    # Poll command into all four channels' output buffers.
    lis     0, 0x0040
    ori     0, 0, 0x0300
    stw     0, 0x6400(3)            # SIC0OUTBUF
    stw     0, 0x640C(3)            # SIC1OUTBUF
    stw     0, 0x6418(3)            # SIC2OUTBUF
    stw     0, 0x6424(3)            # SIC3OUTBUF

    # One SISR write acknowledges every channel's latched error nibble
    # (write-1-to-clear) and sets WR (bit 31), which is what transfers the
    # SICnOUTBUF values above to the hardware -- the SDK's SIEnablePolling
    # always writes SISR = 0x80000000 before SIPOLL.
    lis     0, 0x0F0F
    ori     0, 0, 0x0F0F
    and     4, 4, 0
    oris    4, 4, 0x8000
    stw     4, 0x6438(3)

    # Enable polling (and copy-on-vblank) only for channels whose cached
    # type is a confirmed standard pad. On real hardware a channel's type
    # probe only succeeds while that channel is not being polled.
    li      7, 0                    # enable mask
    li      8, 0
en_loop:
    slwi    10, 8, 2
    lwzx    10, 6, 10
    andi.   11, 10, 0x80
    bne     en_next                 # probe pending
    rlwinm  11, 10, 0, 3, 4         # & 0x18000000
    lis     12, 0x0800
    cmpw    11, 12
    bne     en_next
    li      11, 0x88                # EN + VBCPY bits for channel 0
    srw     11, 11, 8
    or      7, 7, 11
en_next:
    addi    8, 8, 1
    cmpwi   8, 4
    blt     en_loop

    lwz     0, 0x6430(3)            # SIPOLL
    rlwinm  0, 0, 0, 0, 23          # clear the enable/VBCPY byte, keep X/Y
    andi.   4, 0, 0xff00            # Y field already set?
    bne     ypresent
    ori     0, 0, 0x0100            # Y = 1
ypresent:
    or      0, 0, 7
    stw     0, 0x6430(3)

    # Mirror the enable/VBCPY byte into si::'s own SIPOLL shadow. The VI
    # retrace handler calls SIRefreshSamplingRate, which ends in SISetXY
    # writing SIPOLL = shadow | X/Y; with a zero shadow every refresh would
    # switch our polling back off.
    lis     12, {SI_SHADOW_HA}
    lwz     11, {SI_SHADOW_LO}(12)  # si:: SIPOLL shadow
    rlwinm  11, 11, 0, 0, 23
    or      11, 11, 7
    stw     11, {SI_SHADOW_LO}(12)

    # Hot-plug watchdog. si::__SITransfer gates every SI transfer --
    # SIGetType included -- behind ONE global "busy" flag (-1 = idle); the
    # only place that clears it is si::CompleteTransfer, reachable only when
    # both SICOMCSR TC-complete bits are set together. A pad unplugged
    # mid-transfer signals NOREP instead, so the flag can stay wedged on the
    # dead channel and every later SIGetType, for ALL channels, silently
    # no-ops. If the flag has read non-idle for ~1 real second, force it back
    # to -1 and reset SICOMCSR to 0x80000000 (si::SIInit's own idle value).
    # Time is measured with the time base, not by counting calls. The low
    # time-base word at which the flag was first seen busy lives in the
    # patch's scratch area (0 = not currently busy).
    lis     5, {SI_BUSY_HI}
    ori     5, 5, {SI_BUSY_LO}
    lwz     6, 0(5)                 # si:: global transfer-busy flag; -1 = idle
    lis     7, {SCR_WD_HI}
    ori     7, 7, {SCR_WD_LO}       # time base when busy began
    cmpwi   6, -1
    bne     wd_busy
    li      8, 0
    b       wd_store
wd_busy:
    lwz     8, 0(7)
    mftb    9
    cmpwi   8, 0
    bne     wd_timing
    ori     8, 9, 1                 # first busy sighting: stamp it (never store 0)
    b       wd_store
wd_timing:
    subf    10, 8, 9                # ticks busy (unsigned, wrap-safe)
    lis     11, 0x039F
    ori     11, 11, 0x8B0           # 60,750,000 ticks = 1 s
    cmplw   10, 11
    blt     wd_done
    mflr    9
    stw     9, 0x10(1)
    lis     12, {OSDISABLE_HI}
    ori     12, 12, {OSDISABLE_LO}  # OSDisableInterrupts
    mtctr   12
    bctrl
    mr      9, 3
    lis     5, {SI_BUSY_HI}
    ori     5, 5, {SI_BUSY_LO}
    li      6, -1
    stw     6, 0(5)                 # unwedge si::'s global busy flag
    lis     6, 0xCD00
    lis     0, 0x8000
    stw     0, 0x6434(6)            # SICOMCSR = 0x80000000
    mr      3, 9
    lis     12, {OSRESTORE_HI}
    ori     12, 12, {OSRESTORE_LO}  # OSRestoreInterrupts
    mtctr   12
    bctrl
    lwz     9, 0x10(1)
    mtlr    9
    li      8, 0
wd_store:
    stw     8, 0(7)
wd_done:

    lwz     0, 0x1c(1)
    lwz     3, 0x18(1)
    lwz     4, 0x14(1)
    lwz     5, 0x08(1)
    addi    1, 1, 0x20
    blr
