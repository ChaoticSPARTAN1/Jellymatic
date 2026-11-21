/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Launcher behaviour with no hardware calls, so all of it runs in the host test.
 *
 * Stock FPGA facts this relies on (chromatic-fpga top.v, system_monitor.sv):
 * - The overlay opens when the FPGA's menu line toggles; the MCU can press that line
 *   by poking bit 8. The line is gated by cartridge detect, so with no cartridge the
 *   overlay cannot open and the launcher must give up and start stock.
 * - The Buttons message reports bit 9 = menu closed, and is sent on request (address
 *   2) even while the menu is closed, so open/closed is read back, never assumed.
 * Pressing the line while unsure would toggle it the wrong way, hence the read-back.
 */
#ifndef PJ_LAUNCHER_LOGIC_H
#define PJ_LAUNCHER_LOGIC_H
/* PJ_ENABLE_SETUP: the on-console Wi-Fi / Jellyfin setup (shared/setup_ui.h, main/setup_esp32.c,
 * shared/provision_write.c). 0 (default, set in chromatic/launcher/CMakeLists.txt): every SETUP HOOK
 * compiles out and the launcher writes no flash, exactly as before. */
#ifndef PJ_ENABLE_SETUP
#define PJ_ENABLE_SETUP 0
#endif
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "apps_record.h" /* 1.6.4: which optional rows the menu shows (PJ_APPS_*) */
#define PJ_FPGA_MARK_V1 0x8au
#define PJ_FPGA_MARK_V2 0x8fu
#define PJ_FPGA_MAX_PAYLOAD 10u
#define PJ_TX_BUTTONS_REQUEST 0x2u
#define PJ_TX_VERSION_REQUEST 0x6u /* the FPGA answers once with its Version message, overlay open or closed */
#define PJ_TX_POKE 0x9u
#define PJ_RX_BUTTONS 0x2u
#define PJ_RX_VERSION 0x6u
/* Media control register of the stock+media FPGA (chromatic/fpga-media/chromatic_media.v):
 * unused by stock, which ignores it; bit 0 plays the ESP32's I2S stream instead of the game,
 * bit 1 lets the overlay open without a cartridge. Stock's own SysCtrl message clears both. */
#define PJ_TX_MEDIA 0x1du
#define PJ_MEDIA_AUDIO (1u<<0)
#define PJ_MEDIA_OVERLAY (1u<<1)
/* bit 2: "our screen owns the overlay": the physical Menu button no longer closes the overlay; the
 * FPGA only reports it (Buttons bit 8, PJ_BTN_MENU below), holds the game core paused with its
 * cartridge bus idle, and silences the game's sound. Set whenever our overlay is up on the media
 * FPGA (the start-up menu and the Jellyfin app alike), cleared before every hand-over to games;
 * the launcher's own poke of the menu line still closes the overlay, so Games (the Games item,
 * or B) always works. Bit 1 (overlay without a cartridge) is set from the moment the media FPGA
 * is recognised (its Version reply to PJ_TX_VERSION_REQUEST at start) until the hand-over. */
#define PJ_MEDIA_MENU (1u<<2)
/* The media FPGA's version register (system_monitor.sv, 14 bits: reserved, debug, minor[5:0],
 * major[5:0]): stock major.minor with the debug bit set and this minor, as Shiny Relay does,
 * so ModRetro's updater still recognises the console and the launcher can tell sound is there. */
#define PJ_FPGA_MEDIA_MAJOR 18u
#define PJ_FPGA_MEDIA_MINOR 40u
/* Launcher 1.3.0: the combined image (stock 18.14 + sound media + Pocket link bridge,
 * scripts/stage_chromatic_fpga_link.py) reads 18.41 with the debug bit. It has everything the
 * 18.40 sound image has (0x1D bits, I2S sound), so sound is on for both; only 18.41 also has the
 * link bridge (0x1E/0x1F, status channel 0x0A), so only 18.41 may run the Pocket link.
 * Stock 18.14 and any official update: neither. */
#define PJ_FPGA_LINK_MINOR 41u
static inline int pj_fpga_media_audio(uint16_t version) {
    unsigned minor=(version>>6)&63u;
    return ((version>>12)&1u)&&(minor==PJ_FPGA_MEDIA_MINOR||minor==PJ_FPGA_LINK_MINOR)&&(version&63u)==PJ_FPGA_MEDIA_MAJOR;
}
static inline int pj_fpga_pocket_link(uint16_t version) {
    return ((version>>12)&1u)&&((version>>6)&63u)==PJ_FPGA_LINK_MINOR&&(version&63u)==PJ_FPGA_MEDIA_MAJOR;
}
#define PJ_BTN_START (1u<<0)
#define PJ_BTN_SELECT (1u<<1)
#define PJ_BTN_B (1u<<2)
#define PJ_BTN_A (1u<<3)
#define PJ_BTN_UP (1u<<4)
#define PJ_BTN_RIGHT (1u<<5)
#define PJ_BTN_LEFT (1u<<6)
#define PJ_BTN_DOWN (1u<<7)
#define PJ_POKE_MENU (1u<<8)
#define PJ_BTN_MENU (1u<<8)     /* Buttons message: the menu line is down (physical button or our own poke) */
#define PJ_MENU_CLOSED (1u<<9)
#define PJ_BTN_MASK 0xffu

static inline uint8_t pj_crc8(const uint8_t *data,size_t size) {
    uint8_t crc=0xff;
    for(size_t i=0;i<size;i++) {
        crc^=data[i];
        for(unsigned bit=0;bit<8;bit++)crc=(uint8_t)(crc&0x80?(crc<<1)^0x1d:crc<<1);
    }
    return crc;
}
/* Same bytes as stock SetupTxBuffer for a 16-bit payload. Returns the length. */
static inline size_t pj_fpga_encode(uint8_t *out,int v1,unsigned address,uint16_t value) {
    out[0]=(uint8_t)(v1?PJ_FPGA_MARK_V1:PJ_FPGA_MARK_V2);out[1]=(uint8_t)address;
    if(v1){out[2]=(uint8_t)(value>>8);out[3]=(uint8_t)value;return 4;}
    out[2]=2;out[3]=(uint8_t)(value>>8);out[4]=(uint8_t)value;
    out[5]=pj_crc8(out,5);return 6;
}
typedef struct {uint8_t buffer[16],state,need;unsigned used;int v1;
                uint16_t version;uint8_t version_seen;} pj_fpga_rx; /* the FPGA's version message, kept aside */
/* 1 when a Buttons message completed, with its 16-bit value in *value. A V2 Version message
 * (sent by the FPGA while the overlay is open) is kept in r->version instead and returns 0. */
static inline int pj_fpga_rx_byte(pj_fpga_rx *r,uint8_t byte,uint16_t *value) {
    switch(r->state) {
    case 0:
        if(byte!=PJ_FPGA_MARK_V1&&byte!=PJ_FPGA_MARK_V2)return 0;
        r->buffer[0]=byte;r->used=1;r->v1=byte==PJ_FPGA_MARK_V1;r->state=r->v1?1:2;return 0;
    case 1: /* V1: marker, address, two 7-bit data bytes */
        if(byte==PJ_FPGA_MARK_V1){r->state=0;return 0;}
        r->buffer[r->used++]=byte;
        if(r->used<4)return 0;
        r->state=0;
        if(r->buffer[1]!=PJ_RX_BUTTONS)return 0;
        *value=(uint16_t)(((uint16_t)r->buffer[2]<<7)|r->buffer[3]);return 1;
    case 2:r->buffer[r->used++]=byte;r->state=3;return 0;
    case 3:
        if(byte>PJ_FPGA_MAX_PAYLOAD){r->state=0;return 0;}
        r->buffer[r->used++]=byte;r->need=byte;r->state=byte?4:5;return 0;
    case 4:
        r->buffer[r->used++]=byte;
        if(--r->need==0)r->state=5;
        return 0;
    default:
        r->buffer[r->used++]=byte;r->state=0;
        if(pj_crc8(r->buffer,r->used)||r->buffer[2]!=2)return 0;
        if(r->buffer[1]==PJ_RX_VERSION){r->version=(uint16_t)(((uint16_t)r->buffer[3]<<8)|r->buffer[4]);r->version_seen=1;return 0;}
        if(r->buffer[1]!=PJ_RX_BUTTONS)return 0;
        *value=(uint16_t)(((uint16_t)r->buffer[3]<<8)|r->buffer[4]);return 1;
    }
}

