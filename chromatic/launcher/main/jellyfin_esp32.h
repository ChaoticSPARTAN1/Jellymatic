/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * The launcher's view of the Jellyfin app (jellyfin_esp32.c). */
#ifndef PJ_JELLYFIN_ESP32_H
#define PJ_JELLYFIN_ESP32_H
#include <stddef.h>
#include <stdint.h>
#include "pocket_link_diag.h" /* 1.3.6: pj_pld_uart, the link diagnostics lines */
/* From the provisioning record; kept in RAM only while needed, never printed. */
typedef struct {char url[129],user[33],token[65],device[65],version[12];} pj_jf_creds;
typedef struct {uint8_t screen,playing;uint32_t shown,dropped,bitrate,position,art,largest,http_stack,stream_stack,
                 net_bytes,decodes,decode_ms_avg,decode_ms_max;
                 /* pacing: pictures a second asked of the server, measured ms from one shown picture to the
                  * next loop, pictures shown late because the network was behind, frame-rate step-downs */
                 uint32_t fps_cap,cycle_ms,rebased,slowed;
                 /* sound: 0 none, 1 playing on the sound clock, 2 on but the clock fell back, 3 sink dead;
                  * frames played, underruns (silence gaps), audio task stack headroom (words) */
                 uint8_t sound;uint32_t sound_played,sound_underruns,sound_stack;
                 /* sound, since the last status: quality step-downs because the sound starved (the link cannot carry
                  * the stream), pictures skipped to catch up with the sound, the bitrate that starved (never earned
                  * back); ring fill min/max (bytes), longest gap between ring writes (ms), longest wait of the sound
                  * task on an empty ring (ms), longest I2S write (ms), DMA dry events with the ring empty / with
                  * sound queued (task late), DMA buffers finished (x128 frames = the real I2S rate), frames in the DMA */
                 uint32_t sound_starved,sound_catchup,sound_rate_cap,sound_ring_min,sound_ring_max,sound_gap_ms,sound_wait_ms,
                 sound_i2s_ms,sound_dry_ring,sound_dry_task,sound_eofs,sound_in_dma;} pj_jf_status_t;
/* 0 started; <0 not (no memory, ...). `sound`: the FPGA reported media audio (launcher_logic.h
 * pj_fpga_media_audio), so the stream asks for IMA ADPCM and plays it over I2S; 0 = today's stock
 * FPGA, exactly the stream and behaviour as before. */
int pj_jf_begin(const pj_jf_creds *c,uint16_t *fb,uint32_t now,int sound);
int pj_jf_active(void);
int pj_jf_playing(void); /* a video is playing: Remote Pilot's live frames are throttled (remote.h video_slow) */
uint32_t pj_jf_tracks(char *out,size_t size); /* 1.3.2: "jellyfin tracks audio=.. lang=.. of=.. subs=..", changes with each choice */
void pj_jf_invalidate(void); /* the frame was drawn over by someone else */
int pj_jf_wants_exit(void);
int pj_jf_exit_to_setup(void); /* the exit was the menu's "Wi-Fi & sign-in" row: open Setup once the app is gone */
void pj_jf_link(int up);       /* the Wi-Fi link is down (the shell is rejoining) / up again: the app says so and waits */
void pj_jf_set_pump(void (*hook)(void)); /* the shell's USB/LAN output pump, run from inside long picture decodes */
void pj_jf_press(uint16_t pressed);
int pj_jf_tick(uint16_t *fb,uint32_t now); /* 1: send the frame */
void pj_jf_status(pj_jf_status_t *s);
void pj_jf_end(void);   /* returns at once; resources go in pj_jf_poll */
int pj_jf_poll(void);   /* call every loop: 1 while the last session is still letting go */
/* Launcher 1.3.0. pj_jf_set_pocket_link: the FPGA has the link bridge (18.41), so the app's menu offers
 * "Pocket link" (call before pj_jf_begin). pj_jf_exit_to_find / _plink: the app is leaving for Setup's "Find my
 * server again" / for the Pocket link screen. */
