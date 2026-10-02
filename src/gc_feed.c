/*
 * GameCube controller / DK Bongos feeder for Donkey Kong Jungle Beat.
 *
 * Runs at the entry of the KPAD library's pointer (DPD) routine, i.e. once
 * per Wii Remote sample, after the library has finished everything else for
 * that sample. It reads the pad's SI input registers (kept fresh by the SI
 * poller, src/poller.s) and rewrites the sample's KPADStatus so the game
 * sees a Wii Remote with a Nunchuk attached: buttons in hold/trig/release,
 * the Nunchuk stick, and the Wii Remote / Nunchuk acceleration the game
 * reads for claps and shakes.
 *
 * Without a Wii Remote the KPAD library has nothing to read and returns an
 * error, so gc_read() answers KPADRead itself: it fills the game's status
 * buffer with the same Wii Remote + Nunchuk statuses and returns their count.
 *
 * Build rules (see tools/build_blobs.py): freestanding, integer only (no FPU,
 * the hook sits before the hooked function's prologue), and no relocations,
 * so no static data. Region constants arrive as -D macros; the scratch area
 * is passed in, so the code works wherever it is placed.
 */
typedef unsigned int u32;
typedef int s32;
typedef unsigned char u8;

#if !defined(SI_TYPE) || !defined(BUBBLE_PTR) || !defined(WPAD_TBL) || !defined(CONNECT_CB) || !defined(KPAD_BASE)
#error "SI_TYPE, BUBBLE_PTR, WPAD_TBL, CONNECT_CB and KPAD_BASE must be defined"
#endif

#define SI_REG(n)   (((volatile u32 *)0xCD006400)[n])
#define W(p, o)     (*(volatile u32 *)((u8 *)(p) + (o)))

/* scratch layout, mirrored in tools/jbpatch.py */
#define CHAN_OFF    0x18
#define SEQ_OFF     0x1C                /* bumped by the poller once per KPADRead call */
#define ABSENT_OFF  0x50                /* since when the pad has been missing */
#define FAKED_OFF   0x24                /* we told the game a Wii Remote + Nunchuk is connected */
#define DEBUG_OFF   0x28                /* type, INBUFH, INBUFL, call count, event counters */
#define STATE_OFF   0x60
#define STATE_SIZE  0x40

#define TB_MS       60750u              /* time base ticks per millisecond */
#define BONGO_JOIN  (50u * TB_MS)       /* two drums this close = both drums */
#define BONGO_MOVE  (170u * TB_MS)      /* one drum hit = a short run */
#define BONGO_JUMP  (130u * TB_MS)      /* both drums = a held A press */

/* GameCube pad buttons, SICnINBUFH >> 16 */
#define PAD_START   0x1000
#define PAD_Y       0x0800
#define PAD_X       0x0400
#define PAD_B       0x0200
#define PAD_A       0x0100
#define PAD_L       0x0040
#define PAD_R       0x0020
#define PAD_Z       0x0010
#define PAD_UP      0x0008
#define PAD_DOWN    0x0004
#define PAD_RIGHT   0x0002
#define PAD_LEFT    0x0001

/* Wii Remote / Nunchuk hold bits, as the game's KPADStatus reports them */
#define WM_LEFT     0x0001
#define WM_RIGHT    0x0002
#define WM_DOWN     0x0004
#define WM_UP       0x0008
#define WM_PLUS     0x0010
#define WM_TWO      0x0100
#define WM_ONE      0x0200
#define WM_B        0x0400
#define WM_A        0x0800
#define WM_MINUS    0x1000
#define NC_Z        0x2000
#define WM_HOME     0x8000
#define CC_SWING    0x0040              /* pseudo bits the Classic Controller hooks keep in */
#define CC_SHAKE    0x0080              /* hold/trig for the Y and X buttons */

#define STICK_DEAD  18                  /* raw counts around centre */
#define STICK_MAX   88

#define SWING_F32   0x4059999Au         /* 3.4f, what the Classic Controller hooks use */