#define PJ_LAUNCH_PRESS_MS 150u
#define PJ_LAUNCH_SETTLE_MS 400u   /* a reading this long after a release reflects it */
#define PJ_LAUNCH_SILENT_MS 1500u  /* no fresh reading for this long: channel is down */
/* 1.3.9: the same rule while the Pocket link runs on the Media screen (media_link). The link changes the UART's rate
 * (115,200 <-> 524,288) and, when its bridge falls silent, needs up to PJ_PLINK_STATUS_STALE_MS (1 s) to notice and its
 * leave at both rates (each a drained send, PJ_JL_SURE_MS waits) to bring both ends back to 115,200; the bridge's own
 * guard returns it to 115,200 at most 3 s after the last heartbeat it understood. Readings that stop for that long are
 * the link recovering, not a dead FPGA, and must not throw the owner into games (console, 2026-11-10). Still bounded,
 * so a dead FPGA cannot keep the owner on the link screen: 6 s, past the 3 s guard plus the 1 s detection and the
 * leave. The start-up menu, the plain Jellyfin screen, Other and the open/close paths keep 1.5 s. */
#define PJ_LAUNCH_LINK_SILENT_MS 6000u
#define PJ_LAUNCH_RELEASE_MS 3000u /* longest wait for the user to let go of buttons */
#define PJ_LAUNCH_POLL_MS 100u
#define PJ_LAUNCH_OPEN_TRIES 6u
#define PJ_LAUNCH_CLOSE_TRIES 3u
/* No countdown and no idle limits ("Wait for my choice"). The menu,
 * Jellyfin and Other screens stay until the owner chooses. Every way out to stock that
 * does not need a choice stays: a button held at power-on, the menu button, a silent
 * FPGA, and (in the shell and bootloader) the watchdog, crashes and other resets. */
/* Stock FPGA facts the press logic follows (chromatic-fpga):
 * - qspi_slave.v: the menu line is ignored until the MCU has sent two frames (two
 *   address-0 transfers), so PRIME asks the shell to send two before any press.
 * - system_monitor.sv: the overlay toggles on the menu line's release, and only if no
 *   other button was down during the press. So a press waits until the latest reading
 *   shows every button released.
 * - A reading only counts as current once it arrives SETTLE ms after our last release. */
typedef enum {
    PJ_L_PRIME,PJ_L_OPEN_PRESS,PJ_L_OPEN_WAIT,PJ_L_MENU,PJ_L_MEDIA,PJ_L_OTHER,PJ_L_CLOSE_PRESS,PJ_L_CLOSE_WAIT,PJ_L_DONE
} pj_launch_phase;
/* Launcher 1.6.0: two more rows under Jellyfin, "TV" (live channels) and "ChromaTube" (video channels), both
 * through the owner's Jellyfin server. Games stays the first row and the row selected at start; B still means
 * Games. TV and ChromaTube open the same Media screen and phase as Jellyfin (PJ_L_MEDIA: the same Wi-Fi join,
 * the same "set up Jellyfin first" lines, the same Setup hooks); media_app only tells the shell which app to
 * start there. Nothing else in the phase machine, the open/close presses or the hand-over changed. */
typedef enum {PJ_ITEM_GAMES,PJ_ITEM_MEDIA,PJ_ITEM_TV,PJ_ITEM_TUBE,PJ_ITEM_OTHER,PJ_ITEM_COUNT} pj_launch_item;
enum {PJ_APP_JELLYFIN,PJ_APP_TV,PJ_APP_TUBE}; /* pj_launcher.media_app; the same numbers as jf_app.h JF_MODE_* */
/* Why the launcher reached DONE, and why it began closing the overlay. Diagnostics
 * only: nothing decides on these. Names are in launcher_diag.h. */
typedef enum {
    PJ_DONE_NONE,PJ_DONE_NO_FPGA_REPLY,PJ_DONE_MENU_BUTTON,PJ_DONE_CLOSED,PJ_DONE_HELD_AT_POWER_ON,
    PJ_DONE_OPEN_BUTTONS_HELD,PJ_DONE_OPEN_NO_RESPONSE,PJ_DONE_COUNT
} pj_done_reason;
typedef enum {
    PJ_CLOSE_NONE,PJ_CLOSE_CHOSE_GAMES,PJ_CLOSE_B_BUTTON,PJ_CLOSE_OTHER_STOCK,PJ_CLOSE_SERVICE_END,
    PJ_CLOSE_REMOTE_MENU,PJ_CLOSE_COUNT
} pj_close_reason;
/* Other firmware: row 0 is stock and always launches it. The rest are slots that the
 * bootloader cannot start yet, so they only explain that; they never pick an image.
 * 1.6.4: while an app is hidden a fourth row, "Show hidden apps", follows them (PJ_OTHER_SHOW). */
#define PJ_OTHER_ROWS 3u
#define PJ_OTHER_SHOW 3u
/* 1.6.4 (docs/Design-notes.md.6.md "1.6.4"): the apps record (apps_record.h) can leave TV and ChromaTube off the
 * start-up menu. Games, Jellyfin and Other firmware are always there; Games stays the first row, the row selected at
 * start and what B means. `hidden` is 0 after pj_launch_init (all five rows, exactly as 1.6.0-1.6.3); the shell sets it
 * from the record with pj_launch_set_apps. With TV or ChromaTube focused, a PHYSICAL Select asks "Hide ...?" (A: hide,
 * B: cancel; Select on the other rows still opens Credits); Other firmware's "Show hidden apps" asks to bring them back.
 * Only a physical A answers yes (pj_launch_buttons sets by_hand), never a Remote Pilot press: the answer is a flash
 * write. The shell writes (pj_apps_write_wanted / pj_launch_apps_saved) only when it can (apps_writable). */
#define PJ_HIDE_TV 1u
#define PJ_HIDE_TUBE 2u
enum {PJ_APPS_PROMPT_NONE,PJ_APPS_PROMPT_HIDE,PJ_APPS_PROMPT_SHOW};
enum {PJ_AS_ASK=1,PJ_AS_SAVE,PJ_AS_FAILED};
typedef struct {
    pj_launch_phase phase;pj_launch_item selected;
    uint32_t phase_ms,poll_ms,release_ms,message_ms;
    unsigned tries,other_selected;uint16_t held;
    /* fresh: a reading taken after our last release. redraw is also set by the radio task. */
    /* close_hint: the overlay will not close yet (buttons held, or no cartridge). */
    uint8_t seen_message,menu_open,press_sent,other_note,fresh,scan_requested,close_hint;
    uint8_t app_owns_media; /* the Jellyfin app takes the Media screen's buttons (launcher 1.2) */
    uint8_t media_link;     /* 1.3.9: the Pocket link runs (set by the shell each pass): PJ_LAUNCH_LINK_SILENT_MS on Media */
    uint16_t app_pressed;   /* new presses for the app, physical and Remote Pilot alike; the loop takes them */
    /* menu_to_us: the shell has set PJ_MEDIA_MENU on a media FPGA, so a physical Menu press arrives
     * here as Buttons bit 8 instead of closing the overlay. menu_held: that bit's last reading. */
    uint8_t menu_to_us,menu_held;
    uint8_t credits_open; /* Select on the start-up menu shows a small Credits screen; B or Select returns */
    uint8_t media_app;    /* 1.6.0: PJ_APP_*: which row opened the Media screen (set only on that A press) */
    /* 1.6.4: hidden: PJ_HIDE_TV|PJ_HIDE_TUBE; livetv: TV lists the server's channels (else Local Weather only);
     * apps_flags: the record's flags in force; apps_prompt/apps_stage/apps_target: the Hide or Show question;
     * apps_want: the flags the shell is asked to write; apps_writable: the shell can write them; by_hand: the
     * press being handled is physical (set by pj_launch_buttons only); apps_note: the last save failed. */
    uint8_t hidden,livetv,apps_prompt,apps_stage,apps_target,apps_writable,by_hand,apps_note;
    uint32_t apps_flags,apps_want;
    /* SETUP HOOK (shared/setup_ui.h): Select on the Jellyfin screen (app not running) opens the on-console
     * Wi-Fi / Jellyfin setup; while open its presses go to setup_pressed and the loop takes them. */
#if PJ_ENABLE_SETUP
    uint8_t setup_open;uint16_t setup_pressed;
#endif
    uint8_t done_reason,close_reason; /* pj_done_reason, pj_close_reason: diagnostics only */
    /* service: Chromatic Manager is connected (shared/service.h sets and clears it). While
     * set, the menu ignores everything but the menu button; the open and close presses
     * are unchanged. Pairing (shared/remote.h) sets it too. */
    uint8_t service;
    volatile uint8_t redraw;
} pj_launcher;
/* What the shell must do after a step. Poke and request may both be set.
 * prime_frames: draw the menu and send that frame twice before going on. */
typedef struct {int poke;uint16_t poke_value;int request_buttons;int restart_to_stock;int prime_frames;} pj_launch_action;