void pj_jf_set_pocket_link(int on);
int pj_jf_exit_to_find(void);
int pj_jf_exit_to_plink(void);
/* The Pocket link gateway (shared/pocket_link.h), mutually exclusive with the app: same tasks and buffers, never both.
 * pj_jl_begin: 0 started (a refused FPGA UART is retried by pj_jl_tick); -6 the last session is still letting go;
 * -5 not enough heap (nothing taken); -2 an allocation failed; -4 a task did not start; -1 bad call / app running.
 * pj_jl_rx: UART1 bytes beside pj_fpga_rx_byte. pj_jl_tick: every loop (-1 while the link mode is stopped; it is
 * entered again by itself). pj_jl_end: leaves the link mode and frees through pj_jf_poll. */
struct pj_plink_status_s;
int pj_jl_begin(const pj_jf_creds *c,uint32_t now);
int pj_jl_active(void);
/* 1.3.6: 1 for the one or two loop passes between the link's request for the fast UART and the MCU's
 * own change of rate (at most 200 ms): nothing else may be sent to the FPGA meanwhile. */
int pj_jl_rate_changing(void);
void pj_jl_rx(const uint8_t *bytes,int n,uint32_t now);
int pj_jl_tick(uint32_t now);
void pj_jl_view(struct pj_plink_status_s *out,uint32_t now); /* the status line (pocket_link.h pj_plink_view) */
void pj_jl_counts(uint32_t *state,uint32_t *level,uint32_t *faults,uint32_t *fast,uint32_t *stream);
/* 1.3.5: one diagnostics line body ("plink stage=... why=...") whenever the link's stage, state, rate, bridge mode or
 * reason changed since the last call; 0 when nothing changed. Counts and fixed words only. 1.3.6: also, once, the
 * line "plink leave not-sent n=..." when a stuck UART kept a leave back (also after the link has closed). */
int pj_jl_trace(char *out,size_t size);
/* 1.3.6: what the link heard from the Pocket, as diagnostics line bodies (counts, a code and link-protocol bytes only;
 * shared/pocket_link_diag.h). pj_jl_diag_periodic: line `index` (0..PJ_PLD_PERIODIC-1) after the 5 s "plink state="
 * line, 0 when that line has nothing new; `uart` is the launcher's UART1 event counts. pj_jl_diag_event: every loop;
 * 1 with one line the moment a capture completes or a whole frame is refused (at most one a second).
 * Both are gated by the link's view (pocket_link.h pj_plink_say_periodic/_event). Waiting, unplugged or stopped: all
 * seven periodic lines and both immediate ones. Connected, syncing or playing: one short "plink rx linked" sum every
 * 30 s (index 0 only) with no bytes in hex, no capture line, and a refused frame at most once in 10 s, so the console
 * never holds the loop up while the cable carries pictures or a film. */
int pj_jl_diag_periodic(char *out,size_t size,uint32_t now,unsigned index,const pj_pld_uart *uart);
int pj_jl_diag_event(char *out,size_t size,uint32_t now);
/* 1.3.8: one "plink net ..." line body for right after the 5 s line (TIME_WAIT pcbs, connections, lowest free heap
 * since the last line, free heap at the last stream's start); 0 when the link is not running. Counts only. */
int pj_jl_net(char *out,size_t size);
/* 1.4.0: right after "plink net" while a title plays: which 0, one "plink speed ..." line body (what the stream really
 * got: bytes down, READs, ring, server bytes, passes, fits); which 1, right after it, "plink speed uart ..." (UART1
 * trouble since the last speed line); 0 when there is nothing to say. pj_jl_streaming: a title is open on the link
 * (Remote Pilot's live frames are throttled then). Counts only. */
int pj_jl_speed(char *out,size_t size,uint32_t now,const pj_pld_uart *uart,int which);
int pj_jl_streaming(void);
/* 1.4.0: at the end of every loop pass while the link runs, after UART1 was read again: what the bridge's newest
 * statuses allow goes down the cable (a long pass feeds the UART twice). */
void pj_jl_pump(uint32_t now);
void pj_jl_end(void);
#endif