struct state {
    u32 seq;                            /* KPADRead call this state was last updated for */
    u32 last_hold;                      /* hold after the previous sample */
    u32 base_hold;                      /* hold at the end of the previous batch */
    u32 prev_btn;                       /* raw pad buttons, previous batch */
    u32 newb;                           /* raw buttons newly pressed this batch */
    u32 hit_l, hit_r;                   /* pending bongo hits (time | 1), 0 = none */
    u32 move_until;
    u32 jump_until;
    s32 dir;
    s32 shake_cnt;
    s32 shake_neg;
    u32 swing_seq;                      /* debug: last KPADRead call that clapped */
};

static inline u32 timebase(void)
{
    u32 t;
    __asm__ volatile("mftb %0" : "=r"(t));
    return t;
}

/* n / 64 as IEEE-754 single bits, n in -64..64 */
static u32 f32_64ths(s32 n)
{
    u32 sign = 0, m, p;
    if (n == 0)
        return 0;
    if (n < 0) {
        sign = 0x80000000u;
        n = -n;
    }
    m = (u32)n;
    p = 31 - __builtin_clz(m);          /* 0..6 */
    return sign | ((127 + p - 6) << 23) | ((m << (23 - p)) & 0x7FFFFF);
}

/* one stick axis, raw byte 0..255 around 128 -> -64..64 */
static s32 axis(u32 raw)
{
    s32 v = (s32)raw - 128, mag, out;
    mag = v < 0 ? -v : v;
    if (mag <= STICK_DEAD)
        return 0;
    if (mag > STICK_MAX)
        mag = STICK_MAX;
    out = ((mag - STICK_DEAD) * 64) / (STICK_MAX - STICK_DEAD);
    return v < 0 ? -out : out;
}

static int in_bubble(void)
{
    u32 p = W(BUBBLE_PTR, 0);
    if ((p & 3) || p < 0x80000000u || p >= 0x81800000u)
        return 0;
    return W(p, 8) == 0xC5EAE845u;
}

struct pad {
    u32 add;                            /* Wii Remote / Nunchuk hold bits to add */
    s32 sx, sy;                         /* Nunchuk stick, -64..64 */
    int swing, shake, bongo;
    u32 libdev;                         /* extension the library reported (feeder only) */
};

/* Read the pad on `chan` and work out what it means; once per call of the
 * function it is used from. Returns 0 if no pad is answering. */