static inline void pj_launch_enter(pj_launcher *l,pj_launch_phase phase,uint32_t now) {
    l->phase=phase;l->phase_ms=now;
}
static inline void pj_launch_init(pj_launcher *l,uint32_t now) {
    memset(l,0,sizeof(*l));l->selected=PJ_ITEM_GAMES;l->poll_ms=now;l->message_ms=now;
    l->release_ms=now-PJ_LAUNCH_SETTLE_MS; /* nothing released yet: any reading is current */
    l->phase=PJ_L_PRIME;l->phase_ms=now;l->tries=0;
    l->apps_flags=PJ_APPS_KNOWN;l->livetv=1; /* every row and TV's list, as 1.6.0-1.6.3, until the shell reads the record */
}
/* 1.6.4: is this start-up menu row shown? Games, Jellyfin and Other firmware always are. */
static inline int pj_launch_shown(const pj_launcher *l,unsigned item) {
    if(item==PJ_ITEM_TV)return !(l->hidden&PJ_HIDE_TV);
    if(item==PJ_ITEM_TUBE)return !(l->hidden&PJ_HIDE_TUBE);
    return item<PJ_ITEM_COUNT;
}
/* The next shown row from `item` in direction `step` (+1 Down, -1 Up), wrapping; Games is always shown, so it ends. */
static inline pj_launch_item pj_launch_step_row(const pj_launcher *l,unsigned item,int step) {
    for(unsigned n=0;n<PJ_ITEM_COUNT;n++) {
        item=(item+(step>0?1u:PJ_ITEM_COUNT-1u))%PJ_ITEM_COUNT;
        if(pj_launch_shown(l,item))break;
    }
    return (pj_launch_item)item;
}
/* The shell's flags from the apps record (or PJ_APPS_DEFAULTS). A focused row that is now hidden gives its focus to
 * the next shown row below it (the one that takes its place on the screen). */
static inline void pj_launch_set_apps(pj_launcher *l,uint32_t flags) {
    flags&=PJ_APPS_KNOWN;
    l->apps_flags=flags;l->livetv=(uint8_t)((flags&PJ_APPS_LIVETV)!=0);
    l->hidden=(uint8_t)(((flags&PJ_APPS_TV)?0u:PJ_HIDE_TV)|((flags&PJ_APPS_TUBE)?0u:PJ_HIDE_TUBE));
    if(!pj_launch_shown(l,l->selected))l->selected=pj_launch_step_row(l,l->selected,1);
    if(!l->hidden&&l->other_selected==PJ_OTHER_SHOW)l->other_selected=0;
    l->redraw=1;
}
/* Other firmware's rows: the three slots, plus Show hidden apps while something is hidden and the shell can write. */
static inline unsigned pj_other_rows(const pj_launcher *l) {
    return PJ_OTHER_ROWS+(l->hidden&&l->apps_writable?1u:0u);
}
static inline int pj_launch_hide_offered(const pj_launcher *l) {
    return l->apps_writable&&(l->selected==PJ_ITEM_TV||l->selected==PJ_ITEM_TUBE)&&pj_launch_shown(l,l->selected);
}
/* The question's answers. Only a physical A says yes (by_hand); B, a physical or remote one, says no. While the write
 * runs (PJ_AS_SAVE) nothing answers; after a failed write B closes the note. Returns 1 when the press was the prompt's. */
static inline int pj_launch_apps_press(pj_launcher *l,uint16_t pressed) {
    if(!l->apps_prompt)return 0;
    if(l->apps_stage==PJ_AS_SAVE)return 1;
    if(l->apps_stage==PJ_AS_ASK&&(pressed&PJ_BTN_A)&&l->by_hand) {
        uint32_t want=l->apps_flags;
        if(l->apps_prompt==PJ_APPS_PROMPT_HIDE)want&=~(l->apps_target==PJ_ITEM_TV?PJ_APPS_TV:PJ_APPS_TUBE);
        else want|=PJ_APPS_TV|PJ_APPS_TUBE;
        l->apps_want=want;l->apps_stage=PJ_AS_SAVE;l->redraw=1;
    } else if(pressed&PJ_BTN_B){l->apps_prompt=PJ_APPS_PROMPT_NONE;l->apps_stage=0;l->redraw=1;}
    return 1;
}
/* For the shell: the flags to write now (1), or nothing (0). The shell writes, then calls pj_launch_apps_saved. */
static inline int pj_apps_write_wanted(const pj_launcher *l,uint32_t *flags) {
    if(l->apps_prompt&&l->apps_stage==PJ_AS_SAVE){*flags=l->apps_want;return 1;}
    return 0;
}
/* The write's result. ok: the record now holds `flags` (read back and checked), so the menu follows it; a Show
 * returns to the start-up menu on the first row it brought back. Not ok: nothing changed on the menu; the
 * question says so until B. */
static inline void pj_launch_apps_saved(pj_launcher *l,int ok,uint32_t flags,uint32_t now) {
    if(!(l->apps_prompt&&l->apps_stage==PJ_AS_SAVE))return;
    if(!ok){l->apps_stage=PJ_AS_FAILED;l->apps_note=1;l->redraw=1;return;}
    int show=l->apps_prompt==PJ_APPS_PROMPT_SHOW;uint8_t was=l->hidden;
    l->apps_prompt=PJ_APPS_PROMPT_NONE;l->apps_stage=0;l->apps_note=0;
    pj_launch_set_apps(l,flags);
    if(show&&l->phase==PJ_L_OTHER) {
        l->other_selected=0;l->other_note=0;pj_launch_enter(l,PJ_L_MENU,now);
        l->selected=(was&PJ_HIDE_TV)&&pj_launch_shown(l,PJ_ITEM_TV)?PJ_ITEM_TV:pj_launch_shown(l,PJ_ITEM_TUBE)?PJ_ITEM_TUBE:PJ_ITEM_GAMES;
    }
    l->redraw=1;
}
/* 1.6.4: TV without Live TV (the apps record, the default for a new console): its row opens straight to the built-in
 * Local Weather channel. It needs Wi-Fi only: no Jellyfin sign-in, and the server is never asked for anything (the TV
 * app is not started), so "Can't reach the server" cannot appear because Live TV was never set up. */
static inline int pj_tv_weather_only(const pj_launcher *l) {
    return l->phase==PJ_L_MEDIA&&l->media_app==PJ_APP_TV&&!l->livetv;
}
/* The shell may start the TV, ChromaTube or Jellyfin app on the Media screen (its other conditions aside). */
static inline int pj_media_app_allowed(const pj_launcher *l){return l->phase==PJ_L_MEDIA&&!pj_tv_weather_only(l);}
/* The Media screen asks for a Jellyfin sign-in ("Select: sign in to Jellyfin") only for an app that needs one. */
static inline int pj_media_needs_sign_in(const pj_launcher *l){return !pj_tv_weather_only(l);}
/* Weather-only TV: open the channel once Wi-Fi has joined, once per visit to the Media screen (`tried`), and not while
 * anything else holds the heap or the radio (`busy`). */
static inline int pj_tv_weather_start_wanted(const pj_launcher *l,int wifi_joined,int tried,int busy) {
    return pj_tv_weather_only(l)&&wifi_joined&&!tried&&!busy&&!l->app_owns_media;
}
static inline void pj_launch_begin_close(pj_launcher *l,uint32_t now,pj_close_reason why) {
    l->tries=0;l->close_reason=(uint8_t)why;pj_launch_enter(l,PJ_L_CLOSE_PRESS,now);
}
static inline void pj_launch_done(pj_launcher *l,pj_done_reason why) {
    l->phase=PJ_L_DONE;l->done_reason=(uint8_t)why;
}
/* Acts on newly pressed buttons in the menu screens. Physical presses come here through
 * pj_launch_buttons; Remote Pilot's virtual presses (shared/remote.h) come here directly,
 * so they never count as held and never block the menu line's own presses. */
