/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Chromatic launcher "Pocket link" mode (docs/Design-notes.md, phase 2).
 *
 * The Pocket, on the other end of the link cable, speaks the PJB1 request/reply
 * protocol of scripts/link_gateway.py (shared/bridge_rpc.h). This module is the gateway: it
 * takes the Pocket's framed requests from the FPGA bridge (rtl/chromatic_pocket_link.v,
 * status packets on UART address 0x0A), asks the owner's Jellyfin server with the launcher's
 * existing HTTP request path (the same paths jf_app.h uses, parsed with jf_parse_item), and
 * answers with the Gateway's public item form: opaque integer handles, titles, badges, art
 * keys and RGB565 artwork decoded on the console. For playback it opens the same MJPEG +
 * IMA ADPCM transcode the Chromatic's own player uses, demuxes the AVI with jf_avi.h and
 * repacks it as PJAV packets (shared/av_stream.h), the stream the Pocket already decodes.
 * The Pocket never sees the server address, the token, or a server item ID.
 *
 * Portable C, no allocation, no I/O of its own; the platform supplies the callbacks in
 * pj_plink_ops. Host-tested by chromatic/launcher/check_pocket_link.c and the two-sided
 * simulations pocket/check_link_cache.c and pocket/check_tether.c.
 * Memory (launcher 1.3.0): one pj_plink is about 40 KB (heap, never on a task stack); it is only alive while
 * the Pocket-link screen is up, when the Jellyfin app is not. Three users share one scratch area (pj_plink_scratch:
 * JSON tokens while a server reply is parsed, one decoded artwork window, the PJAV ring while streaming), item ids
 * are kept as 16 bytes, and the reply / AVI frame buffer is 12 KB (pages of 6, as the console's own app). The
 * budget against the console's measured heap is in docs/Design-notes.md ("Launcher 1.3.0").
 * Launcher 1.3.7: the same memory in four heap pieces (pj_plink ~11.8 KB, buf 12 KB, response ~8 KB, scratch 8 KB on
 * the ESP32), none above PJ_PLINK_PIECE_LIMIT: the console refused the one 40,464-byte block with 90 KB free but no
 * free block larger than 36,864 B ("Launcher 1.3.7: gateway allocation split").
 *
 * Wire to the FPGA (stock 0x8F packets, CRC-8 J1850, launcher_logic.h pj_crc8):
 *   -> 0x1E ctrl [cmd][key3..0]  cmd 1 enter, 2 leave, 3 heartbeat (every 250 ms), 4 fast UART
 *   -> 0x1F data [1..64 bytes]   downstream link bytes; credit rule below
 *   <- 0x0A status [fast<<6|faults<<2|mode][n][level hi][level lo][6 bytes]  chromatic_pocket_link.v
 * Credit rule: the FPGA queue holds PJ_PLINK_QUEUE bytes; the MCU keeps
 * level_reported + bytes_sent_since_that_report + PJ_PLINK_MARGIN <= PJ_PLINK_QUEUE.
 * 1.3.6 (credit review): a status is read a launcher pass or a little more after the bridge wrote it, so "sent since
 * that report" starts from the previous pass, not from now: what the UART still held when the previous pj_plink_step
 * began (plus one whole packet for the one in flight: the bridge queues a packet at its last byte), everything handed
 * to the UART since, and what the margin lacks of three packets when the UART had work in the pass before that (a
 * status written up to ~200 UART byte times before that step, still on its way when the launcher read its UART; one
 * packet before credit fix). See pj_plink_fpga_packet.
 * Fast UART: once the bridge is in media mode and the platform can change its rate
 * (ops.set_baud), ctrl 4 is sent at 115,200; the FPGA switches at its next byte boundary; the MCU
 * switches after PJ_PLINK_FAST_SWITCH_MS and expects a status packet with the fast bit within
 * PJ_PLINK_FAST_CONFIRM_MS. Launcher 1.3.5: otherwise it returns to 115,200 and KEEPS the link
 * (PJ_PF_OFF, "slow"): the Pocket can still browse and sync pictures at the stock rate; HELLO then
 * advertises no stream and START is refused with a reason, because 115,200 cannot carry ~25 KB/s.
 * Before 1.3.5 the whole link mode ended ("The fast connection failed", retried every 3 s), which
 * is what the owner saw with the first 18.41 image, whose switch never came.
 * Stages (pj_plink_stage, for the launcher's diagnostics and the screen): 1 entering (ENTER sent,
 * waiting for mode 2), 2 fast UART handshake, 3 waiting for the Pocket, 4 linked, 5 streaming.
 */
#ifndef PJ_POCKET_LINK_H
#define PJ_POCKET_LINK_H
#include <stdio.h>
#include "launcher_logic.h"
#include "pocket_link_diag.h"
#include "../../jellyfin/jf_app.h"
#include "../../jellyfin/jf_avi.h"
#include "../../../shared/bridge_rpc.h"
#include "../../../shared/link_framing.h"
#include "../../../shared/pocket_tether.h"
#include "pocket_jpeg_fit.h" /* 1.3.9: server pictures re-coded and letterboxed for the Pocket's decoder */

#define PJ_TX_PLINK_CTRL 0x1eu
#define PJ_TX_PLINK_DATA 0x1fu
#define PJ_RX_PLINK_STATUS 0x0au
#define PJ_LINK_KEY 0x504a4c4bu
#define PJ_PLINK_QUEUE PJ_TETHER_QUEUE
#define PJ_PLINK_MARGIN PJ_TETHER_MARGIN
#define PJ_PLINK_DATA_BYTES PJ_TETHER_DATA_BYTES
#define PJ_PLINK_HANDLES 96u
#define PJ_PLINK_BUF 12288u                                        /* one server reply (JSON or a JPEG), or the AVI frame while streaming */
#define PJ_PLINK_PAGE 6u                                           /* BROWSE page: the owner's 12-item page was 18.4 KB, 6 fit 12 KB */
#define PJ_PLINK_JSON 8192u
#define PJ_PLINK_RESPONSE (PJ_RPC_HEADER+8u+PJ_PLINK_JSON)        /* one reply: header + Home JSON, art chunk or stream chunk */
#define PJ_PLINK_ART_WINDOW 4096u                                  /* every artwork is decoded and served in row windows of at most 4 KB */
#define PJ_PLINK_HERO_ROWS (PJ_PLINK_ART_WINDOW/(PJ_ART_BACKDROP_W*2u)) /* backdrop: 6 rows (3,840 B) per window */
#define PJ_PLINK_OVERVIEW 1000u                                    /* DETAILS overview characters (decoded into the reply's own tail) */
#define PJ_PLINK_UNPLUGGED_MS 9000u                                /* no Pocket request this long: the cable is out (the Pocket says HELLO every 5 s) */
#define PJ_PLINK_SYNC_RECENT_MS 3000u                              /* artwork asked for this recently: "syncing" */
#define PJ_PLINK_TOKENS 1024u
#define PJ_PLINK_HEARTBEAT_MS 250u
/* While the fast rate waits for the FPGA to confirm it (SWITCHED), heartbeats go every 50 ms: the FPGA confirms on
 * the first CRC-valid packet at the new rate and reverts on its own after 250 ms, so one lost or early packet must
 * not decide the handshake. */
#define PJ_PLINK_CONFIRM_HB_MS 50u
#define PJ_PLINK_STATUS_STALE_MS 1000u
/* 1.3.6 (the 18.41 bridge): the bridge may still be at 524,288 baud when a link starts: the launcher
 * restarted, or a leave was lost, in mid-session. It then hears nothing said at 115,200 until its own heartbeat guard
 * returns it to the game and to 115,200, up to 3 s after the last heartbeat it understood. So ENTER is said again
 * every PJ_PLINK_ENTER_MS while arming, always at 115,200, and arming gives up only after PJ_PLINK_ARM_MS, well past
 * those 3 s: the first ENTER after the guard expired is answered and the owner never sees a failure for it. (Was one
 * ENTER and 1.5 s: "no reply from the FPGA bridge" was shown for 3 s before the retry could work.) An ENTER the
 * bridge hears in media mode only clears its fault bits. */
#define PJ_PLINK_ARM_MS 4500u
#define PJ_PLINK_ENTER_MS 500u
/* 1.3.6 (same review): the bridge does not send again a status packet it had already built when the rates change, so
 * up to 6 upstream bytes can be lost at the switch. The fast rate is therefore asked for only while nothing is owed:
 * no request being worked, no reply being sent (never, whatever the wait), and for the first PJ_PLINK_FAST_HOLD_MS
 * of a link also only once the cable has been quiet for PJ_PLINK_FAST_QUIET_MS: no upstream byte, nothing of a reply
 * still on its way down. After the hold the quiet is no longer waited for (a Pocket that never pauses must not keep
 * the link at 115,200 for good); the request then goes out between a reply and the next request. */
#define PJ_PLINK_FAST_QUIET_MS 50u
#define PJ_PLINK_FAST_HOLD_MS 2000u
/* 1.3.6: "nothing owed" did not count what still waits in the MCU's own transmit ring: a reply's tail
 * stays there after its last packet was handed over (up to 100 ms at 115,200). The request for the fast rate was
 * queued behind that tail and the MCU changed its rate 5 ms later, so the tail and the request itself left at the
 * wrong rate, the bridge never switched, and the link stayed at 115,200 without playback. Now the request is queued
 * only into an empty ring (x.tx_pending: ring, FIFO and all), and the MCU changes its rate only once the ring is
 * empty again: the request has left, whole, at 115,200. Nothing else is queued in between (no heartbeat, no data;
 * the launcher holds its own FPGA messages back: pj_plink_rate_changing). A ring that is still not empty
 * PJ_PLINK_FAST_DRAIN_MS after the request (the UART is stuck) ends the try with no rate change at all: the link
 * goes on at 115,200 (PJ_PF_OFF), as when the bridge does not confirm, with a reason of its own ("fast UART request
 * not sent"; the bridge not confirming stays "fast UART not confirmed"). */
#define PJ_PLINK_FAST_DRAIN_MS 200u
#define PJ_PLINK_ROW_LIMIT 6u
#define PJ_PLINK_FAST_SWITCH_MS 5u
#define PJ_PLINK_FAST_CONFIRM_MS 500u
#define PJ_PLINK_RING 4096u
#define PJ_PLINK_READ_MAX (PJ_PLINK_JSON+8u-16u)                   /* READ payload after its 16-byte header */
#define PJ_PLINK_READ_WAIT_MS 150u                                 /* hold a READ this long for data before answering "nothing yet" */
#define PJ_PLINK_READ_FILL 2048u                                   /* answer early once this much waits */
#define PJ_PLINK_NET 512u
#define PJ_PLINK_AUDIO_BLOCK_MAX 1024u
#define PJ_PLINK_FIT_FAILS 30u /* 1.3.9: this many pictures in a row the fit refuses (pocket_jpeg_fit.h) end the stream with a reason */
/* 1.4.0 (first real playback at 24 fps; every READ came back with 4,096 bytes, the whole ring, and
 * took ~200 ms end to end, so the Pocket got 20 KB/s of the ~30 it needed: "laggy", sound gaps every second). Three
 * changes, docs/Design-notes.md "Launcher 1.4.0":
 *  - a READ is answered with up to PJ_PLINK_READ_MAX (8,184) bytes, not one ring: what the ring holds is taken, the
 *    ring is filled again from the stream (at most PJ_PLINK_READ_REFILLS times) and taken again, so each round trip
 *    (request up the cable, the launcher's pass, the reply's turnaround) carries twice as much;
 *  - at most PJ_PLINK_FITS_PER_STEP pictures are fitted (pocket_jpeg_fit.h) in one step, so a pass that refills the
 *    ring is not stretched by a run of fits;
 *  - the downstream packets go out at the start of the step as well as at its end, so the UART has its bytes while
 *    the step fits pictures (the credit rule is unchanged: both are the same loop). */
#define PJ_PLINK_FITS_PER_STEP 2u
#define PJ_PLINK_READ_REFILLS 3u
/* A host model's hook after each fitted picture (check_tether_app.c charges it model time); nothing on the console. */
#ifndef PJ_PLINK_FIT_DONE
#define PJ_PLINK_FIT_DONE(l) ((void)0)
#endif
/* 1.4.0: the 5 s "plink speed" line (pj_plink_say_speed): what the stream really got, so the next console test tells
 * where the time goes. Counts since the last line; reset by the line. */
typedef struct {
    uint32_t reads,read_bytes,read_empty,read_max; /* READs answered with data / their bytes / answered with none; largest */
    uint32_t net_bytes,fits,fit_us_sum,fit_us_max; /* server bytes taken from the stream; pictures fitted, their time */
    uint32_t pass_ms_max,passes,passes_long,step_us_max;  /* gaps between steps: longest, count, over 15 ms; longest step */
    uint32_t ring_sum,ring_max,ring_samples,down_base,fit_drop_base,since_ms,last_step_ms;
    uint32_t status_base,stall_credit,stall_room; /* status packets at the last line; sends stopped by credit / UART room */
    uint32_t ack_base; /* 1.5.0: ACKs taken at the last line */
} pj_plink_speed;

enum {PJ_PL_OFF,PJ_PL_ARMING,PJ_PL_READY,PJ_PL_FAILED};
enum {PJ_PW_NONE,PJ_PW_VIEWS,PJ_PW_RESUME,PJ_PW_NEXTUP,PJ_PW_LATEST,PJ_PW_BROWSE,PJ_PW_ART,PJ_PW_DETAILS,PJ_PW_READ};
enum {PJ_PF_SLOW,PJ_PF_REQUESTED,PJ_PF_SWITCHED,PJ_PF_FAST,PJ_PF_OFF}; /* OFF: the fast rate failed this session; 115,200 kept */
enum {PJ_PST_OFF,PJ_PST_ENTER,PJ_PST_FAST,PJ_PST_POCKET,PJ_PST_LINKED,PJ_PST_STREAM};
enum {PJ_PS_NONE,PJ_PS_OPEN,PJ_PS_ENDED,PJ_PS_FAILED};
enum {PJ_PE_NONE,PJ_PE_CONFIG,PJ_PE_VIDEO,PJ_PE_AUDIO,PJ_PE_END};

typedef struct {
    void *ctx;
    /* GET `path` (relative to the server URL; the platform adds Authorization). 0 when started;
     * the platform then calls pj_plink_reply_data for the body and pj_plink_reply once. */
    int (*request)(void *ctx,const char *path);
    /* Baseline JPEG fitted into w x h RGB565; only rows [row0, row0+rows) are written to dst
     * (rows `stride` pixels apart, dst[0] is row row0). 0 on success. */
    int (*decode)(void *ctx,const uint8_t *jpeg,size_t size,uint16_t *dst,int stride,int w,int h,int row0,int rows);
    /* One complete 0x8F packet to the FPGA UART. 1 queued, 0 the UART is full (offered again). */
    int (*fpga_send)(void *ctx,const uint8_t *packet,size_t size);
    /* Playback (optional: NULL means HELLO advertises no stream). stream_open opens the video
     * transcode at `path` (relative; the platform adds deviceId and Authorization) on its own
     * connection; stream_read returns bytes available now (>0), 0 none yet, -1 ended, -2 failed. */
    int (*stream_open)(void *ctx,const char *path);
    int (*stream_read)(void *ctx,uint8_t *buf,size_t max);
    void (*stream_close)(void *ctx);
    /* Optional: UART1 rate. fast=1 selects PJ_TETHER_UART_FAST_BAUD, 0 the stock 115,200. The
     * platform waits for its transmit queue to drain before changing. */
    void (*set_baud)(void *ctx,int fast);
} pj_plink_ops;
/* Optional platform extras (launcher 1.3.1), set with pj_plink_extras after pj_plink_init (kept out of pj_plink_ops so
 * every existing caller's initialiser stays complete). send_sure: one packet that must not be dropped (the leave): the
 * platform waits for its transmit path to drain, queues the packet, and waits for it to go out; 1 sent. Without it
 * fpga_send is used. tx_pending: bytes the platform has queued for the FPGA UART but not yet put on the wire (its TX
 * ring and FIFO): a status packet's queue level does not include them, so they are counted against the credit. */
typedef struct {
    int (*send_sure)(void *ctx,const uint8_t *packet,size_t size);
    size_t (*tx_pending)(void *ctx);
    uint32_t (*micros)(void *ctx); /* 1.4.0, optional: a microsecond clock, for the picture fit's time in "plink speed" */
    /* 1.4.0, optional: how long the FPGA UART's line has been idle, in microseconds (0 while anything waits or is being
     * sent). See PJ_PLINK_IDLE_BYTES. */
    uint32_t (*idle_us)(void *ctx);
} pj_plink_extra;
/* 1.4.0 (credit, long passes): the allowance for a status written shortly before the previous step (pj_plink_fpga_packet,
 * `busy`, three packets less the margin) covers bytes that left the UART between that status and the step. When the
 * line has been idle for PJ_PLINK_IDLE_BYTES byte times at the step's start (twice the ~200 the allowance was sized
 * for), no byte left in that window and the allowance is not needed. This only happens when a pass is longer than the
 * UART needs for what the pass before handed it (a 20 ms pass: 504 bytes are 9.6 ms at 524,288 baud); with 10 ms passes
 * at the fast rate the line never idles that long and the rule is the 1.3.6 one. On the console the 4 KB READ took
 * ~200 ms (5 a second) where the 10 ms model takes ~114: passes nearer 20 ms, where the allowance cost a fifth of the
 * stream. */
#ifndef PJ_PLINK_IDLE_BYTES /* a host check's negative control may set it lower */
#define PJ_PLINK_IDLE_BYTES 400u
#endif
/* 1.5.0 (Pocket-acknowledged credit, shared/pocket_tether.h has the unit and the frame): the Pocket's ACKs say how
 * many of the bytes this launcher handed the bridge (`down_total`, 0x1F payload bytes, this session) have certainly
 * left the bridge (`acked`: a reply's end in that count plus the bytes the Pocket took since it). The bridge's queue
 * then holds at most down_total - acked; a late ACK only makes that larger. While an ACK moved that bound within the
 * last PJ_PLINK_ACK_FRESH_MS it replaces level + sent_since in the credit rule (pj_plink_unsure); otherwise, and
 * always with a Pocket app that sends none, the 1.4.0 rule is used unchanged. PJ_PLINK_ACK_LOG replies are
 * remembered by sequence with where they end (the first sending of each: a repeat only ends later). */
#define PJ_PLINK_ACK_FRESH_MS 200u
#define PJ_PLINK_ACK_LOG 8u

/* Item and image ids are 32 hex digits on the server; kept here as 16 bytes (all zero: none). */
typedef struct {uint8_t id[16],thumb_id[16],poster_id[16],hero_id[16];uint8_t thumb_kind,poster_kind,hero_kind,type,folder;} pj_plink_handle;
/* One area, three users that never overlap in time: a server reply's JSON tokens (only inside the parse), the
 * decoded window of the current artwork (art_valid is cleared by every parse), the PJAV ring (only while a stream
 * is open: HOME/BROWSE/DETAILS close the stream first, ART is refused while it plays). */
typedef union {
    pj_json_token tokens[PJ_PLINK_TOKENS];
    uint16_t art[PJ_PLINK_ART_WINDOW/2];
    uint8_t ring[PJ_PLINK_RING];
    /* 1.3.9: while a title plays, the picture fit's tables (pocket_jpeg_fit.h) live behind the ring: no new memory */
    struct {uint8_t ring_room[PJ_PLINK_RING];pj_fit_work work;} fit;
} pj_plink_scratch;
_Static_assert(sizeof(((pj_plink_scratch *)0)->fit)<=sizeof(((pj_plink_scratch *)0)->tokens),"the fit's tables must not grow the scratch area");

typedef struct {
    pj_plink_ops io;pj_plink_extra x;char user[33];
    uint8_t state,work,http_busy,cached,tx_active,fast,fpga_mode,fpga_faults,fpga_fast;
    /* http_stale: the Pocket moved on while a server request was still out; its answer is dropped, and no new request
     * (nor a stream, whose frames use `buf`) starts until it has come back. */
    uint8_t http_stale;
    /* For the launcher's status line (pj_plink_view): the Pocket's last request, and the artwork of the last Home. */
    uint8_t pocket_seen;uint32_t pocket_ms,sync_ms;uint16_t sync_total,sync_done;
    uint32_t clear_ms; /* 1.3.1: the last ENTER that cleared a cable timeout (a Pocket unplugged mid-byte) */
    /* 1.3.6: the last ENTER while arming; since when the cable has been quiet; when the bridge entered media mode */
    uint32_t again_ms,quiet_ms,ready_ms;
    uint32_t now,enter_ms,status_ms,heartbeat_ms,fast_ms,level,sent_since;
    /* 1.3.6 (credit review), sampled by pj_plink_step: wire bytes the platform still held for the FPGA UART when the
     * pass's step began (x.tx_pending; 0 without it), payload bytes handed to it in that step, and whether the UART
     * had anything to send in the pass before (it held bytes when that pass's step began, or was handed some). */
    uint32_t held,pass_sent,busy;
    /* 1.5.0 (PJ_PLINK_ACK_FRESH_MS above): payload bytes handed to the UART this session, the newest acknowledged
     * byte and when, the replies sent (sequence, end in down_total) and the ACKs taken / refused (bad check, end past
     * down_total) / passed over (older than the one held, or a reply no longer remembered). */
    uint32_t down_total,acked,ack_ms,ack_good,ack_bad,ack_old;uint8_t ack_have,ack_log_at;
    struct {uint32_t seq,end;uint8_t used;} ack_log[PJ_PLINK_ACK_LOG];
    /* 1.3.5, for the diagnostics and the screen's reason: bridge status packets seen since init (0 at a failure to
     * enter means no word from the bridge at all: wrong image or UART), fast-rate attempts that failed. */
    uint32_t status_packets;uint8_t fast_failed;
    /* upstream request reassembly and the one outstanding request */
    pj_wire_rx upstream;uint8_t incoming[1040],request[1040];size_t request_size;
    /* downstream reply (PJ_PLINK_RESPONSE bytes) and its wire escaper */
    uint8_t *response;size_t response_size;pj_wire_tx tx;
    /* HTTP reply scratch (PJ_PLINK_BUF bytes; also the AVI frame buffer while streaming) and tokens.
     * 1.3.7: response, buf and scratch are separate blocks the platform owns (see pj_plink_init), not members:
     * the console's largest free block was 36,864 B, smaller than the 40,464 B one-block gateway of 1.3.6. */
    uint8_t *buf;size_t reply_size;pj_plink_scratch *scratch;
    /* Home assembly: rows are appended to `response` as the server answers */
    struct {char id[33],name[32];} views[JF_ROW_ITEMS];uint8_t view_count,view_next,rows_done;
    size_t json_at;
    /* handles */
    pj_plink_handle handles[PJ_PLINK_HANDLES];unsigned handle_count;
    /* decoded art for the current (handle, kind): one window (scratch.art) */
    uint32_t art_handle,art_total,art_window;uint8_t art_kind,art_valid,art_jpeg;
    uint32_t art_req_handle,art_req_offset;uint8_t art_req_kind;
    /* Pictures the server said it does not have (404), by image id and kind: asked again (a re-sync, or another row
     * showing the same item) they answer "no art" without a server request. Eight, oldest replaced; for this session. */
    uint8_t noart[8][17];uint8_t noart_next;
    /* details request */
    uint32_t details_handle;
    /* playback: AVI in, PJAV ring out */
    uint8_t stream_state,emit_kind,config_sent,frame_pending;
    uint32_t stream_session,pk_seq,cfg_w,cfg_h,cfg_fps,frame_us,audio_block;
    uint64_t stream_offset,video_index,audio_samples,read_offset;
    unsigned ring_head,ring_count; /* the ring is scratch.ring */
    uint8_t emit_header[PJ_AV_HEADER];unsigned emit_at,emit_size;const uint8_t *emit_body;
    uint8_t ablock[PJ_PLINK_AUDIO_BLOCK_MAX];unsigned ablock_used;
    uint8_t net[PJ_PLINK_NET];size_t net_size,net_used;
    uint32_t read_ms;jf_avi avi;
    /* 1.3.9: pictures the fit refused (pocket_jpeg_fit.h), in a row and since the stream opened */
    uint32_t fit_fails,fit_drops;
    pj_plink_speed sp; /* 1.4.0: the "plink speed" line's counts */
    const char *why;
    /* 1.3.6: what the link heard (pocket_link_diag.h). Kept across pj_plink_init, so the launcher's 3 s retry does
     * not wipe the evidence; zero in a new (calloc'd) object. */
    pj_plink_diag d;
} pj_plink;
/* 1.3.7: the gateway is four heap pieces (pj_plink itself, buf, response, scratch), none larger than
 * PJ_PLINK_PIECE_LIMIT, so it starts on a fragmented heap; together they are what the one block of 1.3.6 was. */
#define PJ_PLINK_PIECE_LIMIT 24576u
#define PJ_PLINK_MEMORY (sizeof(pj_plink)+PJ_PLINK_BUF+PJ_PLINK_RESPONSE+sizeof(pj_plink_scratch))
_Static_assert(sizeof(pj_plink)<=PJ_PLINK_PIECE_LIMIT&&PJ_PLINK_BUF<=PJ_PLINK_PIECE_LIMIT&&
               PJ_PLINK_RESPONSE<=PJ_PLINK_PIECE_LIMIT&&sizeof(pj_plink_scratch)<=PJ_PLINK_PIECE_LIMIT,"gateway piece too large");
static inline size_t pj_plink_piece_max(void) {
    size_t m=sizeof(pj_plink);
    if(m<PJ_PLINK_BUF)m=PJ_PLINK_BUF;
    if(m<PJ_PLINK_RESPONSE)m=PJ_PLINK_RESPONSE;
    if(m<sizeof(pj_plink_scratch))m=sizeof(pj_plink_scratch);
    return m;
}

/* ---- FPGA packets ---- */
static inline size_t pj_plink_encode(uint8_t *out,unsigned address,const uint8_t *payload,size_t n) {
    out[0]=PJ_FPGA_MARK_V2;out[1]=(uint8_t)address;out[2]=(uint8_t)n;memcpy(out+3,payload,n);
    out[3+n]=pj_crc8(out,3+n);return 4+n;
}
static inline int pj_plink_ctrl(pj_plink *l,unsigned cmd) {
    uint8_t p[5]={(uint8_t)cmd,PJ_LINK_KEY>>24,(PJ_LINK_KEY>>16)&255,(PJ_LINK_KEY>>8)&255,PJ_LINK_KEY&255},m[16];
    return l->io.fpga_send(l->io.ctx,m,pj_plink_encode(m,PJ_TX_PLINK_CTRL,p,5));
}
/* A control packet that must arrive (the leave): through send_sure when the platform has it. */
static inline int pj_plink_ctrl_sure(pj_plink *l,unsigned cmd) {
    uint8_t p[5]={(uint8_t)cmd,PJ_LINK_KEY>>24,(PJ_LINK_KEY>>16)&255,(PJ_LINK_KEY>>8)&255,PJ_LINK_KEY&255},m[16];
    size_t n=pj_plink_encode(m,PJ_TX_PLINK_CTRL,p,5);
    return l->x.send_sure?l->x.send_sure(l->io.ctx,m,n):l->io.fpga_send(l->io.ctx,m,n);
}
/* Leaves media mode at whatever rate the FPGA may be at, and ends at 115,200. At the rate the MCU
 * is at first; after a fast-UART request the FPGA may be fast before the MCU (REQUESTED) or still slow after it
 * (SWITCHED, unconfirmed), so the leave is said at both rates, the MCU ending at 115,200 either way. Each is a
 * send_sure: drained, sent, drained. The FPGA also returns to 115,200 by itself when no valid packet arrives at the
 * new rate within 250 ms, and a lost leave still ends with the 3 s heartbeat expiry. */
static inline void pj_plink_leave(pj_plink *l) {
    pj_plink_ctrl_sure(l,PJ_TETHER_CTRL_LEAVE);
    if(l->io.set_baud&&l->fast!=PJ_PF_SLOW&&l->fast!=PJ_PF_OFF) { /* OFF: both ends are at 115,200 already */
        if(l->fast==PJ_PF_REQUESTED){l->io.set_baud(l->io.ctx,1);pj_plink_ctrl_sure(l,PJ_TETHER_CTRL_LEAVE);}
        l->io.set_baud(l->io.ctx,0);pj_plink_ctrl_sure(l,PJ_TETHER_CTRL_LEAVE);
    }
    l->fast=PJ_PF_SLOW;
}
/* 1.3.6: the request for the fast rate is out and the MCU has not changed its rate yet: the bridge
 * may be at either. Nothing else may be said to the FPGA until this is over (the launcher's own button requests and
 * media bits wait for the next loop pass; the gateway's heartbeats and data wait in pj_plink_step). */
static inline int pj_plink_rate_changing(const pj_plink *l){return l->fast==PJ_PF_REQUESTED;}
/* A second, independent V2 frame parser for the FPGA UART bytes: the launcher's own
 * pj_fpga_rx_byte keeps only Buttons and Version; this one hands every frame over. */
/* 1.3.6: three counters, so a lost or damaged packet is seen: status packets (0x0A) whose CRC failed, other
 * packets whose CRC failed, and length bytes above the largest payload. They live as long as the parser object;
 * pj_plink_rx_reset starts a new frame and keeps them. */
typedef struct {uint8_t buffer[16],state,need;unsigned used;uint32_t crc_status,crc_other,bad_len;} pj_plink_rx;
static inline void pj_plink_rx_reset(pj_plink_rx *r){r->state=0;r->need=0;r->used=0;}
static inline int pj_plink_rx_byte(pj_plink_rx *r,uint8_t byte,unsigned *address,const uint8_t **payload,size_t *n) {
    switch(r->state) {
    case 0:if(byte!=PJ_FPGA_MARK_V2)return 0;r->buffer[0]=byte;r->used=1;r->state=1;return 0;
    case 1:r->buffer[r->used++]=byte;r->state=2;return 0;
    case 2:if(byte>PJ_FPGA_MAX_PAYLOAD){r->state=0;r->bad_len++;return 0;}r->buffer[r->used++]=byte;r->need=byte;r->state=byte?3:4;return 0;
    case 3:r->buffer[r->used++]=byte;if(--r->need==0)r->state=4;return 0;
    default:
        r->buffer[r->used++]=byte;r->state=0;
        if(pj_crc8(r->buffer,r->used)){if(r->buffer[1]==PJ_RX_PLINK_STATUS)r->crc_status++;else r->crc_other++;return 0;}
        *address=r->buffer[1];*payload=r->buffer+3;*n=r->buffer[2];return 1;
    }
}

/* ---- JSON writer into `response` ---- */
static inline int pj_plink_put(pj_plink *l,const char *s,size_t n) {
    if(l->json_at+n>PJ_PLINK_RESPONSE)return 0;
    memcpy(l->response+l->json_at,s,n);l->json_at+=n;return 1;
}
static inline int pj_plink_puts(pj_plink *l,const char *s){return pj_plink_put(l,s,strlen(s));}
static inline int pj_plink_put_u64(pj_plink *l,uint64_t v) {
    char d[24];int n=snprintf(d,sizeof(d),"%llu",(unsigned long long)v);return n>0&&pj_plink_put(l,d,(size_t)n);
}
static inline int pj_plink_put_string(pj_plink *l,const char *s) {
    if(!pj_plink_put(l,"\"",1))return 0;
    for(;*s;s++) {
        unsigned char c=(unsigned char)*s;
        if(c=='"'||c=='\\'){char e[2]={'\\',(char)c};if(!pj_plink_put(l,e,2))return 0;}
        else if(c<0x20){char e[8];snprintf(e,sizeof(e),"\\u%04x",c);if(!pj_plink_put(l,e,6))return 0;}
        else if(!pj_plink_put(l,(const char *)&c,1))return 0;
    }
    return pj_plink_put(l,"\"",1);
}
/* 32 lower-case hex digits <-> 16 bytes. pack: 0 (and zeros) when `hex` is not such an id. */
static inline int pj_plink_pack(uint8_t out[16],const char *hex) {
    memset(out,0,16);if(!jf_hex_id(hex))return 0;
    for(unsigned i=0;i<16;i++){unsigned v=0;for(unsigned k=0;k<2;k++){char c=hex[2*i+k];v=v*16u+(unsigned)(c<='9'?c-'0':c-'a'+10);}out[i]=(uint8_t)v;}
    return 1;
}
static inline const char *pj_plink_unpack(char out[33],const uint8_t in[16]) {
    static const char digits[]="0123456789abcdef";
    for(unsigned i=0;i<16;i++){out[2*i]=digits[in[i]>>4];out[2*i+1]=digits[in[i]&15];}
    out[32]=0;return out;
}
static inline unsigned pj_plink_handle_of(pj_plink *l,const jf_item *thumb,const jf_item *poster) {
    uint8_t id[16];if(!pj_plink_pack(id,thumb->id))return 0;
    for(unsigned i=0;i<l->handle_count;i++)if(!memcmp(l->handles[i].id,id,16)) {
        pj_plink_handle *h=&l->handles[i];
        if(thumb->card_kind&&pj_plink_pack(h->thumb_id,thumb->card_id))h->thumb_kind=thumb->card_kind;
        if(poster->card_kind&&pj_plink_pack(h->poster_id,poster->card_id))h->poster_kind=poster->card_kind;
        if(thumb->hero_kind&&pj_plink_pack(h->hero_id,thumb->hero_id))h->hero_kind=thumb->hero_kind;
        return i+1;
    }
    if(l->handle_count>=PJ_PLINK_HANDLES)return 0;
    pj_plink_handle *h=&l->handles[l->handle_count++];memset(h,0,sizeof(*h));
    memcpy(h->id,id,16);h->type=thumb->type;
    if(pj_plink_pack(h->thumb_id,thumb->card_id))h->thumb_kind=thumb->card_kind;
    if(pj_plink_pack(h->poster_id,poster->card_id))h->poster_kind=poster->card_kind;
    if(pj_plink_pack(h->hero_id,thumb->hero_id))h->hero_kind=thumb->hero_kind;
    h->folder=(uint8_t)(thumb->type==JF_T_SERIES||thumb->type==JF_T_SEASON||thumb->type==JF_T_FOLDER);
    return l->handle_count;
}
static inline const char *pj_plink_type_name(uint8_t type) {
    static const char *const names[]={"Other","Movie","Episode","Series","Season","Folder","Video"};
    return type<7?names[type]:"Other";
}
/* The image a given art kind would serve for a handle (id + server image kind), and its key. */
static inline const char *pj_plink_image_for(const pj_plink_handle *h,unsigned kind,uint8_t *image_kind,char id[33]) {
    if(kind==PJ_ART_THUMB){*image_kind=h->thumb_kind;return pj_plink_unpack(id,h->thumb_id);}
    if(kind==PJ_ART_POSTER){*image_kind=h->poster_kind;return pj_plink_unpack(id,h->poster_id);}
    *image_kind=h->hero_kind;return pj_plink_unpack(id,h->hero_id);
}
static inline uint32_t pj_plink_art_key_of(const pj_plink_handle *h,unsigned kind) {
    uint8_t img;char hex[33];const char *id=pj_plink_image_for(h,kind,&img,hex);
    return img&&jf_hex_id(id)?pj_tether_art_key(id,img):0;
}
/* One Jellyfin item object -> one public item in the JSON being built. 0 when it does not fit.
 * "art" is the key of the picture this row's shape shows, "hero" that of its backdrop. */
static inline int pj_plink_put_item(pj_plink *l,const pj_json *j,int obj,int first,unsigned shape) {
    jf_item thumb,poster;
    if(!jf_parse_item(j,obj,&thumb,JF_SHAPE_THUMB)||!jf_parse_item(j,obj,&poster,JF_SHAPE_POSTER))return 1; /* skip */
    unsigned handle=pj_plink_handle_of(l,&thumb,&poster);
    if(!handle)return 1; /* table full: the item is left out, as the Gateway truncates */
    const pj_plink_handle *h=&l->handles[handle-1];
    int ok=(first||pj_plink_puts(l,","))&&pj_plink_puts(l,"{\"handle\":")&&pj_plink_put_u64(l,handle)&&
        pj_plink_puts(l,",\"title\":")&&pj_plink_put_string(l,thumb.name)&&
        pj_plink_puts(l,",\"subtitle\":")&&pj_plink_put_string(l,thumb.sub)&&
        pj_plink_puts(l,",\"type\":\"")&&pj_plink_puts(l,pj_plink_type_name(thumb.type))&&
        pj_plink_puts(l,"\",\"folder\":")&&pj_plink_puts(l,h->folder?"true":"false")&&
        pj_plink_puts(l,",\"duration_ticks\":")&&pj_plink_put_u64(l,(uint64_t)thumb.runtime_s*10000000u)&&
        pj_plink_puts(l,",\"resume_ticks\":")&&pj_plink_put_u64(l,(uint64_t)thumb.resume_s*10000000u)&&
        pj_plink_puts(l,",\"played\":")&&pj_plink_puts(l,thumb.played?"true":"false")&&
        pj_plink_puts(l,",\"art\":")&&pj_plink_put_u64(l,pj_plink_art_key_of(h,shape))&&
        pj_plink_puts(l,",\"hero\":")&&pj_plink_put_u64(l,pj_plink_art_key_of(h,PJ_ART_BACKDROP));
    if(ok&&thumb.year){ok=pj_plink_puts(l,",\"year\":")&&pj_plink_put_u64(l,thumb.year);}
    return ok&&pj_plink_puts(l,"}");
}
/* The current HTTP reply ({"Items":[...]} or a bare array) -> `limit` public items. -1 bad reply. */
static inline int pj_plink_put_items(pj_plink *l,unsigned limit,uint32_t *total,unsigned shape) {
    pj_json j;l->art_valid=0; /* the tokens take the scratch area */
    if(!pj_json_parse(&j,l->buf,l->reply_size,l->scratch->tokens,PJ_PLINK_TOKENS))return -1;
    int list=0;
    if(j.tokens[0].type==PJ_JSON_OBJECT) {
        list=pj_json_find(&j,0,"Items");uint64_t t;
        if(total&&pj_json_u64(&j,pj_json_find(&j,0,"TotalRecordCount"),&t))*total=(uint32_t)t;
    }
    if(list<0||j.tokens[list].type!=PJ_JSON_ARRAY)return -1;
    int count=0;
    for(unsigned i=(unsigned)list+1;i<j.tokens[list].next&&(unsigned)count<limit;i=j.tokens[i].next) {
        if(j.tokens[i].type!=PJ_JSON_OBJECT)continue;
        size_t before=l->json_at;
        if(!pj_plink_put_item(l,&j,(int)i,count==0,shape))return -1;
        if(l->json_at!=before)count++; /* skipped items (no id, table full) are not counted */
    }
    return count;
}
static inline int pj_plink_row_open(pj_plink *l,const char *title,unsigned shape) {
    return (l->rows_done==0||pj_plink_puts(l,","))&&pj_plink_puts(l,"{\"title\":")&&pj_plink_put_string(l,title)&&
        pj_plink_puts(l,",\"shape\":")&&pj_plink_put_u64(l,shape)&&pj_plink_puts(l,",\"items\":[");
}

/* ---- replies to the Pocket ---- */
static inline void pj_plink_fail(pj_plink *l,const char *why){l->state=PJ_PL_FAILED;l->why=why;}
/* 1.5.0: a reply (re)starts on the wire: remember where it will end in down_total (its 7E flags and escapes
 * counted as pj_wire_tx_next makes them). A repeat of a sequence already remembered keeps the first, earlier end:
 * the Pocket may have taken either copy, and the earlier end can only make the bound larger. */
static inline void pj_plink_tx_begin(pj_plink *l) {
    pj_wire_tx_init(&l->tx,l->response,l->response_size);l->tx_active=1;
    uint32_t seq=pj_av_u32(l->response+8),end=l->down_total+2u;
    for(unsigned i=0;i<PJ_PLINK_ACK_LOG;i++)if(l->ack_log[i].used&&l->ack_log[i].seq==seq)return;
    for(size_t i=0;i<l->response_size;i++){uint8_t b=l->response[i];end+=1u+(b==PJ_WIRE_FLAG||b==PJ_WIRE_ESCAPE||b==PJ_WIRE_IDLE);}
    unsigned at=l->ack_log_at++%PJ_PLINK_ACK_LOG;
    l->ack_log[at].seq=seq;l->ack_log[at].end=end;l->ack_log[at].used=1;
}
/* 1.5.0: one ACK frame from the Pocket (pj_tether_ack_parse already checked it). */
static inline void pj_plink_ack(pj_plink *l,uint8_t seq,uint32_t count,uint32_t now) {
    int found=0;uint32_t end=0;
    for(unsigned i=0;i<PJ_PLINK_ACK_LOG;i++)
        if(l->ack_log[i].used&&(uint8_t)l->ack_log[i].seq==seq&&(!found||(int32_t)(l->ack_log[i].end-end)<0)){end=l->ack_log[i].end;found=1;}
    if(!found){l->ack_old++;return;}
    uint32_t at=end+count;
    if((int32_t)(l->down_total-at)<0){l->ack_bad++;return;}                /* more than was ever sent: not believed */
    l->ack_good++;
    if(l->ack_have&&(int32_t)(at-l->acked)<0){l->ack_old++;return;}        /* behind the one held: keep that */
    /* Fresh means the bound moved: an ACK that only repeats it leaves ack_ms alone. A Pocket whose anchor is the
     * second sending of a repeated reply is measured from the first (pj_plink_tx_begin), so its ACKs can stand still
     * below what really left; after PJ_PLINK_ACK_FRESH_MS the 1.4.0 rule lets the next reply out, and that reply is
     * the Pocket's next anchor. Without this a stalled bound would hold the reply back for ever (found by the model's
     * damaged-reply case). */
    if(!l->ack_have||at!=l->acked)l->ack_ms=now;
    l->acked=at;l->ack_have=1;
}
/* 1.5.0: the most the bridge's queue (and the UART on its way there) can hold: the ACK bound while ACKs are fresh,
 * else the 1.4.0 status rule. */
static inline uint32_t pj_plink_unsure(const pj_plink *l) {
    if(l->ack_have&&(uint32_t)(l->now-l->ack_ms)<=PJ_PLINK_ACK_FRESH_MS)return l->down_total-l->acked;
    return l->level+l->sent_since;
}
static inline void pj_plink_finish(pj_plink *l,size_t payload,int error) {
    /* PJB1 reply header over the payload already at response+16 */
    memcpy(l->response,"PJB1",4);l->response[4]=l->request[4];l->response[5]=(uint8_t)(error?3:1);
    l->response[6]=(uint8_t)payload;l->response[7]=(uint8_t)(payload>>8);memcpy(l->response+8,l->request+8,4);
    uint32_t crc=pj_av_crc_part(0xffffffffu,l->response,12);
    pj_put32(l->response+12,pj_av_crc_part(crc,l->response+16,payload)^0xffffffffu);
    l->response_size=PJ_RPC_HEADER+payload;l->cached=1;l->work=PJ_PW_NONE;
    pj_plink_tx_begin(l);l->d.replies++;
}
static inline void pj_plink_error(pj_plink *l,const char *text) {
    l->json_at=PJ_RPC_HEADER;pj_plink_puts(l,"{\"error\":");pj_plink_put_string(l,text);pj_plink_puts(l,"}");
    pj_plink_finish(l,l->json_at-PJ_RPC_HEADER,1);
}
static inline void pj_plink_get(pj_plink *l,uint8_t work,const char *path) {
    /* The reply lands in `buf`, so the artwork fetched there before is gone (ART fetches it again). */
    if(work!=PJ_PW_ART){l->art_jpeg=0;l->art_valid=0;l->art_handle=0;l->art_total=0;}
    if(l->http_stale){pj_plink_error(l,"Busy.");return;} /* an abandoned request is still out: the Pocket asks again */
    l->work=work;l->reply_size=0;
    if(l->io.request(l->io.ctx,path)!=0){pj_plink_error(l,"Request failed locally.");return;}
    l->http_busy=1;
}
/* Decode the window of the fetched JPEG (l->buf, l->reply_size) that holds `offset`. */
/* Rows per decode window for an art kind: as many whole rows as fit PJ_PLINK_ART_WINDOW. */
static inline unsigned pj_plink_window_rows(unsigned kind){return PJ_PLINK_ART_WINDOW/(pj_art_width(kind)*2u);}
static inline uint32_t pj_plink_window_bytes(unsigned kind){return pj_plink_window_rows(kind)*pj_art_width(kind)*2u;}
static inline void pj_plink_art_decode(pj_plink *l,uint32_t offset) {
    unsigned w=pj_art_width(l->art_req_kind),h=pj_art_height(l->art_req_kind);
    unsigned rows=pj_plink_window_rows(l->art_req_kind),window=offset/pj_plink_window_bytes(l->art_req_kind);
    if(l->art_valid&&l->art_window==window)return;
    unsigned row0=window*rows;if(row0+rows>h)rows=h-row0;
    l->art_valid=l->io.decode(l->io.ctx,l->buf,l->reply_size,l->scratch->art,(int)w,(int)w,(int)h,(int)row0,(int)rows)==0;
    l->art_window=window;
}
static inline void pj_plink_art_reply(pj_plink *l) {
    unsigned w=pj_art_width(l->art_req_kind),h=pj_art_height(l->art_req_kind);
    uint32_t total=l->art_jpeg?w*h*2u:0,offset=l->art_req_offset,window=pj_plink_window_bytes(l->art_req_kind);
    if(offset>total){pj_plink_error(l,"Invalid media offset.");return;}
    uint32_t size=total-offset;
    if(l->art_jpeg&&size){
        pj_plink_art_decode(l,offset);
        if(!l->art_valid){total=0;size=0;} /* undecodable: a valid "no art" reply */
    }
    uint32_t window_end=(offset/window+1u)*window;
    if(offset+size>window_end)size=window_end-offset;
    if(size>4096)size=4096;
    uint8_t *p=l->response+PJ_RPC_HEADER;
    pj_put32(p,total);p[4]=(uint8_t)w;p[5]=(uint8_t)(w>>8);p[6]=(uint8_t)h;p[7]=(uint8_t)(h>>8);
    uint32_t within=offset%window;
    if(size)memcpy(p+8,(const uint8_t *)l->scratch->art+within,size);
    l->art_total=total;
    pj_plink_finish(l,8+size,0);
}
static inline void pj_plink_next_home_step(pj_plink *l) {
    char path[220];
    while(l->view_next<l->view_count&&l->rows_done<JF_ROWS) {
        snprintf(path,sizeof(path),"/Items/Latest?userId=%s&parentId=%s&limit=%u&%s",l->user,l->views[l->view_next].id,PJ_PLINK_ROW_LIMIT,JF_FIELDS);
        pj_plink_get(l,PJ_PW_LATEST,path);return;
    }
    if(!pj_plink_puts(l,"],\"truncated\":false}")){pj_plink_error(l,"Home does not fit.");return;}
    pj_plink_finish(l,l->json_at-PJ_RPC_HEADER,0);
    /* For the status line: tiles with a picture (nonzero "art" keys) are what a sync fetches. */
    unsigned tiles=0;
    for(const uint8_t *s=l->response+PJ_RPC_HEADER,*e=l->response+l->response_size;s+7<e;s++)
        if(!memcmp(s,"\"art\":",6)&&s[6]!='0')tiles++;
    l->sync_total=(uint16_t)tiles;l->sync_done=0;l->sync_ms=l->now-PJ_PLINK_SYNC_RECENT_MS-1u; /* "syncing" once artwork is asked for */
}

/* ---- playback: AVI demux -> PJAV ring ---- */
static inline void pj_plink_stream_close(pj_plink *l) {
    /* stream_open was called for OPEN/ENDED/FAILED (any state but NONE), so close whenever
     * a connection exists, not only while it is still streaming. */
    if(l->stream_state!=PJ_PS_NONE&&l->io.stream_close)l->io.stream_close(l->io.ctx);
    l->stream_state=PJ_PS_NONE;l->ring_head=l->ring_count=0;l->emit_kind=PJ_PE_NONE;l->config_sent=0;l->frame_pending=0;
    l->ablock_used=0;l->net_size=l->net_used=0;
}
/* 1.4.0: the "plink speed" counts start again (a new title, a new line); the step's own pass timing carries on. */
static inline void pj_plink_speed_reset(pj_plink *l,uint32_t now) {
    pj_plink_speed *s=&l->sp;uint32_t last=s->last_step_ms;memset(s,0,sizeof(*s));
    s->since_ms=now;s->last_step_ms=last;s->down_base=l->d.down_bytes;s->fit_drop_base=l->fit_drops;s->status_base=l->status_packets;
    s->ack_base=l->ack_good;
}
static inline unsigned pj_plink_ring_free(const pj_plink *l){return PJ_PLINK_RING-l->ring_count;}
static inline void pj_plink_ring_put(pj_plink *l,const uint8_t *data,unsigned n) {
    for(unsigned i=0;i<n;i++){l->scratch->ring[(l->ring_head+l->ring_count)%PJ_PLINK_RING]=data[i];l->ring_count++;}
}
static inline void pj_plink_ring_take(pj_plink *l,uint8_t *out,unsigned n) {
    for(unsigned i=0;i<n;i++){out[i]=l->scratch->ring[l->ring_head];l->ring_head=(l->ring_head+1)%PJ_PLINK_RING;l->ring_count--;}
}
/* Begin emitting one PJAV packet: header built and CRC'd here, body copied into the ring as space frees. */
static inline void pj_plink_emit_begin(pj_plink *l,unsigned kind,unsigned flags,uint64_t pts,const uint8_t *body,unsigned size) {
    uint8_t *h=l->emit_header;memset(h,0,PJ_AV_HEADER);
    memcpy(h,"PJAV",4);h[4]=1;h[5]=(uint8_t)kind;h[6]=(uint8_t)flags;
    pj_put32(h+8,l->stream_session);pj_put32(h+12,kind==PJ_AV_CONFIG?0:l->pk_seq++);
    pj_put32(h+16,(uint32_t)pts);pj_put32(h+20,(uint32_t)(pts>>32));pj_put32(h+24,size);
    uint32_t crc=pj_av_crc_part(0xffffffffu,h,28);pj_put32(h+28,pj_av_crc_part(crc,body,size)^0xffffffffu);
    l->emit_kind=(uint8_t)kind;l->emit_at=0;l->emit_size=PJ_AV_HEADER+size;l->emit_body=body;
}
/* Copies what fits of the packet being emitted. 1 when the packet is complete. */
static inline int pj_plink_emit_step(pj_plink *l) {
    while(l->emit_at<l->emit_size&&pj_plink_ring_free(l)) {
        unsigned n=l->emit_size-l->emit_at,room=pj_plink_ring_free(l);if(n>room)n=room;
        if(l->emit_at<PJ_AV_HEADER){unsigned k=PJ_AV_HEADER-l->emit_at;if(n>k)n=k;pj_plink_ring_put(l,l->emit_header+l->emit_at,n);}
        else pj_plink_ring_put(l,l->emit_body+(l->emit_at-PJ_AV_HEADER),n);
        l->emit_at+=n;
    }
    if(l->emit_at<l->emit_size)return 0;
    if(l->emit_kind==PJ_PE_VIDEO||l->emit_kind==PJ_AV_VIDEO)l->frame_pending=0;
    if(l->emit_kind==PJ_AV_AUDIO)l->ablock_used=0;
    l->emit_kind=PJ_PE_NONE;return 1;
}
static inline uint64_t pj_plink_video_pts(const pj_plink *l,uint64_t index){return index*l->frame_us*9u/100u;}
static inline uint64_t pj_plink_audio_pts(const pj_plink *l){return l->audio_samples*45u/8u;} /* 16 kHz -> 90 kHz */
static inline size_t pj_plink_audio_sink(void *ctx,const uint8_t *data,size_t size,int first) {
    pj_plink *l=ctx;(void)first;
    if(!l->audio_block)return 0;
    size_t room=l->audio_block-l->ablock_used;if(size>room)size=room;
    memcpy(l->ablock+l->ablock_used,data,size);l->ablock_used+=(unsigned)size;return size;
}
static inline int pj_plink_stream_config(pj_plink *l) {
    const jf_avi *a=&l->avi;
    l->frame_us=jf_avi_frame_us(a);
    if(!a->width||!a->height||!l->frame_us||!a->audio_format)return 0; /* headers not complete yet */
    /* 1.3.9: the server keeps the title's shape (a 16:9 title asked for at 128x96 comes as 128x72); every picture is
     * letterboxed to the Pocket's height on the way (pocket_jpeg_fit.h, pj_plink_stream_step). What the Pocket is told,
     * and checked here as before, is the size it will get. */
    unsigned out_h=pj_fit_height(a->width,a->height);
    if(!out_h||!pj_tether_geometry_ok(a->width,out_h)){l->stream_state=PJ_PS_FAILED;l->why="picture size not supported on the Pocket";return -1;}
    if(a->audio_format!=0x11||a->audio_channels!=1||a->audio_rate!=16000||
       (a->audio_block!=128&&a->audio_block!=256&&a->audio_block!=512&&a->audio_block!=1024)){l->stream_state=PJ_PS_FAILED;l->why="sound format not supported on the Pocket";return -1;}
    l->cfg_w=a->width;l->cfg_h=out_h;l->cfg_fps=pj_tether_fps_field(l->frame_us);l->audio_block=a->audio_block;
    static uint8_t cfg[20];memset(cfg,0,sizeof(cfg));
    cfg[0]=(uint8_t)l->cfg_w;cfg[1]=(uint8_t)(l->cfg_w>>8);cfg[2]=(uint8_t)l->cfg_h;cfg[3]=(uint8_t)(l->cfg_h>>8);
    cfg[4]=(uint8_t)l->cfg_fps;cfg[6]=1;pj_put32(cfg+8,16000);cfg[12]=1;cfg[14]=1;cfg[16]=(uint8_t)l->audio_block;cfg[17]=(uint8_t)(l->audio_block>>8);
    pj_plink_emit_begin(l,PJ_AV_CONFIG,0,0,cfg,20);l->config_sent=1;l->pk_seq=1;return 1;
}
/* Bounded demux work per call: keeps the ring fed from the Wi-Fi stream. */
static inline void pj_plink_stream_step(pj_plink *l) {
    unsigned fits=0; /* 1.4.0: at most PJ_PLINK_FITS_PER_STEP pictures fitted per call (the rest wait in `buf`) */
    for(unsigned guard=0;guard<64&&(l->stream_state==PJ_PS_OPEN||l->emit_kind!=PJ_PE_NONE);guard++) {
        if(l->emit_kind!=PJ_PE_NONE){if(!pj_plink_emit_step(l))return;continue;}
        if(l->stream_state!=PJ_PS_OPEN)return; /* ENDED/FAILED with the final packet flushed */
        if(!l->config_sent){int r=pj_plink_stream_config(l);if(r<0)return;if(r>0)continue;}
        else if(l->audio_block&&l->ablock_used==l->audio_block) {
            pj_plink_emit_begin(l,PJ_AV_AUDIO,0,pj_plink_audio_pts(l),l->ablock,l->audio_block);
            l->audio_samples+=1u+2u*(l->audio_block-4u);continue;
        } else if(l->frame_pending) {
            /* 1.3.9: re-coded with the decoder's own tables and letterboxed to cfg_h, in place (pocket_jpeg_fit.h). A picture
             * the fit refuses is left out (every MJPEG picture stands alone; the Pocket keeps time by the timestamps);
             * PJ_PLINK_FIT_FAILS in a row end the stream with a reason. */
            if(fits>=PJ_PLINK_FITS_PER_STEP)return;
            size_t size=0;uint32_t t0=l->x.micros?l->x.micros(l->io.ctx):0;
            int fit=pj_fit_jpeg(l->buf,l->avi.frame_size,PJ_PLINK_BUF,l->cfg_w,l->cfg_h,0,&l->scratch->fit.work,&size);
            fits++;l->sp.fits++;PJ_PLINK_FIT_DONE(l);
            if(l->x.micros){uint32_t us=l->x.micros(l->io.ctx)-t0;l->sp.fit_us_sum+=us;if(us>l->sp.fit_us_max)l->sp.fit_us_max=us;}
            if(fit!=PJ_FIT_OK) {
                l->frame_pending=0;l->fit_drops++;
                if(++l->fit_fails>=PJ_PLINK_FIT_FAILS){l->stream_state=PJ_PS_FAILED;l->why="pictures not in a form the Pocket can show";return;}
                continue;
            }
            l->fit_fails=0;
            pj_plink_emit_begin(l,PJ_AV_VIDEO,PJ_AV_KEYFRAME,pj_plink_video_pts(l,l->video_index),l->buf,(unsigned)size);continue;
        }
        if(l->frame_pending)return; /* headers still incomplete with a frame held: wait for the config */
        if(l->net_used==l->net_size) {
            int n=l->io.stream_read(l->io.ctx,l->net,sizeof(l->net));
            if(n==0)return;
            if(n<0) {
                if(n==-2||!l->config_sent){l->stream_state=PJ_PS_FAILED;l->why=n==-2?"the stream failed":"the stream ended before its headers";return;}
                uint64_t v=pj_plink_video_pts(l,l->video_index),a=pj_plink_audio_pts(l);
                pj_plink_emit_begin(l,PJ_AV_END,0,v>a?v:a,l->net,0);l->stream_state=PJ_PS_ENDED;return;
            }
            l->net_size=(size_t)n;l->net_used=0;l->sp.net_bytes+=(uint32_t)n;
        }
        size_t used=0;int r=jf_avi_feed(&l->avi,l->net+l->net_used,l->net_size-l->net_used,&used);
        l->net_used+=used;
        if(r==JF_AVI_BAD){l->stream_state=PJ_PS_FAILED;l->why="the stream is not an AVI";return;}
        if(r==JF_AVI_FRAME){l->frame_pending=1;l->video_index=l->avi.frame_index;}
        /* JF_AVI_AUDIO_FULL: the block is complete, emitted on the next pass; JF_AVI_MORE: keep feeding */
    }
}
/* Answer the held READ when data waits (or at the deadline). */
static inline void pj_plink_read_reply(pj_plink *l,int force) {
    if(l->work!=PJ_PW_READ)return;
    int ended=l->stream_state==PJ_PS_ENDED&&l->ring_count==0&&l->emit_kind==PJ_PE_NONE;
    if(l->stream_state==PJ_PS_FAILED){pj_plink_error(l,l->why?l->why:"the stream failed");return;}
    if(!l->ring_count&&!ended&&!force)return;
    uint8_t *p=l->response+PJ_RPC_HEADER;
    /* 1.4.0: the ring, then the ring filled again from the stream and taken again, up to PJ_PLINK_READ_MAX (see
     * PJ_PLINK_READ_REFILLS). Each refill is one bounded stream step; it stops as soon as one brings nothing. */
    unsigned n=0;
    for(unsigned refill=0;;refill++) {
        unsigned k=l->ring_count;if(k>PJ_PLINK_READ_MAX-n)k=PJ_PLINK_READ_MAX-n;
        if(k){pj_plink_ring_take(l,p+16+n,k);n+=k;}
        if(n>=PJ_PLINK_READ_MAX||refill>=PJ_PLINK_READ_REFILLS||(l->stream_state!=PJ_PS_OPEN&&l->emit_kind==PJ_PE_NONE))break;
        pj_plink_stream_step(l);
        if(!l->ring_count)break;
    }
    pj_put32(p,l->stream_session);pj_put32(p+4,(uint32_t)l->read_offset);pj_put32(p+8,(uint32_t)(l->read_offset>>32));
    p[12]=(uint8_t)(n?1:ended?3:2);p[13]=0;p[14]=(uint8_t)n;p[15]=(uint8_t)(n>>8);
    l->stream_offset+=n;
    if(n){l->sp.reads++;l->sp.read_bytes+=n;if(n>l->sp.read_max)l->sp.read_max=n;}else l->sp.read_empty++;
    pj_plink_finish(l,16+n,0);
}
/* Begin answering the validated request in l->request. */
static inline void pj_plink_dispatch(pj_plink *l) {
    pj_json j;pj_json_token t[32];uint64_t handle=0,value=0;
    size_t n=l->request_size-16;const uint8_t *body=l->request+16;
    if(!pj_json_parse(&j,n?body:(const uint8_t *)"{}",n?n:2,t,32)||j.tokens[0].type!=PJ_JSON_OBJECT){pj_plink_error(l,"Invalid bridge request.");return;}
    int at=pj_json_find(&j,0,"handle");
    if(at>=0&&!pj_json_u64(&j,at,&handle)){pj_plink_error(l,"Invalid bridge request.");return;}
    char path[460]; /* the START path is the longest: two 32-char ids, a session id and the stream options */
    l->json_at=PJ_RPC_HEADER;
    switch(l->request[4]) {
    case PJ_RPC_HELLO: {
        /* 1.3.5: no stream over a link whose fast rate failed (115,200 cannot carry it); the library still works */
        int stream=l->io.stream_open&&l->io.stream_read&&l->io.stream_close&&l->fast!=PJ_PF_OFF;
        pj_plink_puts(l,"{\"version\":1,\"transport\":\"chromatic-link\",\"hardware_connected\":true,\"max_payload\":");
        pj_plink_put_u64(l,PJ_PLINK_JSON);
        pj_plink_puts(l,stream?",\"compressed_stream\":\"PJAV-JPEG-IMA\",\"pocket_decoder_ready\":true":",\"compressed_stream\":\"none\",\"pocket_decoder_ready\":false");
        pj_plink_puts(l,",\"link_khz\":");pj_plink_put_u64(l,PJ_TETHER_LINK_KHZ_STREAM);pj_plink_puts(l,",\"art_keys\":true");
#ifndef PJ_PLINK_NO_ACK /* a host check's "before" measurement: the 1.4.0 credit rule alone */
        pj_plink_puts(l,",\"credit_ack\":1"); /* 1.5.0: send ACKs in place of the filler */
#endif
        pj_plink_puts(l,"}");
        pj_plink_finish(l,l->json_at-PJ_RPC_HEADER,0);return;
    }
    case PJ_RPC_HOME:
        pj_plink_stream_close(l); /* the Pocket left playback (the tokens need the ring's room) */
        l->view_count=l->view_next=l->rows_done=0;l->handle_count=0;l->art_valid=0;l->art_jpeg=0;
        pj_plink_puts(l,"{\"rows\":[");
        snprintf(path,sizeof(path),"/UserViews?userId=%s",l->user);pj_plink_get(l,PJ_PW_VIEWS,path);return;
    case PJ_RPC_BROWSE: {
        at=pj_json_find(&j,0,"start");
        if(at>=0&&!pj_json_u64(&j,at,&value)){pj_plink_error(l,"Invalid library query.");return;}
        if(value>1000000){pj_plink_error(l,"Invalid library query.");return;}
        const char *parent="";char hex[33];
        if(handle){if(handle>l->handle_count){pj_plink_error(l,"Unknown media handle. Refresh the library.");return;}parent=pj_plink_unpack(hex,l->handles[handle-1].id);}
        pj_plink_stream_close(l);
        l->art_req_offset=(uint32_t)value; /* reused: the page start, echoed in the reply */
        snprintf(path,sizeof(path),"/Items?userId=%s&parentId=%s&startIndex=%u&limit=%u&sortBy=SortName&sortOrder=Ascending&%s",
                 l->user,parent,(unsigned)value,PJ_PLINK_PAGE,JF_FIELDS_GRID);
        pj_plink_puts(l,"{\"items\":[");pj_plink_get(l,PJ_PW_BROWSE,path);return;
    }
    case PJ_RPC_DETAILS: {
        if(!handle||handle>l->handle_count){pj_plink_error(l,"Unknown media handle. Refresh the library.");return;}
        l->details_handle=(uint32_t)handle;pj_plink_stream_close(l);char hex[33];
        snprintf(path,sizeof(path),"/Items?userId=%s&ids=%s&fields=Overview,Genres&enableImages=false",l->user,pj_plink_unpack(hex,l->handles[handle-1].id));
        pj_plink_get(l,PJ_PW_DETAILS,path);return;
    }
    case PJ_RPC_ART: {
        uint64_t kind=PJ_ART_POSTER,offset=0;
        at=pj_json_find(&j,0,"kind");if(at>=0&&!pj_json_u64(&j,at,&kind)){pj_plink_error(l,"Unknown artwork kind.");return;}
        at=pj_json_find(&j,0,"offset");if(at>=0&&!pj_json_u64(&j,at,&offset)){pj_plink_error(l,"Invalid media offset.");return;}
        if(kind>PJ_ART_BACKDROP){pj_plink_error(l,"Unknown artwork kind.");return;}
        if(!handle||handle>l->handle_count){pj_plink_error(l,"Unknown media handle. Refresh the library.");return;}
        if(offset>0xffffffffu){pj_plink_error(l,"Invalid media offset.");return;}
        l->art_req_handle=(uint32_t)handle;l->art_req_kind=(uint8_t)kind;l->art_req_offset=(uint32_t)offset;
        if(offset==0&&l->sync_done<l->sync_total&&!(l->art_handle==handle&&l->art_kind==kind))l->sync_done++; /* status line: N of M */
        l->sync_ms=l->now;
        if(l->art_handle==handle&&l->art_kind==kind&&(l->art_jpeg||l->art_total==0)&&l->work==PJ_PW_NONE&&l->stream_state==PJ_PS_NONE){pj_plink_art_reply(l);return;}
        const pj_plink_handle *h=&l->handles[handle-1];
        uint8_t img;char hex[33];const char *id=pj_plink_image_for(h,(unsigned)kind,&img,hex);
        l->art_valid=0;l->art_jpeg=0;l->art_handle=(uint32_t)handle;l->art_kind=(uint8_t)kind;l->art_total=0;
        if(!img||!jf_hex_id(id)){pj_plink_art_reply(l);return;} /* no art: total 0, a valid reply */
        {uint8_t key[17];pj_plink_pack(key,id);key[16]=img;
         for(unsigned k=0;k<8;k++)if(!memcmp(l->noart[k],key,17)){pj_plink_art_reply(l);return;}} /* the server has none */
        if(l->stream_state!=PJ_PS_NONE){pj_plink_error(l,"Busy playing.");return;} /* l->buf is the frame buffer */
        static const char *const kinds[]={"","Primary","Thumb","Backdrop"};
        snprintf(path,sizeof(path),"/Items/%s/Images/%s?fillWidth=%u&fillHeight=%u&quality=80&format=Jpg",
                 id,kinds[img],pj_art_width((unsigned)kind),pj_art_height((unsigned)kind));
        pj_plink_get(l,PJ_PW_ART,path);return;
    }
    case PJ_RPC_START: {
        if(!l->io.stream_open||!l->io.stream_read||!l->io.stream_close){pj_plink_error(l,"Unsupported bridge request.");return;}
        if(l->fast==PJ_PF_OFF){pj_plink_error(l,"The link is too slow to play (fast connection failed).");return;}
        if(!handle||handle>l->handle_count){pj_plink_error(l,"Unknown media handle. Refresh the library.");return;}
        if(l->http_busy||l->http_stale){pj_plink_error(l,"Busy.");return;} /* `buf` is the frame buffer: no reply may still land in it */
        char profile[40]="";at=pj_json_find(&j,0,"profile");if(at>=0)pj_json_text(&j,at,profile,sizeof(profile));
        at=pj_json_find(&j,0,"start_seconds");if(at>=0&&!pj_json_u64(&j,at,&value)){pj_plink_error(l,"Invalid bridge request.");return;}
        const pj_tether_profile *p=pj_tether_profile_find(profile);if(!p)p=pj_tether_profiles(NULL);
        pj_plink_stream_close(l);
        jf_avi_init(&l->avi,l->buf,PJ_PLINK_BUF);jf_avi_audio(&l->avi,pj_plink_audio_sink,l);
        l->stream_session=(l->stream_session*1664525u+1013904223u+l->now)|1u;l->pk_seq=0;l->stream_offset=0;l->read_offset=0;
        l->video_index=0;l->audio_samples=0;l->audio_block=0;l->frame_us=0;l->art_valid=0;l->art_jpeg=0;l->art_handle=0;
        l->fit_fails=0;l->fit_drops=0;pj_plink_speed_reset(l,l->now); /* 1.4.0: "plink speed" counts this title */
        l->why=""; /* 1.4.0: a new title; the last one's reason ("the stream failed") no longer shows on its trace line */
        char session[17],hex[33];jf_hex16(session,(l->now*2654435761u)^(l->stream_session*2246822519u));
        pj_plink_unpack(hex,l->handles[handle-1].id);
        /* 1.3.9: maxWidth/maxHeight, not width/height. Jellyfin 10.11 treats both alike (EnforceResolutionLimit: the
         * picture keeps the title's shape and fits inside them, probe_link_stream_geometry.py); older servers took
         * width+height as a size to stretch to. The fit (pocket_jpeg_fit.h) letterboxes what comes to the Pocket's size. */
        snprintf(path,sizeof(path),"/Videos/%s/stream.avi?mediaSourceId=%s&playSessionId=%s&videoCodec=mjpeg&%s"
                 "&maxWidth=%u&maxHeight=%u&maxFramerate=%u&videoBitRate=%u&startTimeTicks=%llu",
                 hex,hex,session,JF_STREAM_AUDIO_IMA,
                 p->width,p->height,p->fps,p->video_kbit*1000u,(unsigned long long)value*10000000ull);
        if(l->io.stream_open(l->io.ctx,path)!=0){pj_plink_error(l,"The stream could not be opened.");return;}
        l->stream_state=PJ_PS_OPEN;
        pj_plink_puts(l,"{\"session\":");pj_plink_put_u64(l,l->stream_session);pj_plink_puts(l,",\"start_seconds\":");pj_plink_put_u64(l,value);
        pj_plink_puts(l,",\"profile\":");pj_plink_put_string(l,p->name);pj_plink_puts(l,"}");
        pj_plink_finish(l,l->json_at-PJ_RPC_HEADER,0);return;
    }
    case PJ_RPC_READ: {
        uint64_t session=0,offset=0;
        if(!pj_json_u64(&j,pj_json_find(&j,0,"session"),&session)||!pj_json_u64(&j,pj_json_find(&j,0,"offset"),&offset)){pj_plink_error(l,"Invalid bridge request.");return;}
        if(l->stream_state==PJ_PS_NONE||session!=l->stream_session){pj_plink_error(l,"No such stream.");return;}
        if(offset!=l->stream_offset){pj_plink_error(l,"Read out of step.");return;}
        l->read_offset=offset;l->work=PJ_PW_READ;l->read_ms=l->now;
        pj_plink_stream_step(l);pj_plink_read_reply(l,0);return;
    }
    case PJ_RPC_STOP:
        pj_plink_stream_close(l);
        pj_plink_puts(l,"{\"stopped\":true}");pj_plink_finish(l,l->json_at-PJ_RPC_HEADER,0);return;
    default:pj_plink_error(l,"Unsupported bridge request.");return;
    }
}

/* ---- platform entry points ---- */
static inline int pj_plink_init(pj_plink *l,pj_plink_ops ops,const char *user_id,uint32_t now) {
    if(!l||!ops.request||!ops.decode||!ops.fpga_send||!user_id)return 0;
    pj_plink_diag keep=l->d; /* 1.3.6: the link's evidence outlives a retry (fault_prev too: a fault bit that stuck
                              * through the retry is one appearance, not two) */
    /* 1.3.7: the three large buffers are the platform's, set in l->buf, l->response and l->scratch before the first
     * init and kept by every init after it (cleared, as the one-block gateway's were). The launcher allocates them
     * (jellyfin_esp32.c pj_jl_begin); a host check that sets none gets this translation unit's own static ones. */
    uint8_t *buf=l->buf,*response=l->response;pj_plink_scratch *scratch=l->scratch;
#ifndef ESP_PLATFORM
    if(!buf&&!response&&!scratch) {
        static uint8_t host_buf[PJ_PLINK_BUF],host_response[PJ_PLINK_RESPONSE];static pj_plink_scratch host_scratch;
        buf=host_buf;response=host_response;scratch=&host_scratch;
    }
#endif
    if(!buf||!response||!scratch)return 0;
    memset(buf,0,PJ_PLINK_BUF);memset(response,0,PJ_PLINK_RESPONSE);memset(scratch,0,sizeof(*scratch));
    memset(l,0,sizeof(*l));l->d=keep;l->buf=buf;l->response=response;l->scratch=scratch;
    l->io=ops;jf_copy(l->user,sizeof(l->user),user_id);l->now=now;
    pj_wire_rx_init(&l->upstream,l->incoming,sizeof(l->incoming));
    l->state=PJ_PL_ARMING;l->enter_ms=l->status_ms=l->heartbeat_ms=now;l->why="";l->stream_session=now;
    l->again_ms=now;l->quiet_ms=now-PJ_PLINK_FAST_QUIET_MS;pj_plink_speed_reset(l,now);
    if(!pj_plink_ctrl(l,PJ_TETHER_CTRL_ENTER)){pj_plink_fail(l,"FPGA UART busy");return 0;}
    return 1;
}
static inline void pj_plink_extras(pj_plink *l,pj_plink_extra x){l->x=x;}
/* Leaves media mode. At the fast rate the leave goes out twice: once at the fast rate, once
 * at 115,200 after the platform switched back, so the FPGA hears it whichever rate it is at. */
static inline void pj_plink_stop(pj_plink *l) {
    if(l->state!=PJ_PL_OFF)pj_plink_leave(l);
    pj_plink_stream_close(l);
    l->state=PJ_PL_OFF;l->tx_active=0;l->http_busy=0;l->http_stale=0;l->work=PJ_PW_NONE;l->fast=PJ_PF_SLOW;
}
/* The platform's HTTP task delivers the body here (another task: only `buf`/`reply_size` are touched, and only for
 * the request the gateway still wants). */
static inline void pj_plink_reply_data(pj_plink *l,const uint8_t *data,size_t n) {
    if(!l->http_busy||l->http_stale)return;
    if(l->reply_size+n>PJ_PLINK_BUF){l->reply_size=PJ_PLINK_BUF+1u;return;} /* too big: refused in reply */
    memcpy(l->buf+l->reply_size,data,n);l->reply_size+=n;
}
static inline void pj_plink_reply(pj_plink *l,int status) {
    if(l->http_stale){l->http_stale=0;l->http_busy=0;return;} /* the abandoned request's answer: dropped */
    if(!l->http_busy)return;
    l->http_busy=0;uint8_t work=l->work;char path[220];
    if(l->reply_size>PJ_PLINK_BUF){pj_plink_error(l,"The server sent more than fits.");return;}
    if(work==PJ_PW_ART) {
        l->art_jpeg=status==200&&l->reply_size>=4&&l->buf[0]==0xff&&l->buf[1]==0xd8;
        if(status==404&&l->art_req_handle&&l->art_req_handle<=l->handle_count) { /* remember: asked again, answered here */
            uint8_t img;char hex[33];const char *id=pj_plink_image_for(&l->handles[l->art_req_handle-1],l->art_req_kind,&img,hex);
            uint8_t *slot=l->noart[l->noart_next];pj_plink_pack(slot,id);slot[16]=img;l->noart_next=(uint8_t)((l->noart_next+1u)%8u);
        }
        l->art_valid=0; /* decoded per window on reply */
        pj_plink_art_reply(l);return;
    }
    if(status<200||status>299){pj_plink_error(l,status==401?"Jellyfin sign-in expired":"Request failed locally.");return;}
    if(work==PJ_PW_DETAILS) {
        pj_json j;l->art_valid=0;
        if(!pj_json_parse(&j,l->buf,l->reply_size,l->scratch->tokens,PJ_PLINK_TOKENS)){pj_plink_error(l,"Request failed locally.");return;}
        int list=j.tokens[0].type==PJ_JSON_OBJECT?pj_json_find(&j,0,"Items"):-1,obj=-1;
        if(list>=0&&j.tokens[list].type==PJ_JSON_ARRAY&&(unsigned)list+1<j.tokens[list].next&&j.tokens[list+1].type==PJ_JSON_OBJECT)obj=list+1;
        else if(j.tokens[0].type==PJ_JSON_OBJECT)obj=0;
        if(obj<0){pj_plink_error(l,"Request failed locally.");return;}
        /* The overview is decoded into the reply's own tail (no static buffer): the JSON written from the front can
         * never reach it, at most 6 bytes per character (\u00XX) + 64 < the 7,192 bytes before the tail. */
        char *overview=(char *)l->response+PJ_PLINK_RESPONSE-(PJ_PLINK_OVERVIEW+1u);overview[0]=0;
        _Static_assert(PJ_RPC_HEADER+64u+6u*PJ_PLINK_OVERVIEW<PJ_PLINK_RESPONSE-(PJ_PLINK_OVERVIEW+1u),"overview tail");
        int ov=pj_json_find(&j,obj,"Overview");if(ov>=0&&!pj_json_text(&j,ov,overview,PJ_PLINK_OVERVIEW+1u))overview[0]=0;
        l->json_at=PJ_RPC_HEADER;
        int ok=pj_plink_puts(l,"{\"overview\":")&&pj_plink_put_string(l,overview)&&pj_plink_puts(l,",\"genres\":[");
        int g=pj_json_find(&j,obj,"Genres");unsigned shown=0;
        if(ok&&g>=0&&j.tokens[g].type==PJ_JSON_ARRAY)
            for(unsigned i=(unsigned)g+1;i<j.tokens[g].next&&shown<3&&ok;i=j.tokens[i].next) {
                char one[40];if(!pj_json_text(&j,(int)i,one,sizeof(one)))continue;
                ok=(shown==0||pj_plink_puts(l,","))&&pj_plink_put_string(l,one);shown++;
            }
        if(!ok||!pj_plink_puts(l,"]}")){pj_plink_error(l,"Details do not fit.");return;}
        pj_plink_finish(l,l->json_at-PJ_RPC_HEADER,0);return;
    }
    if(work==PJ_PW_VIEWS) {
        pj_json j;l->art_valid=0;
        if(!pj_json_parse(&j,l->buf,l->reply_size,l->scratch->tokens,PJ_PLINK_TOKENS)){pj_plink_error(l,"Request failed locally.");return;}
        int list=j.tokens[0].type==PJ_JSON_OBJECT?pj_json_find(&j,0,"Items"):0;
        if(list>=0&&j.tokens[list].type==PJ_JSON_ARRAY)
            for(unsigned i=(unsigned)list+1;i<j.tokens[list].next&&l->view_count<JF_ROW_ITEMS;i=j.tokens[i].next) {
                jf_item v;if(j.tokens[i].type==PJ_JSON_OBJECT&&jf_parse_item(&j,(int)i,&v,JF_SHAPE_THUMB))
                    {jf_copy(l->views[l->view_count].id,33,v.id);jf_copy(l->views[l->view_count].name,32,v.name);l->view_count++;}
            }
        if(!pj_plink_row_open(l,"My Media",PJ_ART_THUMB)||pj_plink_put_items(l,JF_ROW_ITEMS,NULL,PJ_ART_THUMB)<0||!pj_plink_puts(l,"]}")){pj_plink_error(l,"Home does not fit.");return;}
        l->rows_done++;
        snprintf(path,sizeof(path),"/UserItems/Resume?userId=%s&limit=%u&mediaTypes=Video&%s",l->user,PJ_PLINK_ROW_LIMIT,JF_FIELDS);
        pj_plink_get(l,PJ_PW_RESUME,path);return;
    }
    if(work==PJ_PW_RESUME||work==PJ_PW_NEXTUP||work==PJ_PW_LATEST) {
        const char *title=work==PJ_PW_RESUME?"Continue Watching":work==PJ_PW_NEXTUP?"Next Up":NULL;
        char latest[40]="Latest ";
        if(!title){jf_copy(latest+7,sizeof(latest)-7,l->views[l->view_next].name);title=latest;}
        size_t mark=l->json_at;unsigned shape=work==PJ_PW_LATEST?PJ_ART_POSTER:PJ_ART_THUMB;
        if(!pj_plink_row_open(l,title,shape)){pj_plink_error(l,"Home does not fit.");return;}
        int n=pj_plink_put_items(l,PJ_PLINK_ROW_LIMIT,NULL,shape);
        if(n<0){pj_plink_error(l,"Home does not fit.");return;}
        if(n==0)l->json_at=mark; /* an empty row is left out, as the Pocket shows no empty strips */
        else if(!pj_plink_puts(l,"]}")){pj_plink_error(l,"Home does not fit.");return;}
        else l->rows_done++;
        if(work==PJ_PW_RESUME){snprintf(path,sizeof(path),"/Shows/NextUp?userId=%s&limit=%u&%s",l->user,PJ_PLINK_ROW_LIMIT,JF_FIELDS);pj_plink_get(l,PJ_PW_NEXTUP,path);return;}
        if(work==PJ_PW_LATEST)l->view_next++;
        pj_plink_next_home_step(l);return;
    }
    if(work==PJ_PW_BROWSE) {
        uint32_t total=0;int n=pj_plink_put_items(l,PJ_PLINK_PAGE,&total,PJ_ART_POSTER);
        if(n<0){pj_plink_error(l,"Request failed locally.");return;}
        if(!pj_plink_puts(l,"],\"shape\":1,\"start\":")||!pj_plink_put_u64(l,l->art_req_offset)||!pj_plink_puts(l,",\"next\":")||
           !pj_plink_put_u64(l,l->art_req_offset+(unsigned)n)||!pj_plink_puts(l,",\"total\":")||!pj_plink_put_u64(l,total?total:(uint32_t)n)||
           !pj_plink_puts(l,"}")){pj_plink_error(l,"Page does not fit.");return;}
        pj_plink_finish(l,l->json_at-PJ_RPC_HEADER,0);return;
    }
}
/* Every FPGA UART frame goes here; 1 when it was the bridge's status packet. */
static inline int pj_plink_fpga_packet(pj_plink *l,unsigned address,const uint8_t *p,size_t n,uint32_t now) {
    if(address!=PJ_RX_PLINK_STATUS||n!=10)return 0;
    if(l->state==PJ_PL_OFF)return 1;
    l->status_packets++;
    l->fpga_mode=p[0]&3;l->fpga_faults=(p[0]>>2)&15;l->fpga_fast=(p[0]&PJ_TETHER_STATUS_FAST_BIT)!=0;
    /* Credit: the reported level counts what had reached the FPGA when the bridge wrote this status, and that was up
     * to one pass ago (statuses are read once a pass, before the step). 1.3.6 (credit review): so everything that had
     * not left the UART when the previous step began counts as sent since (`held`, plus a whole packet for the one in
     * flight, of which x.tx_pending sees only the unsent tail), and so does everything that step handed over
     * (`pass_sent`). Counting only what the UART holds NOW missed the bytes that left between the bridge's write and
     * this read: up to a whole burst (448 bytes) against a margin of 48, and a dropped packet on a slow cable.
     * `busy`: a status needs its own 14 bytes on the wire and the driver's receive time, so one the bridge wrote
     * shortly BEFORE the previous step began can be read only now; a packet whose last byte left the UART in between
     * is in neither its level nor `held`. One such packet is allowed for whenever the UART had work in the pass
     * before: the margin holds 48 of its 64 bytes, the 16 here the rest (16 is also the most that costs the 512 kHz
     * stream nothing: two bursts of 448, a level of 64 and 16 fill the queue less its margin exactly). That is every
     * status written up to a packet's time (68 UART bytes: 1.3 ms at 524,288 baud, 5.9 ms at 115,200) before the
     * previous step, inside a reply (whole packets).
     * 1.3.6: that was too short. On the console a status can be latched about 155 UART byte
     * times before the previous step, or more: the bridge's arbiter wait (up to ~94, all 11 channels), its own 14 bytes,
     * the ESP32's receive FIFO holding up to 47 more (PJ_UART1_RX_FULL 48) and the loop's scheduling. While a stream
     * runs at the fast rate the UART is busy right up to ~48 byte times before each step, so packets end at -48, -116
     * and -184 byte times: three packets (192 bytes) can be missing from a status written 200 byte times early. The
     * allowance is now three packets less the margin (144): every status up to ~200 byte times before the previous
     * step. It costs the 512 kHz stream one packet in many passes (about 41.0 -> 36.9 KB/s; 256 kHz is limited by the
     * cable and unchanged); docs/Design-notes.md "Allowance widened". Still not covered: a reply's short
     * last packet and the next reply's first both ending in that time with the queue full. */
    l->level=((uint32_t)p[2]<<8)|p[3];
    l->sent_since=l->held+(l->held?PJ_PLINK_DATA_BYTES:0u)+(l->busy?3u*PJ_PLINK_DATA_BYTES-PJ_PLINK_MARGIN:0u)+l->pass_sent;l->status_ms=now;
    unsigned count=p[1];if(count>6){pj_plink_fail(l,"FPGA status malformed");return 1;}
    /* 1.3.6: every byte the FPGA hands over is counted and kept for the diagnostics, in any state, before anything
     * below can turn the packet away. */
    pj_pld_faults(&l->d,l->fpga_faults);if(count)l->d.up_packets++;
    for(unsigned i=0;i<count;i++)pj_pld_byte(&l->d,p[4+i],now);
    pj_pld_poll(&l->d,now);
    if(count)l->quiet_ms=now; /* 1.3.6: the Pocket is speaking: not the moment to change the UART rate */
    if(l->state==PJ_PL_ARMING&&l->fpga_mode==2){l->state=PJ_PL_READY;l->ready_ms=now;}
    if(l->fast==PJ_PF_SWITCHED&&l->fpga_fast)l->fast=PJ_PF_FAST;
    if(l->state==PJ_PL_READY) {
        if(l->fpga_mode!=2){pj_plink_fail(l,"link mode ended");return 1;}
        if(l->fpga_faults==1u) { /* only the link-byte timeout: the cable came out in mid-byte. Not a failure: the
                                  * screen says unplugged, the FPGA re-arms by itself, ENTER (in media mode) clears the
                                  * sticky bit; the Pocket asks again when it is back. */
            if(l->pocket_seen)l->pocket_ms=now-PJ_PLINK_UNPLUGGED_MS-1u;
            /* 1.3.6: like the heartbeat, never while the two ends may be at different rates (cmd 4 sent,
             * the MCU not switched yet): this ENTER would leave at 115,200 for a bridge that may be fast already.
             * It goes out once the rate has settled (fast or not): the fault bit is sticky and reported again. */
            if(l->fast!=PJ_PF_REQUESTED&&(uint32_t)(now-l->clear_ms)>=PJ_PLINK_HEARTBEAT_MS&&pj_plink_ctrl(l,PJ_TETHER_CTRL_ENTER))l->clear_ms=now;
        }
        else if(l->fpga_faults){pj_plink_fail(l,"link fault");return 1;}
        for(unsigned i=0;i<count;i++) {
            /* 1.3.6: the decoder's verdict on each byte is counted (the decoding itself is unchanged). */
            unsigned was_active=l->upstream.active;size_t was_length=l->upstream.length;
            size_t size=0;int got=pj_wire_rx_byte(&l->upstream,p[4+i],&size);
            l->d.fed++;
            if(!was_active&&p[4+i]!=PJ_WIRE_FLAG&&p[4+i]!=PJ_WIRE_IDLE)l->d.dropped++; /* outside any frame */
            if(got<0){l->d.torn++;l->d.dropped+=(uint32_t)was_length+(p[4+i]!=PJ_WIRE_FLAG);continue;} /* a damaged frame: the Pocket retries */
            if(!got)continue;
            {uint8_t seq;uint32_t count; /* 1.5.0: a Pocket ACK (never a request: those are 16 bytes or more) */
             if(pj_tether_ack_parse(l->incoming,size,&seq,&count)){pj_plink_ack(l,seq,count,now);continue;}
             if(size==PJ_TETHER_ACK_SIZE)l->ack_bad++;} /* an ACK's size, its check failed: also refused below, as before */
            if(!pj_rpc_validate(l->incoming,size)&&!(size>=16&&size<=1040&&!memcmp(l->incoming,"PJB1",4)&&l->incoming[5]==0&&
               size==16+pj_av_u16(l->incoming+6)&&(pj_av_crc_part(pj_av_crc_part(0xffffffffu,l->incoming,12),l->incoming+16,size-16)^0xffffffffu)==pj_av_u32(l->incoming+12))) {
                /* 1.3.6: a frame that arrived whole and is refused, exactly as before; now the reason and its first
                 * bytes are kept (too short, not "PJB1", length field against the size, CRC, or not a request). */
                unsigned why=PJ_PLR_NOTREQ;
                if(size<16)why=PJ_PLR_SHORT;
                else if(memcmp(l->incoming,"PJB1",4))why=PJ_PLR_MAGIC;
                else if(size>1040||size!=16u+pj_av_u16(l->incoming+6))why=PJ_PLR_LENGTH;
                else if((pj_av_crc_part(pj_av_crc_part(0xffffffffu,l->incoming,12),l->incoming+16,size-16)^0xffffffffu)!=pj_av_u32(l->incoming+12))why=PJ_PLR_CRC;
                pj_pld_reject(&l->d,why,l->incoming,size);
                continue;
            }
            l->d.frames_ok++;
            int same=size==l->request_size&&!memcmp(l->incoming,l->request,size);
            l->pocket_seen=1;l->pocket_ms=now; /* the Pocket is there (status line) */
            if(l->work!=PJ_PW_NONE||l->tx_active){if(same)continue;else{ /* the Pocket moved on: drop the old work */
                if(l->http_busy)l->http_stale=1; /* its answer may still arrive: dropped, see pj_plink_reply */
                l->http_busy=0;l->tx_active=0;l->work=PJ_PW_NONE;}}
            if(same&&l->cached){pj_plink_tx_begin(l);continue;}
            memcpy(l->request,l->incoming,size);l->request_size=size;l->cached=0;
            pj_plink_dispatch(l);
        }
    }
    return 1;
}
/* Downstream: as many data packets as the credit allows and the UART takes (not while the rate changes). */
static inline void pj_plink_send_down(pj_plink *l) {
    int rate_settled=l->fast==PJ_PF_SLOW||l->fast==PJ_PF_FAST||l->fast==PJ_PF_OFF||!l->io.set_baud;
    while(l->state==PJ_PL_READY&&l->tx_active&&rate_settled&&pj_plink_unsure(l)+PJ_PLINK_DATA_BYTES+PJ_PLINK_MARGIN<=PJ_PLINK_QUEUE) {
        uint8_t data[PJ_PLINK_DATA_BYTES],m[PJ_PLINK_DATA_BYTES+8];unsigned n=0;
        pj_wire_tx probe=l->tx;
        while(n<PJ_PLINK_DATA_BYTES&&pj_wire_tx_next(&probe,&data[n]))n++;
        if(!n){l->tx_active=0;break;}
        if(!l->io.fpga_send(l->io.ctx,m,pj_plink_encode(m,PJ_TX_PLINK_DATA,data,n))){l->sp.stall_room++;break;} /* UART full: keep l->tx */
        l->tx=probe;l->sent_since+=n;l->pass_sent+=n;l->d.down_bytes+=n;l->down_total+=n;
        if(n<PJ_PLINK_DATA_BYTES){l->tx_active=0;break;}
    }
    /* 1.4.0: a reply still owed with the credit used up (the bridge's queue as last reported, plus what went since) */
    if(l->state==PJ_PL_READY&&l->tx_active&&rate_settled&&pj_plink_unsure(l)+PJ_PLINK_DATA_BYTES+PJ_PLINK_MARGIN>PJ_PLINK_QUEUE)l->sp.stall_credit++;
}
/* 1.4.0: the two "plink speed" lines, every 5 s while a title plays (the launcher's diagnostics). which 0:
 *   down     bytes a second handed to the bridge for the cable (escapes and reply headers included)
 *   reads    READs answered with data a second; avg/max their size; empty: answered with nothing (in the interval)
 *   ring     the PJAV ring's mean/largest fill after each step (4,096 is full)
 *   net      server bytes a second taken from the stream
 * which 1 ("plink speed pass=..."), after it:
 *   pass     mean/longest gap between two steps in ms (the launcher's loop pass; 10 is the tick) and how many were
 *            over 15 ms; step: the longest step itself in ms x10 (0 without a clock)
 *   fits     pictures fitted (pocket_jpeg_fit.h); fit: mean/longest time, ms x10 (0 without a clock); drop: refused
 *   st       bridge status packets a second (each refreshes the credit)
 *   stall    downstream sends that stopped with a reply still owed: credit used up / UART room (PJ_JL_TX_ROOM) full
 *   uart     the binding's UART1 trouble in the interval (`u`, may be NULL): status CRC / other CRC or length / FIFO
 *            overflow / ring full / event queue full (a pass over ~15 ms at the fast rate) / framing
 *   ack      1.5.0: Pocket ACKs taken a second / refused (past what was sent) / passed over (older), the last two
 *            since the link opened; 0/0/0 with a Pocket app that sends none (the 1.4.0 credit rule is then used)
 * Counts only, each at most 99,999 (reads 999.9), so each line fits the console's 200 characters. Line 1 restarts the
 * counts, as does a call with nothing to say (no title open: 0). */
static inline unsigned long pj_plink_cap5(uint64_t v){return (unsigned long)(v>99999u?99999u:v);}
static inline int pj_plink_say_speed(pj_plink *l,uint32_t now,int which,const pj_pld_uart *u,char *out,size_t size) {
    pj_plink_speed *s=&l->sp;uint32_t ms=now-s->since_ms;
    int say=l->stream_state!=PJ_PS_NONE&&ms>=1000u&&size;
    if(say&&!which) {
        uint32_t down=l->d.down_bytes-s->down_base;unsigned long reads10=pj_plink_cap5((uint64_t)s->reads*10000u/ms);
        if(reads10>9999u)reads10=9999u;
        snprintf(out,size,"plink speed down=%lu reads=%lu.%lu avg=%lu max=%lu empty=%lu ring=%lu/%lu net=%lu",
                 pj_plink_cap5((uint64_t)down*1000u/ms),reads10/10u,reads10%10u,
                 pj_plink_cap5(s->reads?s->read_bytes/s->reads:0u),pj_plink_cap5(s->read_max),pj_plink_cap5(s->read_empty),
                 pj_plink_cap5(s->ring_samples?s->ring_sum/s->ring_samples:0u),pj_plink_cap5(s->ring_max),
                 pj_plink_cap5((uint64_t)s->net_bytes*1000u/ms));
        return 1;
    }
    if(say) {
        pj_pld_uart z;if(!u){memset(&z,0,sizeof(z));u=&z;}
        snprintf(out,size,"plink speed pass=%lu/%lu/%lu step=%lu fits=%lu fit=%lu/%lu drop=%lu st=%lu stall=%lu/%lu uart=%lu/%lu/%lu/%lu/%lu/%lu ack=%lu/%lu/%lu",
                 pj_plink_cap5(s->passes?ms/s->passes:0u),pj_plink_cap5(s->pass_ms_max),pj_plink_cap5(s->passes_long),
                 pj_plink_cap5(s->step_us_max/100u),pj_plink_cap5(s->fits),pj_plink_cap5(s->fits?s->fit_us_sum/s->fits/100u:0u),
                 pj_plink_cap5(s->fit_us_max/100u),pj_plink_cap5(l->fit_drops-s->fit_drop_base),
                 pj_plink_cap5((uint64_t)(l->status_packets-s->status_base)*1000u/ms),pj_plink_cap5(s->stall_credit),pj_plink_cap5(s->stall_room),
                 pj_plink_cap5(u->pkt_crc),pj_plink_cap5(u->pkt_other),pj_plink_cap5(u->ovf),pj_plink_cap5(u->full),pj_plink_cap5(u->evq),
                 pj_plink_cap5(u->frame),pj_plink_cap5((uint64_t)(l->ack_good-s->ack_base)*1000u/ms),pj_plink_cap5(l->ack_bad),
                 pj_plink_cap5(l->ack_old));
    }
    pj_plink_speed_reset(l,now);
    return say;
}
/* 1.4.0: between two steps, after the bridge's newest status packets went through pj_plink_fpga_packet: the downstream
 * packets they allow. The launcher calls it at the end of its loop pass (UART1 read again first), so a pass whose
 * other work is long still feeds the UART twice. Credit as in the step (the same loop, the same counts: what it sends
 * belongs to the step before, as the statuses read with it do). Nothing else is done here. */
static inline void pj_plink_pump(pj_plink *l,uint32_t now) {
    l->now=now;
    if(l->state==PJ_PL_READY)pj_plink_send_down(l);
}
/* Called often (each launcher loop, ~10 ms). 0 running, -1 failed (l->why says what). */
static inline int pj_plink_step_timed(pj_plink *l,uint32_t now);
static inline int pj_plink_step(pj_plink *l,uint32_t now) {
    /* 1.4.0: the pass and the step's own time, for "plink speed" */
    if(l->sp.last_step_ms) {
        uint32_t gap=now-l->sp.last_step_ms;l->sp.passes++;
        if(gap>l->sp.pass_ms_max)l->sp.pass_ms_max=gap;
        if(gap>15u)l->sp.passes_long++;
    }
    l->sp.last_step_ms=now?now:1u;
    uint32_t t0=l->x.micros?l->x.micros(l->io.ctx):0;
    int r=pj_plink_step_timed(l,now);
    if(l->x.micros){uint32_t us=l->x.micros(l->io.ctx)-t0;if(us>l->sp.step_us_max)l->sp.step_us_max=us;}
    return r;
}
static inline int pj_plink_step_timed(pj_plink *l,uint32_t now) {
    l->now=now;
    pj_pld_poll(&l->d,now); /* 1.3.6: a short capture completes once the line has been quiet */
    if(l->state==PJ_PL_OFF)return 0;
    /* 1.3.6 (credit review): the base of the next status's credit (pj_plink_fpga_packet) */
    l->busy=l->held||l->pass_sent;l->held=l->x.tx_pending?(uint32_t)l->x.tx_pending(l->io.ctx):0;l->pass_sent=0;
    if(l->busy&&!l->held&&l->x.idle_us) { /* 1.4.0: the line has been idle long enough (PJ_PLINK_IDLE_BYTES) */
        uint32_t baud=l->fast==PJ_PF_FAST?PJ_TETHER_UART_FAST_BAUD:PJ_TETHER_UART_SLOW_BAUD;
        if(l->x.idle_us(l->io.ctx)>=(uint32_t)((uint64_t)PJ_PLINK_IDLE_BYTES*10000000u/baud))l->busy=0;
    }
    if(l->state==PJ_PL_FAILED){pj_plink_leave(l);pj_plink_stream_close(l);return -1;}
    /* 1.3.9 (handed over to stock games mid-session, "done=no-fpga-reply"): a failure found HERE
     * leaves at once, as one found in a status packet does on the next step. These two returned -1 without the leave,
     * and the launcher (jellyfin_esp32.c pj_jl_tick) steps a failed link no more, so the MCU stayed at 524,288 baud
     * until its 3 s retry (later still while a server request was out). A bridge that had dropped to 115,200 (it left
     * media mode: its 3 s heartbeat guard, a reset) is exactly what "status stale" means, so all that time the
     * launcher's button requests went out at the wrong rate and the FPGA's readings came back unreadable: after 1.5 s
     * of that the start-up menu's silent-FPGA rule handed the console to stock. Now the leave goes out at both rates
     * and both ends are at 115,200 within this step; the readings resume on the next pass. */
    if(l->state==PJ_PL_ARMING&&(uint32_t)(now-l->enter_ms)>PJ_PLINK_ARM_MS) {
        /* 1.3.5: two different failures. No status packet at all: the FPGA has no bridge (not the 18.41 image) or the
         * UART never delivered one. Packets, but never mode 2: the bridge heard us and refused (key, or SysCtrl). */
        pj_plink_fail(l,l->status_packets?"FPGA did not enter link mode":"no reply from the FPGA bridge");
        pj_plink_leave(l);pj_plink_stream_close(l);return -1;
    }
    if(l->state==PJ_PL_READY&&(uint32_t)(now-l->status_ms)>PJ_PLINK_STATUS_STALE_MS) {
        pj_plink_fail(l,"FPGA status stale");pj_plink_leave(l);pj_plink_stream_close(l);return -1;
    }
    /* 1.3.6: ENTER again while arming (PJ_PLINK_ENTER_MS above), at 115,200 like everything outside media mode */
    if(l->state==PJ_PL_ARMING&&(uint32_t)(now-l->again_ms)>=PJ_PLINK_ENTER_MS&&pj_plink_ctrl(l,PJ_TETHER_CTRL_ENTER))l->again_ms=now;
    /* fast UART handshake */
    if(l->state==PJ_PL_READY&&l->io.set_baud) {
        /* 1.3.6: asked for only while nothing is owed and the cable is quiet (PJ_PLINK_FAST_QUIET_MS above) */
        int owed=l->tx_active||l->work!=PJ_PW_NONE||l->http_busy;
        if(owed||l->level+l->sent_since)l->quiet_ms=now;
        /* 1.3.6: and only with the MCU's transmit ring empty, before the request and before the
         * rate changes (PJ_PLINK_FAST_DRAIN_MS above) */
        size_t ring=(l->fast==PJ_PF_SLOW||l->fast==PJ_PF_REQUESTED)&&l->x.tx_pending?l->x.tx_pending(l->io.ctx):0;
        int idle=!owed&&!ring&&((uint32_t)(now-l->quiet_ms)>=PJ_PLINK_FAST_QUIET_MS||(uint32_t)(now-l->ready_ms)>=PJ_PLINK_FAST_HOLD_MS);
        if(l->fast==PJ_PF_SLOW&&idle&&pj_plink_ctrl(l,PJ_TETHER_CTRL_FAST)){l->fast=PJ_PF_REQUESTED;l->fast_ms=now;}
        else if(l->fast==PJ_PF_REQUESTED&&!ring&&(uint32_t)(now-l->fast_ms)>=PJ_PLINK_FAST_SWITCH_MS){l->io.set_baud(l->io.ctx,1);l->fast=PJ_PF_SWITCHED;l->fast_ms=now;l->heartbeat_ms=now-PJ_PLINK_HEARTBEAT_MS;}
        else if(l->fast==PJ_PF_REQUESTED&&(uint32_t)(now-l->fast_ms)>=PJ_PLINK_FAST_DRAIN_MS) {
            /* the request never left the ring: no rate change at all, and the next heartbeat a whole period later
             * (should the request still leave, the bridge is back at 115,200 250 ms after it). Its own reason, so
             * the log tells "the ring never emptied" from "the bridge never confirmed" (the branch below). */
            l->fast=PJ_PF_OFF;if(l->fast_failed<255)l->fast_failed++;l->heartbeat_ms=now;l->why="fast UART request not sent";
        }
        else if(l->fast==PJ_PF_SWITCHED&&(uint32_t)(now-l->fast_ms)>PJ_PLINK_FAST_CONFIRM_MS) {
            /* 1.3.5: not confirmed: back to 115,200 and the link goes on at the stock rate (browse and pictures; no
             * stream). The FPGA is at 115,200 too: it never switched, or it reverted after 250 ms without a valid
             * packet. A heartbeat goes out at once at the stock rate (none reached it for up to 500 ms; its guard
             * expires at 3 s). The 1.3.0-1.3.4 launchers ended the whole mode here. */
            l->io.set_baud(l->io.ctx,0);l->fast=PJ_PF_OFF;if(l->fast_failed<255)l->fast_failed++;
            l->heartbeat_ms=now-PJ_PLINK_HEARTBEAT_MS;l->why="fast UART not confirmed";
        }
    }
    /* Never while the two ends may be at different rates (cmd 4 sent, the MCU not switched yet): a heartbeat then would
     * go out at 115,200 to an FPGA already at the new rate. Right after the switch one goes at once: it confirms. */
    if(l->fast!=PJ_PF_REQUESTED&&(uint32_t)(now-l->heartbeat_ms)>=(l->fast==PJ_PF_SWITCHED?PJ_PLINK_CONFIRM_HB_MS:PJ_PLINK_HEARTBEAT_MS)&&
       pj_plink_ctrl(l,PJ_TETHER_CTRL_HEARTBEAT))l->heartbeat_ms=now;
    /* 1.4.0: downstream first (the UART works through these while the step below fits pictures), then again after the
     * stream work for what it freed or a reply it began; one credit rule for both (pj_plink_send_down) */
    pj_plink_send_down(l);
    /* playback: keep the ring fed; answer a held READ */
    if(l->stream_state!=PJ_PS_NONE) {
        pj_plink_stream_step(l);
        if(l->work==PJ_PW_READ)pj_plink_read_reply(l,l->ring_count>=PJ_PLINK_READ_FILL||(uint32_t)(now-l->read_ms)>=PJ_PLINK_READ_WAIT_MS);
        if(l->sp.ring_samples<0xffffu){l->sp.ring_sum+=l->ring_count;l->sp.ring_samples++;}
        if(l->ring_count>l->sp.ring_max)l->sp.ring_max=l->ring_count;
    }
    pj_plink_send_down(l);
    return 0;
}
/* 1.3.5: which stage the link is at (header: 1 entering, 2 fast UART, 3 waiting for the Pocket, 4 linked, 5 streaming;
 * 0 off). A failure keeps the stage it failed in, so the launcher's diagnostics and the screen can say where. */
static inline unsigned pj_plink_stage(const pj_plink *l) {
    if(l->state==PJ_PL_OFF)return PJ_PST_OFF;
    if(l->state==PJ_PL_ARMING)return PJ_PST_ENTER;
    if(l->state==PJ_PL_FAILED&&(l->why==NULL||!strcmp(l->why,"FPGA did not enter link mode")||!strcmp(l->why,"no reply from the FPGA bridge")||
                                !strcmp(l->why,"FPGA UART busy")))return PJ_PST_ENTER;
    if(l->io.set_baud&&(l->fast==PJ_PF_REQUESTED||l->fast==PJ_PF_SWITCHED))return PJ_PST_FAST;
    if(l->stream_state!=PJ_PS_NONE)return PJ_PST_STREAM;
    return l->pocket_seen?PJ_PST_LINKED:PJ_PST_POCKET;
}
/* ---- the launcher's status line (pixel screen in pocket_link_screen.h) ---- */
enum {PJ_PLV_WAITING,PJ_PLV_CONNECTED,PJ_PLV_SYNCING,PJ_PLV_STREAMING,PJ_PLV_UNPLUGGED,PJ_PLV_STOPPED};
/* slow (1.3.5): the fast UART failed and the link runs at 115,200 (browse and pictures only). */
/* 1.3.6: heard/good/bad and hint are what the link has heard from the Pocket so far (pocket_link_diag.h), shown as
 * small text while the screen waits. good: frames accepted; bad: frames refused or torn. */
typedef struct pj_plink_status_s {uint8_t what;uint16_t done,total;const char *why;uint8_t slow;
                                  uint32_t heard,good,bad;pj_pld_hint hint;} pj_plink_status;
static inline pj_plink_status pj_plink_view(const pj_plink *l,uint32_t now) {
    pj_plink_status s={PJ_PLV_WAITING,0,0,"",0,0,0,0,{PJ_PLH_NOTHING,0,0,0}};
    s.slow=(uint8_t)(l->fast==PJ_PF_OFF);
    s.heard=l->d.up_bytes;s.good=l->d.frames_ok;s.bad=l->d.frames_bad+l->d.torn;s.hint=pj_pld_hint_of(&l->d);
    if(l->state==PJ_PL_FAILED){s.what=PJ_PLV_STOPPED;s.why=l->why?l->why:"";return s;}
    if(l->state!=PJ_PL_READY||!l->pocket_seen)return s;
    if((uint32_t)(now-l->pocket_ms)>PJ_PLINK_UNPLUGGED_MS){s.what=PJ_PLV_UNPLUGGED;return s;}
    if(l->stream_state==PJ_PS_OPEN){s.what=PJ_PLV_STREAMING;return s;}
    if(l->sync_total&&l->sync_done<l->sync_total&&(uint32_t)(now-l->sync_ms)<PJ_PLINK_SYNC_RECENT_MS) {
        s.what=PJ_PLV_SYNCING;s.done=l->sync_done;s.total=l->sync_total;return s;
    }
    s.what=PJ_PLV_CONNECTED;return s;
}
/* 1.3.6: the diagnostics' console lines, gated by what the link is doing (pocket_link_diag.h pj_pld_periodic and
 * pj_pld_event). Linked: the screen says connected, syncing or playing. Then the cable carries pictures or a film and
 * the console must not hold the loop up: no periodic "plink rx" lines but one short sum every 30 s, no capture or
 * ring bytes, a refused frame at most once in 10 s. Waiting, unplugged or stopped: all the lines. */
static inline int pj_plink_linked(const pj_plink *l,uint32_t now) {
    unsigned what=pj_plink_view(l,now).what;
    return what==PJ_PLV_CONNECTED||what==PJ_PLV_SYNCING||what==PJ_PLV_STREAMING;
}
static inline int pj_plink_say_periodic(const pj_plink *l,pj_pld_printer *p,const pj_pld_uart *u,uint32_t now,unsigned index,char *out,size_t size) {
    return pj_pld_periodic(p,&l->d,u,now,index,pj_plink_linked(l,now),out,size);
}
static inline int pj_plink_say_event(const pj_plink *l,pj_pld_printer *p,uint32_t now,char *out,size_t size) {
    return pj_pld_event(p,&l->d,now,pj_plink_linked(l,now),out,size);
}
#endif