static int read_pad(u8 *S, u32 chan, struct pad *p)
{
    struct state *st = (struct state *)(S + STATE_OFF + chan * STATE_SIZE);
    u32 type, hi, btn, now;

    p->add = 0;
    p->sx = p->sy = 0;
    p->swing = p->shake = 0;

    type = ((volatile u32 *)SI_TYPE)[chan];
    W(S, DEBUG_OFF + 0x00) = type;                      /* last look at the pad, for debugging */
    W(S, DEBUG_OFF + 0x0C)++;
    if ((type & 0x80) || (type & 0x18000000) != 0x08000000)
        goto idle;
    hi = SI_REG(1 + 3 * chan);
    W(S, DEBUG_OFF + 0x04) = hi;
    W(S, DEBUG_OFF + 0x08) = SI_REG(2 + 3 * chan);
    if (hi & 0x80000000u)               /* ERRSTAT: nothing answered */
        goto idle;

    now = timebase();
    btn = (hi >> 16) & 0x1FFF;
    p->bongo = (hi & 0xFFFF) == 0;      /* a pad's sticks rest near 0x80 */

    /* The KPAD library hands the game one status per sample but works out
     * trig/release once per KPADRead call, so every sample of a call carries
     * the same edges and the game can read them from whichever it looks at.
     * Do the same: edges are taken against the end of the previous call. */
    if (st->seq != W(S, SEQ_OFF)) {
        st->seq = W(S, SEQ_OFF);
        st->base_hold = st->last_hold;
        st->newb = btn & ~st->prev_btn;
        st->prev_btn = btn;
        if (p->bongo) {
            /* DK Bongos: A/X are the right drum, B/Y the left, R is the clap
             * microphone. A drum hit runs DK for a moment; both drums
             * together jump (and confirm in menus). */
            if (st->hit_l && now - st->hit_l > BONGO_JOIN) {
                st->dir = -1;
                st->move_until = now + BONGO_MOVE;
                st->hit_l = 0;
                W(S, DEBUG_OFF + 0x14)++;
            }
            if (st->hit_r && now - st->hit_r > BONGO_JOIN) {
                st->dir = 1;
                st->move_until = now + BONGO_MOVE;
                st->hit_r = 0;
                W(S, DEBUG_OFF + 0x18)++;
            }
            if (st->newb & (PAD_B | PAD_Y))     st->hit_l = now | 1;
            if (st->newb & (PAD_A | PAD_X))     st->hit_r = now | 1;
            if (st->hit_l && st->hit_r) {
                st->jump_until = now + BONGO_JUMP;
                W(S, DEBUG_OFF + 0x10)++;
                st->hit_l = st->hit_r = 0;
                st->move_until = 0;
            }
        }
    }

    if (!p->bongo) {
        if (btn & PAD_A)                p->add |= WM_A;
        if (btn & (PAD_B | PAD_R | PAD_Z)) p->add |= WM_B;
        if (btn & PAD_L)                p->add |= NC_Z;
        if (btn & PAD_X)                p->shake = 1;
        if (st->newb & PAD_Y)           p->swing = 1;
        if (btn & PAD_UP)               p->add |= WM_UP;
        if (btn & PAD_DOWN)             p->add |= WM_DOWN;
        if (btn & PAD_LEFT)             p->add |= WM_LEFT;
        if (btn & PAD_RIGHT)            p->add |= WM_RIGHT;
        if (btn & PAD_START) {
            if ((btn & (PAD_L | PAD_R)) == (PAD_L | PAD_R))
                p->add |= WM_HOME;
            else
                p->add |= WM_PLUS | WM_TWO;
        }
        p->sx = axis((hi >> 8) & 0xFF);
        p->sy = axis(hi & 0xFF);
    } else {
        if ((s32)(st->jump_until - now) > 0)
            p->add |= WM_A;
        if ((s32)(st->move_until - now) > 0)
            p->sx = st->dir * 64;
        if (st->newb & PAD_R)           p->swing = 1;
        if (btn & PAD_START)            p->add |= WM_PLUS | WM_TWO;
    }
    return 1;

idle:
    st->prev_btn = 0;
    st->newb = 0;
    st->hit_l = st->hit_r = 0;
    st->move_until = st->jump_until = 0;
    return 0;
}

/* Clap and shake as acceleration. The game finds a clap or a shake in the
 * history of the Wii Remote's (or Nunchuk's) acceleration, one entry per
 * status. A clap is a single status at 3.4 g on every axis, as the Classic
 * Controller hooks send it; the next clap goes the other way, so clapping to a
 * beat also reads as a shake. A shake is the square wave those hooks use:
 * three statuses one way, one at rest, three the other way, because the
 * library delivers several statuses per frame. */
static u32 motion(u8 *S, struct state *st, const struct pad *p)
{
    u32 acc = 0;

    if (p->swing) {
        if (st->swing_seq != st->seq) { /* the first status of this KPADRead call only */
            st->swing_seq = st->seq;
            W(S, DEBUG_OFF + 0x1C)++;
            acc = st->shake_neg ? (SWING_F32 | 0x80000000u) : SWING_F32;
            st->shake_neg ^= 1;
        }
    } else if (p->shake) {
        if (++st->shake_cnt <= 3) {
            acc = st->shake_neg ? (SWING_F32 | 0x80000000u) : SWING_F32;
        } else {
            st->shake_neg ^= 1;
            st->shake_cnt = 0;
        }
    } else {
        st->shake_cnt = 0;
    }
    return acc;
}

