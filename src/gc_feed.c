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
 * Build rules (see tools/build_blobs.py): freestanding, integer only (no FPU,
 * the hook sits before the hooked function's prologue), and no relocations,
 * so no static data. Region constants arrive as -D macros.
 */
typedef unsigned int u32;
typedef int s32;
typedef unsigned char u8;

#ifndef SCRATCH
#error "SCRATCH, SI_TYPE and BUBBLE_PTR must be defined"
#endif

#define SI_REG(n)   (((volatile u32 *)0xCD006400)[n])
#define W(p, o)     (*(volatile u32 *)((u8 *)(p) + (o)))

/* scratch layout, mirrored in tools/jbpatch.py */
#define CHAN_OFF    0x18
#define SEQ_OFF     0x1C                /* bumped by the poller once per KPADRead call */
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

void gc_feed(u8 *k)
{
    u32 chan = W(SCRATCH, CHAN_OFF);
    struct state *st;
    u32 type, hi, btn, newb, hold, trig, rel, now;
    u32 add = 0, acc;
    s32 sx = 0, sy = 0;
    int swing = 0, shake = 0, bongo;
    u8 libdev = k[0x5C];

    if (chan > 3)
        return;
    st = (struct state *)((u8 *)SCRATCH + STATE_OFF + chan * STATE_SIZE);

    type = ((volatile u32 *)SI_TYPE)[chan];
    W(SCRATCH, DEBUG_OFF + 0x00) = type;                /* last look at the pad, for debugging */
    W(SCRATCH, DEBUG_OFF + 0x0C)++;
    if ((type & 0x80) || (type & 0x18000000) != 0x08000000)
        goto idle;
    hi = SI_REG(1 + 3 * chan);
    W(SCRATCH, DEBUG_OFF + 0x04) = hi;
    W(SCRATCH, DEBUG_OFF + 0x08) = SI_REG(2 + 3 * chan);
    if (hi & 0x80000000u)               /* ERRSTAT: nothing answered */
        goto idle;

    now = timebase();
    btn = (hi >> 16) & 0x1FFF;
    bongo = (hi & 0xFFFF) == 0;         /* a pad's sticks rest near 0x80 */

    /* The KPAD library hands the game one status per sample but works out
     * trig/release once per KPADRead call, so every sample of a call carries
     * the same edges and the game can read them from whichever it looks at.
     * Do the same: edges are taken against the end of the previous call. */
    if (st->seq != W(SCRATCH, SEQ_OFF)) {
        st->seq = W(SCRATCH, SEQ_OFF);
        st->base_hold = st->last_hold;
        st->newb = btn & ~st->prev_btn;
        st->prev_btn = btn;
        if (bongo) {
            /* DK Bongos: A/X are the right drum, B/Y the left, R is the clap
             * microphone. A drum hit runs DK for a moment; both drums
             * together jump (and confirm in menus). */
            if (st->hit_l && now - st->hit_l > BONGO_JOIN) {
                st->dir = -1;
                st->move_until = now + BONGO_MOVE;
                st->hit_l = 0;
                W(SCRATCH, DEBUG_OFF + 0x14)++;
            }
            if (st->hit_r && now - st->hit_r > BONGO_JOIN) {
                st->dir = 1;
                st->move_until = now + BONGO_MOVE;
                st->hit_r = 0;
                W(SCRATCH, DEBUG_OFF + 0x18)++;
            }
            if (st->newb & (PAD_B | PAD_Y))     st->hit_l = now | 1;
            if (st->newb & (PAD_A | PAD_X))     st->hit_r = now | 1;
            if (st->hit_l && st->hit_r) {
                st->jump_until = now + BONGO_JUMP;
                W(SCRATCH, DEBUG_OFF + 0x10)++;
                st->hit_l = st->hit_r = 0;
                st->move_until = 0;
            }
        }
    }
    newb = st->newb;

    if (!bongo) {
        if (btn & PAD_A)                add |= WM_A;
        if (btn & (PAD_B | PAD_R | PAD_Z)) add |= WM_B;
        if (btn & PAD_L)                add |= NC_Z;
        if (btn & PAD_X)                shake = 1;
        if (newb & PAD_Y)               swing = 1;
        if (btn & PAD_UP)               add |= WM_UP;
        if (btn & PAD_DOWN)             add |= WM_DOWN;
        if (btn & PAD_LEFT)             add |= WM_LEFT;
        if (btn & PAD_RIGHT)            add |= WM_RIGHT;
        if (btn & PAD_START) {
            if ((btn & (PAD_L | PAD_R)) == (PAD_L | PAD_R))
                add |= WM_HOME;
            else
                add |= WM_PLUS | WM_TWO;
        }
        sx = axis((hi >> 8) & 0xFF);
        sy = axis(hi & 0xFF);
    } else {
        if ((s32)(st->jump_until - now) > 0)
            add |= WM_A;
        if ((s32)(st->move_until - now) > 0)
            sx = st->dir * 64;
        if (newb & PAD_R)               swing = 1;
        if (btn & PAD_START)            add |= WM_PLUS | WM_TWO;
    }

    hold = W(k, 0x00) | add;
    trig = hold & ~st->base_hold;
    rel = st->base_hold & ~hold;
    st->last_hold = hold;
    W(k, 0x00) = hold;
    W(k, 0x04) = trig;
    W(k, 0x08) = rel;

    /* Clap and shake as acceleration: the same square wave the Classic
     * Controller hooks use (three samples one way, one at rest, three the
     * other way), because the game counts a shake from the change between
     * frames and the library delivers several samples per frame. */
    acc = 0;
    if (swing && st->seq != st->swing_seq) {
        st->swing_seq = st->seq;
        W(SCRATCH, DEBUG_OFF + 0x1C)++;
    }
    if (swing) {
        acc = st->shake_neg ? (SWING_F32 | 0x80000000u) : SWING_F32;
    } else if (shake) {
        if (++st->shake_cnt <= 3) {
            acc = st->shake_neg ? (SWING_F32 | 0x80000000u) : SWING_F32;
        } else {
            st->shake_neg ^= 1;
            st->shake_cnt = 0;
        }
    } else {
        st->shake_cnt = 0;
    }

    /* A pad owns the motion inputs outright. Bongos have no sticks and no
     * accelerometer, so the Wii Remote (and Nunchuk, if there is one) keeps
     * working next to them: only claps and drum runs are added on top. */
    if (!bongo) {
        if (in_bubble()) {
            /* bubble: the Wii Remote's tilt steers; left stick stands in */
            W(k, 0x0C) = f32_64ths(-sx);
        } else {
            W(k, 0x0C) = acc;
            W(k, 0x10) = acc;
            W(k, 0x14) = acc;
        }
    } else if (swing) {
        W(k, 0x0C) = acc;
        W(k, 0x10) = acc;
        W(k, 0x14) = acc;
    }
    if (!bongo || swing || libdev != 1) {
        W(k, 0x68) = W(k, 0x0C);        /* Nunchuk acceleration mirrors the Wii Remote */
        W(k, 0x6C) = W(k, 0x10);
        W(k, 0x70) = W(k, 0x14);
    }
    if (!bongo || sx || libdev != 1) {
        W(k, 0x60) = f32_64ths(sx);     /* Nunchuk stick */
        W(k, 0x64) = f32_64ths(sy);
    }
    k[0x5C] = 1;                        /* Wii Remote + Nunchuk */
    k[0x5D] = 0;
    return;

idle:
    st->prev_btn = 0;
    st->newb = 0;
    st->hit_l = st->hit_r = 0;
    st->move_until = st->jump_until = 0;
    st->last_hold = W(k, 0x00);
    st->base_hold = st->last_hold;
}