static inline void pj_launch_press(pj_launcher *l,uint16_t pressed,uint32_t now) {
    if(!pressed||!(l->phase==PJ_L_MENU||l->phase==PJ_L_MEDIA||l->phase==PJ_L_OTHER))return;
    /* 1.6.4: the Hide / Show question takes every press while it is up (nothing under it moves or starts). */
    if((l->phase==PJ_L_MENU||l->phase==PJ_L_OTHER)&&pj_launch_apps_press(l,pressed))return;
    if(l->phase==PJ_L_MENU) {
        /* The Credits screen is a plain overlay on the menu: while it is up only B or Select return
         * from it, and nothing else (Games, A, the arrows) acts, so the hand-over logic is untouched. */
        if(l->credits_open){if(pressed&(PJ_BTN_B|PJ_BTN_SELECT)){l->credits_open=0;l->redraw=1;}return;}
        /* 1.6.4: Select on a shown TV or ChromaTube row, pressed on the console itself, asks to hide it. A remote
         * Select there does nothing (the answer would be a flash write); on every other row Select is Credits. */
        if((pressed&PJ_BTN_SELECT)&&!(pressed&(PJ_BTN_A|PJ_BTN_START|PJ_BTN_B))&&pj_launch_hide_offered(l)) {
            if(l->by_hand){l->apps_prompt=PJ_APPS_PROMPT_HIDE;l->apps_stage=PJ_AS_ASK;l->apps_target=(uint8_t)l->selected;l->apps_note=0;l->redraw=1;}
            return;
        }
        /* One action per message: a chord of A with a direction must not both move and
         * choose, or the chosen item would not be the one the user saw highlighted. */
        if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(l->selected==PJ_ITEM_MEDIA||l->selected==PJ_ITEM_TV||l->selected==PJ_ITEM_TUBE) {
                l->media_app=(uint8_t)(l->selected==PJ_ITEM_TV?PJ_APP_TV:l->selected==PJ_ITEM_TUBE?PJ_APP_TUBE:PJ_APP_JELLYFIN);
                pj_launch_enter(l,PJ_L_MEDIA,now);l->redraw=1;
            }
            else if(l->selected==PJ_ITEM_OTHER) {
                l->other_selected=0;l->other_note=0;pj_launch_enter(l,PJ_L_OTHER,now);l->redraw=1;
            } else pj_launch_begin_close(l,now,PJ_CLOSE_CHOSE_GAMES);
        } else if(pressed&PJ_BTN_B)pj_launch_begin_close(l,now,PJ_CLOSE_B_BUTTON);
        else if(pressed&PJ_BTN_SELECT){l->credits_open=1;l->redraw=1;} /* Credits: no effect on Games/A/B */
        /* 1.6.4: hidden rows are skipped (with none hidden this is the 1.6.0 wrap exactly) */
        else if(pressed&PJ_BTN_UP){l->selected=pj_launch_step_row(l,l->selected,-1);l->redraw=1;}
        else if(pressed&PJ_BTN_DOWN){l->selected=pj_launch_step_row(l,l->selected,1);l->redraw=1;}
    } else if(l->phase==PJ_L_MEDIA) {
        if(l->app_owns_media){l->app_pressed|=pressed;return;} /* the app's; B on its Home returns to the menu */
#if PJ_ENABLE_SETUP
        if(l->setup_open){l->setup_pressed|=pressed;return;} /* SETUP HOOK: the setup screens take the buttons */
        if(pressed&PJ_BTN_SELECT){l->setup_open=1;l->setup_pressed=0;l->redraw=1;} /* SETUP HOOK: Select opens setup */
        else
#endif
        if(pressed&(PJ_BTN_B|PJ_BTN_START)){pj_launch_enter(l,PJ_L_MENU,now);l->redraw=1;} /* back to the menu */
        else if((pressed&PJ_BTN_A)&&!l->scan_requested){l->scan_requested=1;l->redraw=1;} /* radio on request only */
    } else if(l->phase==PJ_L_OTHER) {
        unsigned rows=pj_other_rows(l);
        if(l->other_selected>=rows)l->other_selected=0;
        if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(l->other_selected==0)pj_launch_begin_close(l,now,PJ_CLOSE_OTHER_STOCK); /* stock */
            else if(l->other_selected==PJ_OTHER_SHOW) { /* 1.6.4: Show hidden apps: asked on the console itself only */
                if(l->by_hand&&(pressed&PJ_BTN_A)){l->apps_prompt=PJ_APPS_PROMPT_SHOW;l->apps_stage=PJ_AS_ASK;l->apps_note=0;l->redraw=1;}
            }
            else{l->other_note=1;l->redraw=1;}
        } else if(pressed&PJ_BTN_B){l->other_note=0;pj_launch_enter(l,PJ_L_MENU,now);l->redraw=1;}
        else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN)) {
            l->other_selected=(pressed&PJ_BTN_UP)?(l->other_selected+rows-1u)%rows
                                                 :(l->other_selected+1u)%rows;
            l->other_note=0;l->redraw=1;
        }
    }
}
/* A physical Menu press forwarded by the media FPGA (PJ_MEDIA_MENU set). In the Jellyfin app it is
 * the app's (it opens the Jellyfin menu); in the Media and Other screens it is Back, like B; in the
 * start-up menu it does nothing: Games is chosen with the Games item or B, never by Menu. */
static inline void pj_launch_menu_press(pj_launcher *l) {
    /* 1.6.4: Menu on the Hide / Show question is No (not while its write runs); nothing under it moves. */
    if((l->phase==PJ_L_MENU||l->phase==PJ_L_OTHER)&&l->apps_prompt) {
        if(l->apps_stage!=PJ_AS_SAVE){l->apps_prompt=PJ_APPS_PROMPT_NONE;l->apps_stage=0;l->redraw=1;}
        return;
    }
    if(l->phase==PJ_L_MEDIA&&l->app_owns_media){l->app_pressed|=PJ_BTN_MENU;return;}
#if PJ_ENABLE_SETUP
    if(l->phase==PJ_L_MEDIA&&l->setup_open){l->setup_pressed|=PJ_BTN_MENU;return;} /* SETUP HOOK: Back inside setup */
#endif
    if(l->phase==PJ_L_MEDIA||l->phase==PJ_L_OTHER){l->other_note=0;pj_launch_enter(l,PJ_L_MENU,l->phase_ms);l->redraw=1;}
}
/* Feed every Buttons message. Buttons held from before the menu opened do not count. */
static inline void pj_launch_buttons(pj_launcher *l,uint16_t value,uint32_t now) {
    uint16_t buttons=value&PJ_BTN_MASK,pressed=(uint16_t)(buttons&~l->held);
    uint8_t menu_down=(value&PJ_BTN_MENU)!=0,menu_pressed=(uint8_t)(menu_down&&!l->menu_held);
    l->held=buttons;l->menu_held=menu_down;l->seen_message=1;l->menu_open=!(value&PJ_MENU_CLOSED);l->message_ms=now;
    if((uint32_t)(now-l->release_ms)>=PJ_LAUNCH_SETTLE_MS)l->fresh=1;
    if(l->phase==PJ_L_MENU||l->phase==PJ_L_MEDIA||l->phase==PJ_L_OTHER) {
        /* The user closed the overlay with the menu button: that means games. No press
         * of ours is held in these screens, so stopping here releases nothing. */
        if(!l->menu_open){pj_launch_done(l,PJ_DONE_MENU_BUTTON);return;}
        if(l->service)return; /* the service screen is up: no hidden menu navigation */
        /* On the media FPGA with PJ_MEDIA_MENU set the Menu button reaches us instead (bit 8);
         * our own poke is never down in these screens, so this is the physical button. */
        if(menu_pressed&&l->menu_to_us)pj_launch_menu_press(l);
    }
    l->by_hand=1;pj_launch_press(l,pressed,now);l->by_hand=0; /* 1.6.4: a physical press (Remote Pilot's never set it) */
}
/* One press of the menu line, shared by opening and closing. Before pressing it needs
 * a current reading with every button up; `want_open` is the state that ends the job. */