/* the sample ring slot `i` of a channel's KPAD state */
static u8 *ring_slot(u8 *b, u32 i)
{
    return i < 0x10 ? b + 0x13C + i * 0x38 : *(u8 **)(b + 0x4BC) + (i - 0x10) * 0x38;
}

/* gc_feed() leaves each status reporting a Nunchuk, but the samples queued
 * for the library are a bare remote's. It compares the channel's extension
 * with every sample's and, finding them different, takes it for an
 * extension being plugged in or pulled out: it marks the older samples
 * invalid and clears its state, which the game sees as the stick snapping
 * to zero and back every other frame (menus scroll as fast as they can).
 * So make the queued samples say Nunchuk too, whoever queued them. */
static void unify_ring(u8 *b)
{
    u32 total = *(u8 **)(b + 0x4BC) ? W(b, 0x4C0) + 0x10 : 0x10;
    u32 idx = b[0x13A], cnt = b[0x13B], n, at;

    if (total > 0x400 || idx >= total)
        return;
    if (cnt > total)
        cnt = total;
    for (n = cnt, at = idx; n--;) {
        at = at ? at - 1 : total - 1;
        ring_slot(b, at)[0x28] = 1;
    }
    if (cnt)
        b[0x5C] = 1;
}

void gc_feed(u8 *k, u8 *S)
{
    u32 chan = W(S, CHAN_OFF);
    struct state *st;
    struct pad p;
    u32 hold, acc;

    if (chan > 3)
        return;
    st = (struct state *)(S + STATE_OFF + chan * STATE_SIZE);
    p.libdev = k[0x5C];
    unify_ring((u8 *)KPAD_BASE + chan * 0x5C0);
    if (!read_pad(S, chan, &p)) {
        st->last_hold = W(k, 0x00);
        st->base_hold = st->last_hold;
        return;
    }

    acc = motion(S, st, &p);
    if (acc && p.swing)  p.add |= CC_SWING;
    if (p.shake)         p.add |= CC_SHAKE;
    hold = W(k, 0x00) | p.add;
    W(k, 0x00) = hold;
    W(k, 0x04) = hold & ~st->base_hold;
    W(k, 0x08) = st->base_hold & ~hold;
    st->last_hold = hold;

    /* A pad owns the motion inputs outright. Bongos have no sticks and no
     * accelerometer, so the Wii Remote (and Nunchuk, if there is one) keeps
     * working next to them: only claps and drum runs are added on top. */
    if (!p.bongo) {
        if (in_bubble()) {
            /* bubble: the Wii Remote's tilt steers; left stick stands in */
            W(k, 0x0C) = f32_64ths(-p.sx);
        } else {
            W(k, 0x0C) = acc;
            W(k, 0x10) = acc;
            W(k, 0x14) = acc;
        }
    } else if (p.swing && acc) {
        W(k, 0x0C) = acc;
        W(k, 0x10) = acc;
        W(k, 0x14) = acc;
    }
    if (!p.bongo || acc || p.libdev != 1) {
        W(k, 0x68) = W(k, 0x0C);        /* Nunchuk acceleration mirrors the Wii Remote */
        W(k, 0x6C) = W(k, 0x10);
        W(k, 0x70) = W(k, 0x14);
    }
    if (!p.bongo || p.sx || p.libdev != 1) {
        W(k, 0x60) = f32_64ths(p.sx);   /* Nunchuk stick */
        W(k, 0x64) = f32_64ths(p.sy);
    }
    k[0x5C] = 1;                        /* Wii Remote + Nunchuk */
    k[0x5D] = 0;
}

/* What WPADProbe(chan) returns: 0 for a connected remote, -1 for none, -2 if
 * the channel isn't set up yet. Same reads the library makes. */
static s32 remote_probe(u32 chan)
{
    u8 *blk = *(u8 **)(WPAD_TBL + chan * 4);
    s32 ret = (s32)W(blk, 0x8BC);

    if (ret != -1) {
        if (blk[0x8C1] == 0xFD)
            ret = -1;
        else if (W(blk, 0x8DC) == 0)
            ret = -2;
    }
    return ret;
}

/* Is a GameCube pad answering on `chan`? Looks only. */
static int pad_present(u32 chan)
{
    u32 type = ((volatile u32 *)SI_TYPE)[chan];

    if ((type & 0x80) || (type & 0x18000000) != 0x08000000)
        return 0;
    return !(SI_REG(1 + 3 * chan) & 0x80000000u);
}

/* WPADProbe(chan, &type): with no remote but a pad, say a remote with a
 * Nunchuk is there, so the game doesn't stop to tell the player the Wii Remote
 * has been disconnected. Returns 1 if it answered (the result is 0). */
int gc_probe(u32 chan, s32 *type, u8 *S)
{
    (void)S;
    if (chan != 0 || !pad_present(chan) || remote_probe(chan) >= 0)
        return 0;
    if (type)
        *type = 1;
    return 1;
}

/* KPADRead(chan, ...) with no Wii Remote connected: the library has nothing to
 * read, so give it something. Queue two bare Wii Remote samples in the channel's
 * sample ring -- two, like the library's own readers expect, with the game
 * looking at the second entry for some things -- and let the library process
 * them as usual; gc_feed() then turns each one into the pad's status. */

void gc_inject(u32 chan, u8 *S)
{
    u8 *b = (u8 *)KPAD_BASE + chan * 0x5C0;
    u32 total, idx, cnt, i, want;
    int remote;

    if (chan != 0)
        return;
    remote = remote_probe(chan) >= 0;
    if (!pad_present(chan)) {
        /* The pad can drop out for a moment while the poller re-probes the port,
         * so only call it gone after a full second. */
        if (W(S, FAKED_OFF) && !remote) {
            u32 now = timebase();
            if (!W(S, ABSENT_OFF))
                W(S, ABSENT_OFF) = now | 1;
            else if (now - W(S, ABSENT_OFF) > 1000u * TB_MS) {
                W(S, FAKED_OFF) = 0;    /* the pad went away: the remote did too */
                W(S, ABSENT_OFF) = 0;
                ((void (*)(u32, s32))CONNECT_CB)(chan, -1);
            }
        }
        return;
    }
    W(S, ABSENT_OFF) = 0;
    total = *(u8 **)(b + 0x4BC) ? W(b, 0x4C0) + 0x10 : 0x10;   /* 16 inline slots plus the extra array */
    if (total > 0x400)
        return;
    idx = b[0x13A];
    cnt = b[0x13B];
    want = 0;
    if (remote) {
        W(S, FAKED_OFF) = 0;
        if (cnt == 0)
            want = 1;                   /* a frame the remote had nothing new for: the library
                                           would hand the game an error status, which reads as
                                           the stick snapping to zero. Give it a resting one. */
    } else {
        if (!W(S, FAKED_OFF)) {
            /* Tell the game what the library would on a connection, so its own
             * "controller connected" state is right and it doesn't stop to say
             * the Wii Remote has been disconnected. */
            W(S, FAKED_OFF) = 1;
            ((void (*)(u32, s32))CONNECT_CB)(chan, 0);
        }
        if (cnt < 2)
            want = 2 - cnt;
    }
    if (want && idx < total) {
        for (i = 0; i < want; i++) {
            u8 *s = ring_slot(b, idx);
            u32 j;
            for (j = 0; j < 0x38; j += 4)
                W(s, j) = 0;
            s[7] = 0x68;                /* what a resting remote reports on Z */
            s[0x28] = 1;
            s[0x36] = 1;
            idx = idx + 1 >= total ? 0 : idx + 1;
        }
        b[0x13A] = idx;
        b[0x13B] = cnt + want;
    }
    unify_ring((u8 *)KPAD_BASE + chan * 0x5C0);
}