static inline void pj_launch_press_step(pj_launcher *l,pj_launch_action *a,uint32_t now,int want_open,
                                       pj_launch_phase wait_phase) {
    uint32_t elapsed=now-l->phase_ms;
    if(l->press_sent) {
        if(elapsed>=PJ_LAUNCH_PRESS_MS) {
            a->poke=1;a->poke_value=0;l->press_sent=0;l->release_ms=now;l->fresh=0;
            pj_launch_enter(l,wait_phase,now);
        }
        return;
    }
    if(!l->fresh){if(elapsed>=PJ_LAUNCH_SILENT_MS)pj_launch_done(l,PJ_DONE_NO_FPGA_REPLY);return;}
    if(l->menu_open==want_open) {
        if(want_open){pj_launch_enter(l,PJ_L_MENU,now);l->redraw=1;}
        else pj_launch_done(l,PJ_DONE_CLOSED);
        return;
    }
    /* A button held at power-on skips the menu: the user wants the game straight away,
     * and the boot logo's palette shortcuts (direction + A/B held) keep working. */
    if(want_open&&l->held&&l->tries==0){pj_launch_done(l,PJ_DONE_HELD_AT_POWER_ON);return;}
    if(l->held) {
        /* Opening: giving up leaves the overlay closed, which is always safe. Closing:
         * never hand over with it open (stock would start under an open overlay, with
         * game input blocked and FPGA defaults it may save as settings). Ask instead. */
        if(elapsed>=PJ_LAUNCH_RELEASE_MS) {
            if(want_open)pj_launch_done(l,PJ_DONE_OPEN_BUTTONS_HELD);
            else if(!l->close_hint){l->close_hint=1;l->redraw=1;}
        }
        return;
    }
    a->poke=1;a->poke_value=PJ_POKE_MENU;l->press_sent=1;l->tries++;l->phase_ms=now;
}
static inline void pj_launch_wait_step(pj_launcher *l,uint32_t now,int want_open,unsigned tries,
                                      pj_launch_phase press_phase) {
    if(!l->fresh) {
        if((uint32_t)(now-l->phase_ms)>=PJ_LAUNCH_SILENT_MS)pj_launch_done(l,PJ_DONE_NO_FPGA_REPLY);
        return;
    }
    if(l->menu_open==want_open) {
        if(want_open){pj_launch_enter(l,PJ_L_MENU,now);l->redraw=1;}
        else pj_launch_done(l,PJ_DONE_CLOSED);
    } else if(l->tries>=tries) {
        if(want_open)pj_launch_done(l,PJ_DONE_OPEN_NO_RESPONSE);
        else {
            /* Still open with the FPGA answering: keep trying, slowly, and say why. The
             * menu line is ignored without a cartridge; reinserting it lets this finish. */
            if(!l->close_hint){l->close_hint=1;l->redraw=1;}
            if((uint32_t)(now-l->phase_ms)>=1000u)pj_launch_enter(l,press_phase,now);
        }
    } else pj_launch_enter(l,press_phase,now);
}
static inline pj_launch_action pj_launch_step(pj_launcher *l,uint32_t now) {
    pj_launch_action a={0,0,0,0,0};
    switch(l->phase) {
    case PJ_L_PRIME:
        a.prime_frames=1;pj_launch_enter(l,PJ_L_OPEN_PRESS,now);
        break;
    case PJ_L_OPEN_PRESS:pj_launch_press_step(l,&a,now,1,PJ_L_OPEN_WAIT);break;
    case PJ_L_OPEN_WAIT:pj_launch_wait_step(l,now,1,PJ_LAUNCH_OPEN_TRIES,PJ_L_OPEN_PRESS);break;
    case PJ_L_MENU:case PJ_L_MEDIA:case PJ_L_OTHER:
        /* Waits for the owner's choice: nothing here closes by itself. Only a silent FPGA
         * (no reading for 1.5 s while we poll every 0.1 s) goes on to stock, without a
         * press, as in the open and close paths: the owner could not choose anyway. */
        /* Signed: a reading stamped a moment after `now` is not 49 days of silence. */
        {
            /* 1.3.9: longer, still bounded, while the Pocket link runs on the Media screen (PJ_LAUNCH_LINK_SILENT_MS) */
            int32_t silent=(int32_t)(l->phase==PJ_L_MEDIA&&l->media_link?PJ_LAUNCH_LINK_SILENT_MS:PJ_LAUNCH_SILENT_MS);
            if((int32_t)(now-l->message_ms)>=silent&&(int32_t)(now-l->phase_ms)>=silent)
                pj_launch_done(l,PJ_DONE_NO_FPGA_REPLY);
        }
        break;
    case PJ_L_CLOSE_PRESS:pj_launch_press_step(l,&a,now,0,PJ_L_CLOSE_WAIT);break;
    case PJ_L_CLOSE_WAIT:pj_launch_wait_step(l,now,0,PJ_LAUNCH_CLOSE_TRIES,PJ_L_CLOSE_PRESS);break;
    default:break;
    }
    /* DONE is only ever reached with no press held, so this release changes nothing. */
    if(l->phase==PJ_L_DONE){a.restart_to_stock=1;a.poke=1;a.poke_value=0;}
    else if((uint32_t)(now-l->poll_ms)>=PJ_LAUNCH_POLL_MS){a.request_buttons=1;l->poll_ms=now;}
    return a;
}

/* Launcher 1.3.4 ("pressing A too early, while the Wi-Fi join runs after a power-on, gives
 * Wi-Fi password is wrong"). The shell starts the radio (the join of the saved networks, or the plain A check
 * without any) only on the Media screen, once (radio_done) per boot or teardown, never while one runs
 * (radio_running), and not under an open or busy Setup. A press only sets scan_requested (pj_launch_press), which
 * is read here and nowhere else: no press stops, restarts or aborts a join in progress. Host-tested with A pressed
 * mid-join (check_launcher.c). */
static inline int pj_radio_start_wanted(const pj_launcher *l,int record_wifi,int radio_running,int radio_done,int setup_open,
                                        int rejoin_from_setup,int rejoin_wanted,int setup_busy) {
    return l->phase==PJ_L_MEDIA&&(l->scan_requested||record_wifi)&&!radio_running&&!radio_done&&(!setup_open||rejoin_from_setup)
           &&!rejoin_wanted&&!setup_busy;
}

/* 160x144 RGB565 overlay. 0xF81F is the FPGA's transparent key, so never use it.
 * Look: the stock overlay's black page and per-tab accents (osd_default.c), with the
 * official Jellyfin artwork from launcher_art.h. */
#include "launcher_art.h"
#include "launcher_icons.h"
#define PJ_OSD_W 160
#define PJ_OSD_H 144
#define PJ_OSD_BG 0x080808u /* 24-bit, like every colour passed to the draw helpers */
#define PJ_OSD_KEY 0xf81fu
#define PJ_UI_BAR 0x0b0b10u
#define PJ_UI_ROW 0x15151cu
#define PJ_UI_EDGE 0x23232du
#define PJ_UI_MUTED 0x8c8c9cu
#define PJ_UI_GAMES 0x00ccffu    /* stock Controls tab */
#define PJ_UI_JELLYFIN 0x00a4dcu /* Jellyfin accent */
#define PJ_UI_OTHER 0xff3399u    /* stock System tab */
#define PJ_UI_TV 0x5ec556u       /* the TV icon's green (chromatic/launcher/art/tv-icon-12.png) */
#define PJ_UI_TUBE 0x8f7bffu     /* the violet of the icons' colour bars */
#define PJ_UI_SAFE 0x5fe39au
#define PJ_UI_NOTE 0xffcf5cu
static inline uint16_t pj_osd_rgb(unsigned rgb) {
    uint16_t c=(uint16_t)(((rgb>>8)&0xf800u)|((rgb>>5)&0x07e0u)|((rgb>>3)&31u));
    return c==PJ_OSD_KEY?(uint16_t)(PJ_OSD_KEY^1u):c; /* one step of blue, never see-through */
}
/* a of 255 is all `top`. */
static inline unsigned pj_osd_mix(unsigned top,unsigned bottom,unsigned a) {
    unsigned r=(((top>>16)&255)*a+((bottom>>16)&255)*(255-a)+127)/255;
    unsigned g=(((top>>8)&255)*a+((bottom>>8)&255)*(255-a)+127)/255;
    unsigned b=((top&255)*a+(bottom&255)*(255-a)+127)/255;
    return (r<<16)|(g<<8)|b;
}
static inline unsigned pj_osd_unpack(uint16_t c) {
    return ((((unsigned)c>>11)*255u/31u)<<16)|((((unsigned)c>>5&63u)*255u/63u)<<8)|(((unsigned)c&31u)*255u/31u);
}
static inline void pj_osd_rect(uint16_t *fb,int x,int y,int w,int h,unsigned rgb) {
    uint16_t c=pj_osd_rgb(rgb);
    for(int row=y;row<y+h;row++)for(int col=x;col<x+w;col++)
        if(row>=0&&row<PJ_OSD_H&&col>=0&&col<PJ_OSD_W)fb[row*PJ_OSD_W+col]=c;
}
static inline void pj_osd_fill(uint16_t *fb,unsigned rgb) {pj_osd_rect(fb,0,0,PJ_OSD_W,PJ_OSD_H,rgb);}
/* Alpha-blended RGB565 artwork, clipped to the screen. */
static inline void pj_osd_image(uint16_t *fb,int x,int y,int w,int h,const uint16_t *rgb,const uint8_t *alpha) {
    for(int row=0;row<h;row++)for(int col=0;col<w;col++) {
        int px=x+col,py=y+row;unsigned a=alpha[row*w+col];
        if(!a||px<0||px>=PJ_OSD_W||py<0||py>=PJ_OSD_H)continue;
        fb[py*PJ_OSD_W+px]=pj_osd_rgb(pj_osd_mix(pj_osd_unpack(rgb[row*w+col]),pj_osd_unpack(fb[py*PJ_OSD_W+px]),a));
    }
}
/* alpha/advance are the project's 13x11 Roboto atlas (pocket/ui_font.h). */
static inline void pj_osd_text(uint16_t *fb,int x,int y,const char *text,unsigned rgb,
                              const unsigned char *alpha,const unsigned char *advance) {
    for(unsigned i=0;text[i];i++) {
        unsigned c=(unsigned char)text[i];
        if(c<32||c>126)c='?';
        unsigned glyph=c-32;
        for(int row=0;row<11;row++)for(int col=0;col<13;col++) {
            int px=x+col,py=y+row;
            if(px<0||px>=PJ_OSD_W||py<0||py>=PJ_OSD_H)continue;
            unsigned a=alpha[glyph*143u+(unsigned)row*13u+(unsigned)col];
            if(a)fb[py*PJ_OSD_W+px]=pj_osd_rgb(pj_osd_mix(rgb,pj_osd_unpack(fb[py*PJ_OSD_W+px]),a));
        }
        x+=advance[glyph];
    }
}
static inline int pj_osd_measure(const char *text,const unsigned char *advance) {
    int width=0;
    for(unsigned i=0;text[i];i++) {
        unsigned c=(unsigned char)text[i];
        width+=advance[(c<32||c>126?'?':c)-32];
    }
    return width;
}
/* 12x12 pixel icons drawn from rectangles, after the stock overlay's tab glyphs. */
static inline void pj_icon_cart(uint16_t *fb,int x,int y,unsigned rgb) {
    pj_osd_rect(fb,x+1,y,10,12,rgb);pj_osd_rect(fb,x+3,y+2,6,4,PJ_UI_BAR);
    for(int i=0;i<3;i++)pj_osd_rect(fb,x+2+i*3,y+9,2,2,PJ_UI_BAR);
}
static inline void pj_icon_chip(uint16_t *fb,int x,int y,unsigned rgb) {
    pj_osd_rect(fb,x+2,y+2,8,8,rgb);pj_osd_rect(fb,x+4,y+4,4,4,PJ_UI_BAR);
    for(int i=0;i<2;i++) {
        pj_osd_rect(fb,x,y+3+i*4,2,1,rgb);pj_osd_rect(fb,x+10,y+3+i*4,2,1,rgb);
        pj_osd_rect(fb,x+3+i*4,y,1,2,rgb);pj_osd_rect(fb,x+3+i*4,y+10,1,2,rgb);
    }
}
static inline void pj_icon_lock(uint16_t *fb,int x,int y,unsigned rgb) {
    pj_osd_rect(fb,x+3,y+1,6,1,rgb);pj_osd_rect(fb,x+3,y+1,1,4,rgb);pj_osd_rect(fb,x+8,y+1,1,4,rgb);
    pj_osd_rect(fb,x+2,y+5,8,6,rgb);pj_osd_rect(fb,x+5,y+7,2,2,PJ_UI_BAR);
}
static inline void pj_icon_jellyfin(uint16_t *fb,int x,int y,int big) {
    if(big)pj_osd_image(fb,x,y,PJ_BRAND_JF14_W,PJ_BRAND_JF14_H,pj_brand_jf14_rgb,pj_brand_jf14_alpha);
    else pj_osd_image(fb,x,y,PJ_BRAND_JF12_W,PJ_BRAND_JF12_H,pj_brand_jf12_rgb,pj_brand_jf12_alpha);
}
static inline void pj_icon_tv(uint16_t *fb,int x,int y) {
    pj_osd_image(fb,x,y,PJ_ICON_TV_W,PJ_ICON_TV_H,pj_icon_tv_rgb,pj_icon_tv_alpha);
}
static inline void pj_icon_tube(uint16_t *fb,int x,int y) {
    pj_osd_image(fb,x,y,PJ_ICON_TUBE_W,PJ_ICON_TUBE_H,pj_icon_tube_rgb,pj_icon_tube_alpha);
}
static inline unsigned pj_launch_accent(unsigned item) {
    return item==PJ_ITEM_GAMES?PJ_UI_GAMES:item==PJ_ITEM_MEDIA?PJ_UI_JELLYFIN:item==PJ_ITEM_TV?PJ_UI_TV:
           item==PJ_ITEM_TUBE?PJ_UI_TUBE:PJ_UI_OTHER;
}
/* A list row; the focused one takes the accent like the stock overlay's tabs. */
static inline void pj_launch_row(uint16_t *fb,int y,const char *label,int focused,unsigned accent,
                                const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_rect(fb,8,y,144,24,focused?accent:PJ_UI_EDGE);
    pj_osd_rect(fb,9,y+1,142,22,focused?pj_osd_mix(accent,0x101016u,56u):PJ_UI_ROW);
    pj_osd_rect(fb,12,y+3,18,18,PJ_UI_BAR);
    pj_osd_text(fb,36,y+7,label,focused?accent:0xffffffu,alpha,advance);
    if(focused)pj_osd_rect(fb,3,y+8,3,8,accent);
}
/* 1.6.0: the start-up menu's five rows: the same row, 19 pixels high (PJ_MENU_PITCH apart) so all five show at
 * once with the note and the key line under them. */
#define PJ_MENU_TOP 21
#define PJ_MENU_PITCH 20
static inline void pj_launch_row_small(uint16_t *fb,int y,const char *label,int focused,unsigned accent,
                                      const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_rect(fb,8,y,144,19,focused?accent:PJ_UI_EDGE);
    pj_osd_rect(fb,9,y+1,142,17,focused?pj_osd_mix(accent,0x101016u,56u):PJ_UI_ROW);
    pj_osd_rect(fb,11,y+2,16,15,PJ_UI_BAR);
    pj_osd_text(fb,33,y+4,label,focused?accent:0xffffffu,alpha,advance);
    if(focused)pj_osd_rect(fb,3,y+6,3,8,accent);
}
static inline void pj_launch_item_icon(uint16_t *fb,unsigned item,int x,int y) {
    if(item==PJ_ITEM_GAMES)pj_icon_cart(fb,x,y,PJ_UI_GAMES);
    else if(item==PJ_ITEM_MEDIA)pj_icon_jellyfin(fb,x,y,0);
    else if(item==PJ_ITEM_TV)pj_icon_tv(fb,x,y);
    else if(item==PJ_ITEM_TUBE)pj_icon_tube(fb,x,y);
    else pj_icon_chip(fb,x,y,PJ_UI_OTHER);
}
static inline void pj_launch_tabbar(uint16_t *fb) {
    pj_osd_rect(fb,0,0,PJ_OSD_W,18,PJ_UI_BAR);pj_osd_rect(fb,0,18,PJ_OSD_W,1,0x22222cu);
}
static const char *const pj_launch_labels[PJ_ITEM_COUNT]={"Games","Jellyfin","TV","ChromaTube","Other firmware"};
static const char *const pj_launch_notes[PJ_ITEM_COUNT]={"Stock Chromatic + cartridge","Your Jellyfin server",
    "Live TV channels","Your video channels","Launch another install"};
/* Five rows since 1.6.0 (three before): the tab bar names the screen ("Start with", with the focused row's icon),
 * as the Other firmware and Jellyfin menu pages do, and the rows carry the icons. */
/* 1.6.4: the Hide / Show question, a box over the start-up menu or Other firmware (pj_launch_apps_press answers it). */
static const char *const pj_apps_hide_lines[4]={"It goes off this menu.","Bring it back with","Other firmware >","Show hidden apps."};
static inline const char *pj_apps_hidden_words(uint8_t hidden) {
    return (hidden&(PJ_HIDE_TV|PJ_HIDE_TUBE))==(PJ_HIDE_TV|PJ_HIDE_TUBE)?"TV and ChromaTube":(hidden&PJ_HIDE_TV)?"TV":"ChromaTube";
}
static inline void pj_launch_draw_apps_prompt(uint16_t *fb,const pj_launcher *l,const unsigned char *alpha,const unsigned char *advance) {
    int hide=l->apps_prompt==PJ_APPS_PROMPT_HIDE;
    unsigned accent=hide?pj_launch_accent(l->apps_target):PJ_UI_SAFE;
    pj_osd_rect(fb,8,26,144,96,accent);pj_osd_rect(fb,9,27,142,94,0x101016u);
    if(hide)pj_launch_item_icon(fb,l->apps_target,14,32);
    pj_osd_text(fb,hide?31:14,33,hide?(l->apps_target==PJ_ITEM_TV?"Hide TV?":"Hide ChromaTube?"):"Show hidden apps?",0xffffffu,alpha,advance);
    if(l->apps_stage==PJ_AS_FAILED) {
        pj_osd_text(fb,14,56,"Could not save.",PJ_UI_NOTE,alpha,advance);
        pj_osd_text(fb,14,69,"Try again, or use",0xc8c8c8u,alpha,advance);
        pj_osd_text(fb,14,82,"Chromatic Manager.",0xc8c8c8u,alpha,advance);
        pj_osd_text(fb,14,106,"B: back",PJ_UI_MUTED,alpha,advance);
        return;
    }
    if(hide)for(int i=0;i<4;i++)pj_osd_text(fb,14,50+i*13,pj_apps_hide_lines[i],0xc8c8c8u,alpha,advance);
    else{pj_osd_text(fb,14,56,"Back on the menu:",0xc8c8c8u,alpha,advance);
         pj_osd_text(fb,14,69,pj_apps_hidden_words(l->hidden),0xffffffu,alpha,advance);}
    if(l->apps_stage==PJ_AS_SAVE)pj_osd_text(fb,14,106,"Saving...",PJ_UI_NOTE,alpha,advance);
    else pj_osd_text(fb,14,106,hide?"A: hide    B: cancel":"A: show    B: cancel",PJ_UI_MUTED,alpha,advance);
}
static inline void pj_launch_draw_menu(uint16_t *fb,const pj_launcher *l,uint32_t now,
                                      const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_fill(fb,PJ_OSD_BG);pj_launch_tabbar(fb);
    unsigned accent=pj_launch_accent(l->selected);
    pj_launch_item_icon(fb,l->selected,6,3);
    pj_osd_text(fb,24,4,"Start with",accent,alpha,advance);
    /* 1.6.4: only the shown rows, in order, from the top (all five with nothing hidden: the 1.6.0 screen exactly) */
    for(unsigned i=0,row=0;i<PJ_ITEM_COUNT;i++) {
        if(!pj_launch_shown(l,i))continue;
        int y=PJ_MENU_TOP+(int)row++*PJ_MENU_PITCH;
        pj_launch_row_small(fb,y,pj_launch_labels[i],i==l->selected,pj_launch_accent(i),alpha,advance);
        pj_launch_item_icon(fb,i,13,y+3);
    }
    pj_osd_text(fb,8,121,pj_launch_notes[l->selected],PJ_UI_MUTED,alpha,advance);
    (void)now; /* no countdown: the menu waits for the owner's choice */
    pj_osd_text(fb,8,133,pj_launch_hide_offered(l)?"A: start   B: Games   Select: hide":"A: start    B: Games",PJ_UI_MUTED,alpha,advance);
    if(l->apps_prompt==PJ_APPS_PROMPT_HIDE)pj_launch_draw_apps_prompt(fb,l,alpha,advance);
}
/* Credits (Select on the start-up menu): who made it and the required notices, in the normal font
 * wrapped to short lines so the long notices fit; B or Select returns. Same pixel style as the menu.
 * The author line (GPLv3 7(b), ADDITIONAL-TERMS.md) and the licence line must stay. */
static const char *const pj_credits_lines[]={
    "Created by ChaoticSPARTAN1","",
    "Jellyfin logo (c) Jellyfin","contributors, CC BY-SA 4.0","",
    "Not made or endorsed by","ModRetro or Jellyfin","",
    "GPL-3.0-or-later, no warranty.","Source: github.com/","ChaoticSPARTAN1/Jellymatic"};
#define PJ_CREDITS_COUNT (sizeof(pj_credits_lines)/sizeof(pj_credits_lines[0]))
#define PJ_CREDITS_TOP 24   /* first line */
#define PJ_CREDITS_PITCH 11 /* a text line: the font's 11 rows */
#define PJ_CREDITS_GAP 4    /* an empty line */
#define PJ_CREDITS_FOOTER 132 /* "B: back" */
/* The y of credits line i (laid out from PJ_CREDITS_TOP); i == PJ_CREDITS_COUNT gives the end of the text. */
static inline int pj_credits_y(unsigned i) {
    int y=PJ_CREDITS_TOP;
    for(unsigned k=0;k<i&&k<PJ_CREDITS_COUNT;k++)y+=pj_credits_lines[k][0]?PJ_CREDITS_PITCH:PJ_CREDITS_GAP;
    return y;
}
static inline void pj_launch_draw_credits(uint16_t *fb,const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_fill(fb,PJ_OSD_BG);pj_launch_tabbar(fb);
    pj_icon_jellyfin(fb,6,3,0);
    pj_osd_text(fb,24,4,"Credits",PJ_UI_JELLYFIN,alpha,advance);
    for(unsigned i=0;i<PJ_CREDITS_COUNT;i++)
        if(pj_credits_lines[i][0])
            pj_osd_text(fb,8,pj_credits_y(i),pj_credits_lines[i],i==0?0xffffffu:i>=8?0xa8a8b4u:0xc8c8c8u,alpha,advance);
    pj_osd_text(fb,8,PJ_CREDITS_FOOTER,"B: back",PJ_UI_MUTED,alpha,advance);
}
/* Jellyfin on the Chromatic is a status screen until streaming is built; it says so. */
static inline void pj_launch_draw_media(uint16_t *fb,const char *const lines[3],
                                       const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_fill(fb,0x101010u);
    pj_osd_image(fb,24,14,PJ_BRAND_JFBANNER_W,PJ_BRAND_JFBANNER_H,pj_brand_jfbanner_rgb,pj_brand_jfbanner_alpha);
    for(int x=0;x<80;x++)pj_osd_rect(fb,40+x,52,1,2,pj_osd_mix(PJ_UI_JELLYFIN,0xaa5cc3u,(unsigned)(x*255/79)));
    for(int i=0;i<3;i++)if(lines[i])pj_osd_text(fb,12,62+i*15,lines[i],0xd0d0d0u,alpha,advance);
    pj_osd_rect(fb,12,111,136,24,0x2e2e3au);pj_osd_rect(fb,13,112,134,22,0x1c1c24u);
    const char *back="A: check Wi-Fi   B: back";
    pj_osd_text(fb,80-pj_osd_measure(back,advance)/2,118,back,0xffffffu,alpha,advance);
}
/* 1.6.0: TV and ChromaTube while they open (and between TV and its Local Weather channel): their own icon and
 * name, what is happening, and a second line when a start failed. The Jellyfin row keeps its screen above, which
 * TV and ChromaTube also show while the console is not set up (the same lines as the Jellyfin row). */
static inline void pj_launch_draw_opening(uint16_t *fb,unsigned item,const char *line,const char *trouble,
                                         const unsigned char *alpha,const unsigned char *advance) {
    const char *name=pj_launch_labels[item<PJ_ITEM_COUNT?item:PJ_ITEM_MEDIA];
    unsigned accent=pj_launch_accent(item);
    int w=pj_osd_measure(name,advance),x=80-(w+18)/2;
    pj_osd_fill(fb,0x101010u);
    pj_launch_item_icon(fb,item,x,30);
    pj_osd_text(fb,x+18,31,name,0xffffffu,alpha,advance);
    for(int i=0;i<80;i++)pj_osd_rect(fb,40+i,50,1,2,pj_osd_mix(accent,0x101010u,(unsigned)(255-i*2)));
    pj_osd_text(fb,80-pj_osd_measure(line,advance)/2,66,line,0xd0d0d0u,alpha,advance);
    if(trouble&&trouble[0])pj_osd_text(fb,80-pj_osd_measure(trouble,advance)/2,84,trouble,PJ_UI_NOTE,alpha,advance);
    pj_osd_text(fb,80-pj_osd_measure("B: back",advance)/2,118,"B: back",PJ_UI_MUTED,alpha,advance);
}
/* 1.6.4: TV and ChromaTube on a console that is not (or not yet) set up for them: a friendly page under the app's own
 * icon and name instead of the Jellyfin status screen: what is missing, in plain words, and the one next step (the
 * console's own setup with Select, or Chromatic Manager). Drawing only: B leaves as on the Media screen, nothing is
 * written. The Jellyfin row keeps its status screen. */
enum {PJ_NOTICE_NONE,PJ_NOTICE_NO_WIFI,PJ_NOTICE_JOINING,PJ_NOTICE_WIFI_FAILED,PJ_NOTICE_NO_SIGN_IN,PJ_NOTICE_COUNT};
/* Which page, if any: `has_wifi` a saved network, `signed_in` a Jellyfin sign-in on the console, `wifi_state` the
 * shell's (0 idle, 1 joining, 2 joined, 3 failed), `rejoining` a dropped link or a switch being joined again. */
static inline unsigned pj_media_notice(const pj_launcher *l,int has_wifi,int signed_in,int wifi_state,int rejoining) {
    if(l->phase!=PJ_L_MEDIA||(l->media_app!=PJ_APP_TV&&l->media_app!=PJ_APP_TUBE))return PJ_NOTICE_NONE;
    if(!has_wifi)return PJ_NOTICE_NO_WIFI;
    if(!signed_in&&pj_media_needs_sign_in(l))return PJ_NOTICE_NO_SIGN_IN;
    if(rejoining||wifi_state==1||wifi_state==0)return PJ_NOTICE_JOINING;
    if(wifi_state==3)return PJ_NOTICE_WIFI_FAILED;
    return PJ_NOTICE_NONE;
}
/* The page's words: title, three lines, the next step ("" for none). `setup_here`: the console's own Wi-Fi and
 * Jellyfin setup is built in (Select opens it). Every line fits 148 pixels (check_apps.c). */
typedef struct {const char *title,*line[3],*step;} pj_notice_text;
static inline pj_notice_text pj_media_notice_text(const pj_launcher *l,unsigned notice,int setup_here) {
    int tube=l->media_app==PJ_APP_TUBE,weather=pj_tv_weather_only(l);
    pj_notice_text t={"",{"","",""},""};
    switch(notice) {
    case PJ_NOTICE_NO_WIFI:
        t.title="Wi-Fi isn't set up yet";
        t.line[0]=tube?"ChromaTube needs Wi-Fi.":weather?"Local Weather needs Wi-Fi.":"TV needs Wi-Fi.";
        t.line[1]=setup_here?"Add your network here,":"Add your network in";
        t.line[2]=setup_here?"or in Chromatic Manager.":"Chromatic Manager: Wi-Fi.";
        t.step=setup_here?"Select: set up Wi-Fi":"";
        break;
    case PJ_NOTICE_JOINING:
        t.title="Connecting to Wi-Fi...";
        t.line[0]=tube?"ChromaTube opens as soon":weather?"Local Weather opens as":"TV opens as soon as the";
        t.line[1]=tube?"as the console is online.":weather?"soon as the console is":"console is online.";
        t.line[2]=weather?"online.":"";
        break;
    case PJ_NOTICE_WIFI_FAILED:
        t.title="Wi-Fi didn't connect";
        t.line[0]="Is the network in range?";
        t.line[1]=setup_here?"Check its password here":"Check its password in";
        t.line[2]=setup_here?"or in Chromatic Manager.":"Chromatic Manager: Wi-Fi.";
        t.step=setup_here?"Select: check Wi-Fi":"";
        break;
    case PJ_NOTICE_NO_SIGN_IN:
        t.title="Not signed in to Jellyfin";
        t.line[0]=tube?"ChromaTube plays from":"Live TV comes from";
        t.line[1]="your Jellyfin server.";
        t.line[2]=setup_here?"Or use Chromatic Manager.":"Link it in Chromatic Manager.";
        t.step=setup_here?"Select: sign in here":"";
        break;
    default:break;
    }
    return t;
}
static inline void pj_launch_draw_notice(uint16_t *fb,const pj_launcher *l,unsigned notice,int setup_here,
                                        const unsigned char *alpha,const unsigned char *advance) {
    unsigned item=l->media_app==PJ_APP_TUBE?PJ_ITEM_TUBE:PJ_ITEM_TV;
    const char *name=pj_launch_labels[item];
    unsigned accent=pj_launch_accent(item);
    pj_notice_text t=pj_media_notice_text(l,notice,setup_here);
    int w=pj_osd_measure(name,advance),x=80-(w+18)/2;
    pj_osd_fill(fb,0x101010u);
    pj_launch_item_icon(fb,item,x,12);
    pj_osd_text(fb,x+18,13,name,0xffffffu,alpha,advance);
    for(int i=0;i<80;i++)pj_osd_rect(fb,40+i,30,1,2,pj_osd_mix(accent,0x101010u,(unsigned)(255-i*2)));
    pj_osd_text(fb,80-pj_osd_measure(t.title,advance)/2,40,t.title,0xffffffu,alpha,advance);
    for(int i=0;i<3;i++)if(t.line[i][0])pj_osd_text(fb,80-pj_osd_measure(t.line[i],advance)/2,60+i*13,t.line[i],0xc8c8c8u,alpha,advance);
    if(t.step[0])pj_osd_text(fb,80-pj_osd_measure(t.step,advance)/2,104,t.step,accent,alpha,advance);
    pj_osd_text(fb,80-pj_osd_measure("B: back",advance)/2,124,"B: back",PJ_UI_MUTED,alpha,advance);
}
/* 1.6.0: over the Local Weather channel for 3 s after it opens (and on A): its number and name, and its keys,
 * which are a channel's: B is the channel list, not Games. */
/* 1.6.4: the same banner with its keys given: "B: back" when TV opened straight to Local Weather (no Live TV). */
static inline void pj_wx_banner_keys(uint16_t *fb,const char *keys,const unsigned char *alpha,const unsigned char *advance);
static inline void pj_wx_banner(uint16_t *fb,const unsigned char *alpha,const unsigned char *advance) {
    pj_wx_banner_keys(fb,"B: channels   Up/Down: TV",alpha,advance);
}
static inline void pj_wx_banner_keys(uint16_t *fb,const char *keys,const unsigned char *alpha,const unsigned char *advance) {
    static const char name[]="WX  LOCAL WEATHER";
    int w=pj_osd_measure(name,advance),k=pj_osd_measure(keys,advance);
    if(k>w)w=k;
    w+=8;
    pj_osd_rect(fb,PJ_OSD_W-w-2,2,w,27,PJ_UI_TV);pj_osd_rect(fb,PJ_OSD_W-w-1,3,w-2,25,0x101016u);
    pj_osd_text(fb,PJ_OSD_W-w+2,4,name,0xffffffu,alpha,advance);
    pj_osd_text(fb,PJ_OSD_W-w+2,16,keys,PJ_UI_MUTED,alpha,advance);
}
/* Shown when the overlay will not close: games cannot start under it. */
static inline void pj_launch_draw_closing(uint16_t *fb,const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_fill(fb,0x101010u);
    pj_osd_text(fb,8,40,"Starting Games...",0xffffffu,alpha,advance);
    pj_osd_text(fb,8,70,"Let go of all buttons.",PJ_UI_NOTE,alpha,advance);
    pj_osd_text(fb,8,86,"No cartridge? Insert one,",PJ_UI_MUTED,alpha,advance);
    pj_osd_text(fb,8,100,"or switch off and on.",PJ_UI_MUTED,alpha,advance);
}
/* Last frame before stock: shown if the menu button is pressed before stock draws. */
static inline void pj_launch_draw_handover(uint16_t *fb,const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_fill(fb,0x101010u);
    pj_osd_text(fb,8,64,"Starting Games...",0xffffffu,alpha,advance);
}
static const char *const pj_other_labels[PJ_OTHER_ROWS]={"Stock Chromatic","Slot 2: empty","Slot 3: empty"};
/* 1.6.4: a plus sign for Show hidden apps (12x12, rectangles like the other glyphs). */
static inline void pj_icon_plus(uint16_t *fb,int x,int y,unsigned rgb) {
    pj_osd_rect(fb,x+5,y+1,2,10,rgb);pj_osd_rect(fb,x+1,y+5,10,2,rgb);
}
static inline void pj_launch_draw_other(uint16_t *fb,const pj_launcher *l,
                                       const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_fill(fb,PJ_OSD_BG);pj_launch_tabbar(fb);
    pj_icon_chip(fb,6,3,PJ_UI_OTHER);
    pj_osd_text(fb,24,4,"Other firmware",PJ_UI_OTHER,alpha,advance);
    if(pj_other_rows(l)>PJ_OTHER_ROWS) { /* 1.6.4: four rows, the menu's smaller rows, while an app is hidden */
        static const char *const labels[4]={"Stock Chromatic","Slot 2: empty","Slot 3: empty","Show hidden apps"};
        for(unsigned i=0;i<4;i++) {
            int y=24+(int)i*21;
            pj_launch_row_small(fb,y,labels[i],i==l->other_selected,i==PJ_OTHER_SHOW?PJ_UI_SAFE:PJ_UI_OTHER,alpha,advance);
            if(i==0)pj_icon_lock(fb,13,y+3,PJ_UI_SAFE);
            else if(i==PJ_OTHER_SHOW)pj_icon_plus(fb,13,y+3,PJ_UI_SAFE);
            else pj_icon_chip(fb,13,y+3,0x5a5a68u);
        }
        const char *note=l->other_note?"Nothing installed there":l->other_selected==0?"Protected, always here":
            l->other_selected==PJ_OTHER_SHOW?(l->hidden==(PJ_HIDE_TV|PJ_HIDE_TUBE)?"Hidden: TV, ChromaTube":
                                              l->hidden&PJ_HIDE_TV?"Hidden: TV":"Hidden: ChromaTube"):"Not available yet";
        pj_osd_text(fb,8,112,note,l->other_note?PJ_UI_NOTE:PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,132,"A: launch    B: back",PJ_UI_MUTED,alpha,advance);
        if(l->apps_prompt==PJ_APPS_PROMPT_SHOW)pj_launch_draw_apps_prompt(fb,l,alpha,advance);
        return;
    }
    for(unsigned i=0;i<PJ_OTHER_ROWS;i++) {
        int y=26+(int)i*26;
        pj_launch_row(fb,y,pj_other_labels[i],i==l->other_selected,PJ_UI_OTHER,alpha,advance);
        if(i==0)pj_icon_lock(fb,15,y+6,PJ_UI_SAFE);
        else pj_icon_chip(fb,15,y+6,0x5a5a68u);
    }
    const char *note=l->other_note?"Nothing installed there":
        l->other_selected==0?"Protected, always here":"Not available yet";
    pj_osd_text(fb,8,108,note,l->other_note?PJ_UI_NOTE:PJ_UI_MUTED,alpha,advance);
    pj_osd_text(fb,8,132,"A: launch    B: back",PJ_UI_MUTED,alpha,advance);
}
#endif
