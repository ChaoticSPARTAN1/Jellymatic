/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Chromatic Jellyfin app: talks to the owner's Jellyfin server directly over Wi-Fi.
 *
 * Portable C, no allocation, no I/O of its own. The platform (ESP32 launcher, or the PC
 * harness chromatic/jellyfin/check_app.c) performs one HTTP request at a time, a separate
 * video stream, JPEG decoding and the clock; this file decides what to ask for, keeps a
 * small model of the library, paces playback and draws the screens (jf_ui.h), which follow
 * the Pocket app's Jellyfin layout (pocket/live_ui.h) on the 160x144 display.
 *
 * Memory: the ESP32 has about 75 KB free with Wi-Fi up (launcher diagnostics),
 * so the whole app stays near 54 KB (check_app.c prints sizeof): video decodes straight into
 * the launcher's screen buffer, one 16 KB buffer holds a reply while browsing and the
 * incoming JPEG while playing, and artwork is cached compressed, decoded when drawn.
 *
 * Sign-in: the console's own token from its provisioning record (Quick Connect). It goes
 * only in the Authorization header the platform adds; never in a path, the screen or a log.
 * Requests mirror scripts/jellyfin_client.py; measured on the owner's server, 8 items per
 * Home row and 12 per folder page keep replies near 13 KB.
 */
#ifndef JF_APP_H
#define JF_APP_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "jf_avi.h"
#include "jf_audio.h"
#include "../../shared/bounded_json.h"

#define JF_W 160
#define JF_H 144
#define JF_VIDEO_H 120u      /* picture area; the playback strip sits below it */
#define JF_HERO_H 72
/* Items kept per Home row: the server is asked for JF_ROW_LIMIT (6) per row, so slots 7 and 8 were never
 * filled; 6 saves 2.2 KB of the app on the ESP32 (launcher 1.2.9, heap-min 5 KB on the console). My Media
 * lists at most 6 libraries. */
#define JF_ROW_ITEMS 6u
#define JF_PAGE_ITEMS 8u   /* one poster grid (4x2); the owner's largest 12-item page was 18.4 KB */
#define JF_ROWS 5u
#define JF_ROW_LIMIT 6u    /* items asked for per Home row (3-4 are visible): ~9.4 KB on the owner's server */
/* One reply or one video frame. scripts/probe_chromatic_reply_sizes.py on the owner's server: Home rows of
 * 8 up to 12.5 KB, 12-item folder pages up to 18.4 KB; with 6-item rows, 8-item pages and no backdrop tags
 * in grids the largest is ~10.5 KB. Video frames are ~4-9 KB (bigger ones are skipped, never overrun). */
#define JF_BUF 14336u
#define JF_ART_MAX 16384u   /* the platform gives up to this much for compressed artwork */
#define JF_ART_SLOTS 16u
#define JF_PROGRESS_MS 10000u
#define JF_SEEK_S 10u
/* Picture quality the server is asked for (MJPEG videoBitRate). Starts sharp; a slow link
 * halves it and reopens at the same position; smooth playback earns it back on the next start. */
#define JF_RATE_START 1000000u
#define JF_RATE_MIN 200000u
#define JF_RATE_MAX 1500000u
/* Frame rate asked of the server (maxFramerate: the title's own rate when lower). The ROM
 * decoder, the overlay send and the 10 ms loop cost the console 35-75 ms per picture
 * (unmeasured until the console reports dec=), so 30 fps titles cannot be decoded in time.
 * The player measures its own cycle and lowers the cap mid-play (one reopen), raises it on
 * the next start when there is headroom; a decoder that is too slow for the cap still plays
 * every picture it can, late but continuous, instead of skipping nearly all of them. */
#define JF_FPS_START 15u
#define JF_FPS_MIN 6u
#define JF_FPS_MAX 30u
#define JF_ADAPT_MS 10000u   /* at most one quality/rate reopen per 10 s: each costs a transcoder restart */
#define JF_DROPS_MAX 2u      /* late pictures skipped in a row before one is shown anyway */
/* Audio on the Wi-Fi stream (docs/Design-notes.md). With a platform that can play
 * sound (cfg.audio: the FPGA with media audio) the server is asked for IMA ADPCM 16 kHz mono
 * (8 KB/s; confirmed on the owner's server), decoded by jf_audio.h. Without one
 * (today's stock FPGA) the stream is exactly what plays today: the server ignores
 * audioStreamIndex=-1 (585 chunks still came), so the cheapest verified track is asked for,
 * MP3 32 kbit/s mono (~4 KB/s next to ~20 KB/s of pictures), and the demuxer skips it. */
#define JF_STREAM_AUDIO_MP3 "audioCodec=mp3&audioChannels=1&audioBitRate=32000"
#define JF_STREAM_AUDIO_IMA "audioCodec=adpcm_ima_wav&audioChannels=1&audioSampleRate=16000"
/* Sound clock (jf_player_tick): the audio clock is trusted while samples keep playing; after
 * this long without progress the frame clock takes over (and comes back when sound resumes).
 * A sink that refuses bytes for this long while nothing plays is dead: sound is dropped and
 * the video goes on alone. */
#define JF_AUDIO_STALE_MS 1500u
#define JF_AUDIO_DEAD_MS 1000u
/* The sink may refuse to start (its memory not back yet from the last play, a task still
 * leaving): it is asked again at every following block for this long, then the video plays
 * alone. A late start keeps sync: the sound clock counts from the block it started on. */
#define JF_AUDIO_TRY_MS 4000u
/* Sound starved: with the sound as the clock a stream the link cannot carry does not make pictures
 * late (the clock itself stalls while the ring is empty), so the network rule above never fires;
 * everything just plays slower with gaps (launcher 1.2.6 on the console: ~100 KB/s of Wi-Fi for a
 * ~122 KB/s stream, 6 underruns a second, 92% of the samples played, 13.4 of 15 pictures, nothing
 * "late"). So the 5 s window also measures the sound itself: fewer samples played than wall time asks
 * for, by more than this share (per mille) over at least JF_AUDIO_JUDGE_MS of live sound clock, is the
 * network behind, and the quality is halved like any other network step-down, provided the network
 * really had nothing to give at least JF_AUDIO_STARVE_WAITS times in the window (stream_read returned
 * 0): a main loop held by something else starves the sound too, with bytes waiting, and fewer bytes
 * would not help that. The rate that starved is remembered (rate_cap): smooth play never earns it back
 * within this Jellyfin session. */
#define JF_AUDIO_STARVE_PERMILLE 40u
#define JF_AUDIO_JUDGE_MS 4000u
#define JF_AUDIO_STARVE_WAITS 10u
/* The decoder is the bottleneck when its measured cycle (decode + overlay send + loop) takes
 * more than this share of a frame time (tenths; the same line at which the frame rate is
 * lowered); only then does skipping a late picture gain time. The console measures 36-48 ms
 * of a 66 ms frame at 15 fps. Late pictures with a decoder under that line came late over the
 * network (a burst after a stall) and are the network's, never a reason to lower the frame rate. */
#define JF_DEC_BUSY_TENTHS 9u
/* The platform tells the app when the Wi-Fi link is down (jf_app_set_link). While it is, a lost stream
 * waits for the link instead of spending its reconnect tries against nothing (launcher 1.2.8 on the console:
 * "reconnecting" with the network frozen; the ESP-IDF driver never rejoins by itself, the shell does now),
 * for at most this long; then the video stops with a plain message. */
#define JF_LINK_WAIT_MS 90000u
#define JF_MSG_LINK_DOWN "Wi-Fi dropped: rejoining..."
#define JF_MSG_LINK_BACK "Wi-Fi is back: reconnecting..."
#define JF_MSG_SERVER_LOST "Server lost: reconnecting..."
#define JF_MSG_WAITING "Waiting for the server..."
/* Launcher 1.3.2: audio and subtitle tracks (docs/Design-notes.md "Tracks"). The track list of the
 * title is kept (indexes and 3-letter language codes only, at most JF_TRACKS of each kind), so the Audio row can
 * change the track mid-play: the stream reopens where it is with the new audioStreamIndex and a fresh
 * playSessionId. A note stays on the picture for JF_NOTE_MS. Burned-in subtitles (subtitleMethod=Encode) are
 * not proven on the owner's server, so the Subtitles row is built only with JF_SUBS_ROW=1; whenever subtitles are
 * asked for, a stream that brings no picture within JF_SUBS_WAIT_MS (or fails) reopens without them. */
#define JF_TRACKS 16u
#define JF_NOTE_MS 4000u
#define JF_SUBS_WAIT_MS 15000u
#ifndef JF_SUBS_ROW
#define JF_SUBS_ROW 0
#endif
#define JF_MSG_NO_ENGLISH "No English audio in this video: playing the original"
#define JF_MSG_NO_JAPANESE "No Japanese audio in this video: playing the original"
#define JF_MSG_SUBS_FAILED "Subtitles did not work here: playing without them"
#define JF_MSG_NO_SUBS "No English subtitles in this video"
/* Where B / the menu's leave rows send the launcher: its start-up menu, or straight into Setup (the
 * "Wi-Fi & sign-in" row; the launcher closes the app first and frees its memory). */
/* Launcher 1.3.0: JF_EXIT_FIND opens Setup straight at "Find my server again" (the error screen's Select when the
 * server could not be reached), JF_EXIT_PLINK leaves for the launcher's Pocket link screen (the menu row exists only
 * when the FPGA has the link bridge, jf_config.pocket_link). */
enum {JF_EXIT_MENU,JF_EXIT_SETUP,JF_EXIT_FIND,JF_EXIT_PLINK,
      JF_EXIT_WEATHER}; /* 1.6.0, TV: the Local Weather row was chosen: the launcher shows that channel, then starts TV again */
/* What the error screen's Select offers: nothing, Find my server again, or sign in again in Setup. */
enum {JF_ERR_OTHER,JF_ERR_UNREACHABLE,JF_ERR_SIGNIN};

/* Launcher 1.6.0: two more apps on this same code, chosen by the launcher's start-up menu (jf_config.mode).
 *  - JF_MODE_TV: live TV channels of the owner's Jellyfin server. A channel list (GET /LiveTv/Channels, JF_TV_PAGE
 *    a page, no images, no user data, no programme), A tunes: POST /Items/{id}/PlaybackInfo with AutoOpenLiveStream
 *    opens the channel and names its media source and live stream; the usual stream.avi then carries liveStreamId
 *    and no startTimeTicks. Leaving a channel closes the stream, then DELETE /Videos/ActiveEncodings (this stream's
 *    playSessionId), then POST /LiveStreams/Close, in that order, before anything else is opened. No playback
 *    reports (no watch history), no pause, no seek, no reconnect: a channel that ends, fails, stalls or never sends
 *    a picture shows "No signal" and returns to the list. Up/Down while watching step to the next channel; the tune
 *    starts JF_TV_SURF_MS after the last press, and the same channel is never opened again within
 *    JF_TV_REOPEN_MS of its close (its conversion takes that long to go).
 *    With jf_config.wx_row the list starts with the built-in Local Weather row. That channel is not played here:
 *    choosing it leaves the app (JF_EXIT_WEATHER) and the launcher shows it with this app's memory given back
 *    (its HTTPS fetch does not fit beside the app); jf_config.tv_resume says how the app comes back from it.
 *  - JF_MODE_TUBE: the ordinary browser rooted at the library named JF_TUBE_LIBRARY (found by name among the
 *    user's views). Its videos are remote streams the server only knows the size and sound of after
 *    PlaybackInfo, so Play asks that first (same body) and uses the media source it names. No seek, no reopen at
 *    a position (startTimeTicks is not proven for remote videos): a video plays from its beginning in one stream.
 *  - JF_MODE_JELLYFIN (0): everything exactly as before 1.6.0: no PlaybackInfo, the same paths byte for byte.
 * Review of 1.6.0 (docs/Design-notes.md), the fixes here:
 *  - B1: TV and ChromaTube ask for JF_RATE_START_TV (500 kbit/s, the rate the console's link is proven to carry),
 *    in videoBitRate and in PlaybackInfo's MaxStreamingBitrate, and never more. ChromaTube never reopens. TV may
 *    step down once in a session (JF_RATE_LOW_TV) when the link starves: the channel is closed and tuned again
 *    under the reopen rule.
 *  - H2: PlaybackInfo's reply is not kept: the two ids are read from it as it arrives (jf_pi_feed), whatever its
 *    size, so a live stream the server opened is closed even when the reply is cut short, too long for the
 *    console's use, or nobody waits for it any more.
 *  - M1/M2: pictures of a faster-than-asked stream are thinned by count of pictures delivered, up to one in 8.
 * Measured on the owner's server after the second review pass (2026-11-19, videoBitRate=500000, maxFramerate=15,
 * PlaybackInfo first): a live channel's AVI header says 15.0 pictures a second at 160x90, and by the stream's own
 * time it carries 70-83 KB a second with sound; the server does keep to the cap and, within about 20%, to the bit
 * rate. It sends the first seconds faster than real time (its backlog: 25-26 pictures a wall second at first), which
 * is what the earlier "24 a second" was. A ChromaTube video: 15.00 a second, about 21 KB a second. So:
 *  - N1: these are 15-a-second streams like the Jellyfin row's, and are paced exactly like them (the stream's own
 *    time, read at the player's pace, TCP holding the server back): every picture shown. The thinning, the
 *    "sound first" skip and the sink's refill loop are a guard for a stream whose HEADER says more than the cap
 *    asked (jf_thin > 1, thin_on); they never look at how fast bytes arrive.
 *  - TV asks for JF_RATE_START_TV, 400 kbit/s (83 KB/s at 500 is close to the console's 100 KB/s link);
 *    ChromaTube for JF_RATE_START_TUBE, 500 kbit/s.
 *  - N3: TV's step down needs two starved 5 s windows in a row with pictures still coming, and holds for that
 *    channel only: the next channel starts at 400 again.
 *  - A channel whose pictures stop while its sound goes on (seen in the measurement) is "No signal" after
 *    JF_TV_STALL_MS, with the closes; it is not a slow link.
 *  - N4: a PlaybackInfo nobody waits for any more is given up by the platform after a short grace
 *    (jf_app_request_unwanted), so the next action is not held for up to 30 s. */
enum {JF_MODE_JELLYFIN,JF_MODE_TV,JF_MODE_TUBE};
enum {JF_TV_RESUME_NONE,JF_TV_RESUME_NEXT,JF_TV_RESUME_PREV}; /* back from the Local Weather channel: by B, by Up, by Down */
enum {JF_TV_ERR_NONE,JF_TV_ERR_UNREACHABLE,JF_TV_ERR_SIGNIN,JF_TV_ERR_OTHER}; /* why the channel list is not there */
#define JF_TUBE_LIBRARY "ChromaTube"
/* 1.6.4: what an owner who has not (or only half) set up Live TV or ChromaTube sees: plain words and where to go next,
 * never a bare error (docs/Apps-on-the-start-up-menu.md, "What you see before setup"). */
#define JF_MSG_TV_NONE "No Live TV channels yet."
#define JF_MSG_TV_NONE_STEP "Add them in Chromatic Manager: Settings > Live TV channels."
#define JF_MSG_TV_UNREACHABLE "Your Jellyfin server isn't answering. Local Weather still works."
#define JF_MSG_TV_RETRYING "Your Jellyfin server isn't answering. Trying again..."
#define JF_MSG_TUBE_MISSING "ChromaTube isn't set up on your server"
#define JF_MSG_TUBE_MISSING_STEP "Set it up in Chromatic Manager: Settings > ChromaTube server"
#define JF_MSG_TUBE_EMPTY "No channels in ChromaTube yet"
#define JF_MSG_TUBE_EMPTY_STEP "Add channels in Chromatic Manager: Settings > ChromaTube server. New videos can take a few minutes to appear."
#define JF_MSG_TUBE_DOWN "Your ChromaTube server isn't answering"
#define JF_MSG_TUBE_DOWN_STEP "Check that it is running at home, then A: try again."
#define JF_RATE_START_TV 400000u   /* TV: asked for, and never more */
#define JF_RATE_START_TUBE 500000u /* ChromaTube: asked for, and never more */
#define JF_RATE_START_MODE(a) ((a)->cfg.mode==JF_MODE_TV?JF_RATE_START_TV:JF_RATE_START_TUBE)
#define JF_RATE_LOW_TV 250000u   /* TV: the one step down of a session */
#define JF_TV_PAGE 7u            /* channels per list page (with the built-in channel: 8 rows on the first) */
#define JF_TV_SURF_MS 1500u      /* Up/Down while watching: the tune starts this long after the last press */
#define JF_TV_REOPEN_MS 10000u   /* the same channel is not opened again sooner after its close */
#define JF_TV_BANNER_MS 3000u    /* number and name under the picture after a tune */
#define JF_TV_FIRST_MS 20000u    /* no first picture this long after the stream was asked for: No signal */
#define JF_TV_STALL_MS 20000u    /* no picture for this long while watching (the sound may go on): No signal */
#define JF_TV_NOSIGNAL_MS 3000u  /* "No signal" stays this long, then the list */
#define JF_TV_EXIT_MS 6000u      /* leaving the list waits at most this long for the server closes to go out */
#define JF_SOURCE_ID 65u         /* PlaybackInfo's media source id, already URL-encoded */
#define JF_LIVE_ID 161u          /* its live stream id, already URL-encoded; a longer one is closed at once, not played */
#define JF_PI_ID_RAW 256u        /* where the reply scanner keeps the two ids while the reply arrives: in the */
#define JF_PI_LIVE_RAW 768u      /* app's own buffer (free then: nothing else is asked or played), raw, then escaped */
#define JF_TV_WX_NUMBER "WX"
#define JF_TV_WX_NAME "LOCAL WEATHER"
enum {JF_TV_IDLE,JF_TV_SURF,JF_TV_WAIT,JF_TV_OPENING,JF_TV_PLAYING,JF_TV_NOSIGNAL};
enum {JF_CLOSE_NONE,JF_CLOSE_ENCODING,JF_CLOSE_ENCODING_OUT,JF_CLOSE_LIVE,JF_CLOSE_LIVE_OUT};
/* The proven request (measured on a real server: docs/Design-notes.md), with the bit
 * rate asked for and the sound the stream request will name (review M6: without the FPGA's sound the stream asks
 * for MP3 as the Jellyfin row does, and so does this profile). */
#define JF_PLAYINFO_BODY "{\"UserId\":\"%s\",\"AutoOpenLiveStream\":true,\"EnableDirectPlay\":false,\"EnableDirectStream\":false," \
    "\"EnableTranscoding\":true,\"MaxStreamingBitrate\":%u,\"DeviceProfile\":{\"Name\":\"Chromatic\",\"MaxStreamingBitrate\":%u," \
    "\"DirectPlayProfiles\":[],\"TranscodingProfiles\":[{\"Container\":\"avi\",\"Type\":\"Video\",\"VideoCodec\":\"mjpeg\"," \
    "\"AudioCodec\":\"%s\",\"Protocol\":\"http\",\"Context\":\"Streaming\"}]}}"

/* Buttons, same bits as the launcher's FPGA button message (launcher_logic.h). */
#define JF_BTN_START  (1u<<0)
#define JF_BTN_SELECT (1u<<1)
#define JF_BTN_B      (1u<<2)
#define JF_BTN_A      (1u<<3)
#define JF_BTN_UP     (1u<<4)
#define JF_BTN_RIGHT  (1u<<5)
#define JF_BTN_LEFT   (1u<<6)
#define JF_BTN_DOWN   (1u<<7)
#define JF_BTN_MENU   (1u<<8) /* the Chromatic's Menu button, forwarded by the media FPGA (launcher_logic.h PJ_BTN_MENU) */

enum {JF_T_OTHER,JF_T_MOVIE,JF_T_EPISODE,JF_T_SERIES,JF_T_SEASON,JF_T_FOLDER,JF_T_VIDEO};
/* Rows and pages keep their base shape (THUMB or POSTER); the "Bigger cards" layout draws
 * them as THUMB_L / POSTER_L, which are also separate artwork-cache keys (own image sizes). */
enum {JF_SHAPE_THUMB,JF_SHAPE_POSTER,JF_SHAPE_HERO,JF_SHAPE_THUMB_L,JF_SHAPE_POSTER_L};
enum {JF_IMG_NONE,JF_IMG_PRIMARY,JF_IMG_THUMB,JF_IMG_BACKDROP};

typedef struct {
    char id[33],name[32],sub[36],card_id[33],hero_id[33];
    uint8_t type,card_kind,hero_kind,played;
    uint16_t year;
    uint32_t runtime_s,resume_s;
} jf_item;

typedef struct {char title[28];uint8_t shape,count,focus;jf_item items[JF_ROW_ITEMS];} jf_row;
typedef struct {uint8_t shape,count,focus;jf_item items[JF_PAGE_ITEMS];} jf_page;

enum {JF_S_CONNECT,JF_S_HOME,JF_S_FOLDER,JF_S_DETAILS,JF_S_PLAYER,JF_S_ERROR,JF_S_EXIT,JF_S_MENU,
      JF_S_CHANNELS}; /* 1.6.0, TV only: the channel list */
/* What jf_app_draw paints after a tick: everything, or only the focused card's scrolling name. */
enum {JF_DRAW_FULL,JF_DRAW_LABEL};
/* What the one outstanding request is for. */
enum {JF_R_NONE,JF_R_VIEWS,JF_R_RESUME,JF_R_NEXTUP,JF_R_LATEST,JF_R_FOLDER,JF_R_DETAILS,JF_R_IMAGE,
      JF_R_STREAMS,JF_R_PLAYING,JF_R_PROGRESS,JF_R_STOPPED,
      JF_R_CHANNELS,JF_R_PLAYINFO,JF_R_ENCODING_STOP,JF_R_LIVE_CLOSE}; /* 1.6.0 */

typedef struct {
    void *ctx;
    /* Starts one HTTP request (GET when method is NULL, else "POST" with a JSON body; 1.6.0: "POST" with no
     * body, and "DELETE", for the TV app's closes). The
     * path starts with '/' and is relative to the server URL; the platform adds the
     * Authorization header. Returns 0 when started; the platform then passes the body to
     * jf_app_reply_data and finishes with jf_app_reply exactly once. */
    int (*request)(void *ctx,const char *method,const char *path,const char *body);
    /* Opens / closes the video stream (separate connection). */
    int (*stream_open)(void *ctx,const char *path);
    void (*stream_close)(void *ctx);
    /* Up to `max` stream bytes now, without waiting: >0 bytes, 0 none yet, -1 ended, -2 failed. */
    int (*stream_read)(void *ctx,uint8_t *buf,size_t max);
    /* Decodes a baseline JPEG fitted (aspect kept, centred, black bars) into a w x h area of
     * dst, whose rows are `stride` pixels apart (RGB565). 0 on success. */
    int (*decode)(void *ctx,const uint8_t *jpeg,size_t size,uint16_t *dst,int stride,int w,int h);
    /* Sound sink, optional (all NULL on a console that cannot play it: today's stock FPGA).
     * The app hands IMA ADPCM bytes as the stream delivers them, in any split; the platform
     * keeps them in a small ring, decodes them (jf_audio.h) into its I2S output and plays
     * silence when the ring is empty. audio_write returns how many bytes it took now (less
     * than `size`: full, the rest is offered later); `block_start` marks a new ADPCM block.
     * audio_played: PCM frames of real sound played since audio_start or audio_flush, which
     * is the playback clock. audio_pause(1) holds the ring and plays silence. */
    int (*audio_start)(void *ctx,uint32_t rate);
    size_t (*audio_write)(void *ctx,const uint8_t *adpcm,size_t size,int block_start);
    uint32_t (*audio_played)(void *ctx);
    void (*audio_pause)(void *ctx,int paused);
    void (*audio_flush)(void *ctx);
    void (*audio_stop)(void *ctx);
} jf_platform;

typedef struct {
    char server_name[24];
    char user[33],device[65];
    uint8_t audio;                        /* the platform plays sound: ask the server for IMA ADPCM */
    uint16_t *fb;                         /* the 160x144 screen; video decodes straight into it */
    uint8_t *art;uint16_t art_cap;        /* compressed artwork cache, sized by the platform from free memory */
    /* JSON scratch: the screen buffer itself. Replies are parsed only while browsing, and
     * every browsing screen is redrawn in full afterwards (jf_app_reply sets redraw). */
    void *scratch;uint32_t scratch_bytes;
    const unsigned char *font_alpha,*font_advance;
    uint8_t pocket_link;                  /* 1.3.0: the FPGA has the link bridge (18.41): the menu offers "Pocket link" */
    uint8_t mode;                         /* 1.6.0: JF_MODE_*: Jellyfin (0, as before), TV, ChromaTube */
    uint8_t wx_row;                       /* 1.6.0, TV: the list starts with the Local Weather row (JF_EXIT_WEATHER) */
    uint8_t tv_resume;                    /* 1.6.0, TV: JF_TV_RESUME_*: coming back from that channel by Up or Down */
} jf_config;

typedef struct {char id[33];uint8_t kind,shape,used,ok;uint16_t off,len;uint32_t touched;} jf_art;
/* One audio or subtitle stream of the title: Jellyfin's stream Index, ISO 639-2 language ("und" unknown). */
/* want: bit 0 English, bit 1 Japanese, by the 1.2.6 rule (jf_lang_is), which is what picks the track; lang is for
 * the diagnostics line only. */
typedef struct {int16_t index;char lang[4];uint8_t def,text,want;} jf_track;

typedef struct {
    jf_platform io;jf_config cfg;
    uint8_t screen,request,redraw,busy,retries,view_next,offline;
    uint32_t now,request_ms,retry_at;
    /* Home: rows in Jellyfin's order */
    jf_row rows[JF_ROWS];uint8_t row_count,row_focus;
    jf_row views;
    /* Folder (library, series, season), one page */
    jf_page folder;char folder_id[33],folder_title[32];uint32_t folder_start,folder_total;
    uint8_t depth;char parents[4][33],parent_titles[4][32];uint32_t parent_start[4];
    /* Details */
    jf_item detail;char overview[200],genres[36];uint8_t detail_action,detail_from;
    /* Artwork: compressed JPEGs, decoded when drawn */
    jf_art art[JF_ART_SLOTS];uint8_t art_slot;uint16_t art_used;uint32_t art_clock;
    uint8_t *art_bytes;uint16_t art_cap;
    /* Player */
    jf_avi avi;uint8_t chunk[512];size_t chunk_size,chunk_used;
    uint8_t playing,paused,frame_ready,ended,stream_state,osd,softened,slowed;
    uint32_t start_s,clock_ms,shown,dropped,last_progress_ms,osd_until,stall_ms,osd_drawn;
    uint64_t base_frame,at_frame; /* at_frame: last picture shown or skipped */
    uint32_t bitrate,window_ms,window_shown,window_dropped,window_rebased,smooth_ms;
    /* Pacing: anchored once the clock is set to a picture; pictured once any picture of this
     * play is up (messages then go in the strip); frame_waited: the picture being assembled
     * had to wait for bytes (the network is behind, not the decoder); cycle_ms: measured time
     * from one shown picture to the next loop (decode + overlay send + loop), which sets the
     * frame rate asked of the server (fps_cap, kept between plays like the bitrate). */
    uint8_t anchored,pictured,frame_waited,drops_in_row,measuring;
    uint32_t cycle_ms,shown_at,adapt_at,rebased,fps_cap,play_frame_ms,reconnect_mark;
    uint8_t want,reconnects;uint32_t reconnect_at,reconnect_pos,play_from; /* work waiting for the one request slot */
    /* Sound: on once the stream's audio is IMA ADPCM mono and the sink started; tried once per
     * play; dead when the sink stopped taking bytes (video goes on alone). played/played_at:
     * the sink's frame count and when it last moved; full_at: since when the sink refuses. */
    uint8_t audio_on,audio_tried,audio_dead,audio_clock;
    uint32_t audio_rate,audio_played,audio_played_at,audio_full_at,audio_blocks,audio_fallbacks;
    uint32_t audio_try_ms,audio_base_ms,audio_retries; /* first start attempt of this segment; media time of the block the sink started on */
    uint32_t audio_catchup; /* late pictures skipped to catch up with the sound clock (diagnostic; never "dropped") */
    /* Sound starvation (JF_AUDIO_STARVE_PERMILLE): samples played and the time since the sound clock
     * last went live, was unpaused or the window rolled; starved: step-downs for it; rate_cap: the
     * bitrate that starved, never earned back. */
    uint32_t starve_mark,starve_ms,starved,rate_cap,window_waits; /* window_waits: loops the network had no bytes for */
    /* Reporting session (resume/watched) stays for the whole playback; the stream's own playSessionId is
     * fresh on every (re)open, because Jellyfin serves a transcode whose playSessionId it has seen from
     * that file's first byte (the "resets to the start" seen on a reopen): see jf_stream_start. */
    char session[17],stream_session[17];uint32_t stream_seq;
    /* Chosen tracks for the next stream (RAM only): -1 means the server's default / none. */
    int16_t audio_index,sub_index;uint8_t streams_for_play,streams_done;
    /* 1.3.2: the title's track list (tracks_ok once read), the totals the server listed, what this stream asked for,
     * a timed note on the picture (note_warn: yellow), the mid-play switch (switching: the stream is closed while the
     * track list is fetched again; switch_pos: where it reopens), the menu opened from the player (menu_paused: it
     * paused the video; menu_lang/menu_subs: the choices it opened with), subtitles that failed for this title, and
     * tracks_seq/tracks_line: the diagnostics line, renewed with every choice. */
    jf_track audio_tracks[JF_TRACKS],sub_tracks[JF_TRACKS];uint8_t audio_count,sub_count,audio_total,sub_total,tracks_ok;
    uint8_t note_warn,switching,menu_paused,menu_lang,menu_subs,subs_failed,stream_subs,chosen_lang,chosen_subs;
    uint32_t note_until,switch_pos,open_ms,open_shown,tracks_seq;
    char message[56],detail_line[64],note[56]; /* 1.6.4: detail_line 48 -> 64 for the next-step lines */
    /* One buffer: the reply of the outstanding request while browsing, the incoming JPEG
     * while playing (no artwork or library request runs during playback). */
    uint8_t buf[JF_BUF];size_t reply_size;
    pj_json_token *tokens;unsigned token_cap;
    /* Jellyfin menu (Select) and the settings it holds: RAM only, never flash or NVS.
     * audio_lang: 0 English (default), 1 Original, 2 Japanese. subs: always 0 (Off) since 1.2.7: burned-in
     * subtitles fail on the owner's server, so the menu row is hidden; the stream code stays for later. */
    uint8_t menu_from,menu_sel,menu_page,big_cards,audio_lang,subs;
    uint8_t exit_to;                  /* JF_EXIT_*: read by the launcher when screen==JF_S_EXIT */
    uint8_t err_kind;                 /* JF_ERR_*: what Select offers on the error screen (1.3.0) */
    uint8_t link_down;uint32_t link_lost_ms; /* the platform's Wi-Fi link is down; since when a lost stream waits for it */
    /* The focused card's name scrolls when it does not fit (jf_menu.h); draw_part says what
     * the last tick asked jf_app_draw to paint. */
    uint8_t draw_part;int16_t scroll_px;uint32_t scroll_at,scroll_key;
    /* 1.6.0, TV and ChromaTube. source_id / live_id: what PlaybackInfo named for the title being opened (URL-encoded;
     * empty: the item's own id, no live stream). playinfo_for_play: the PlaybackInfo that is out is still wanted.
     * tv_opened: a stream of this title was asked for (its conversion is stopped on leaving). tv_close: JF_CLOSE_*,
     * the server closes still owed; nothing is opened while any is. tv_state: JF_TV_*; tv_at: when the surf or the
     * reopen wait ends, when "No signal" goes, when the exit began. tv_closed_*: the last two channels closed and
     * when (JF_TV_REOPEN_MS). tv_exit: leaving once the closes are out (1 the start-up menu, 2 the Local Weather
     * channel). tv_list_err: JF_TV_ERR_*, the list could not be read (the weather row and "Try again" stay).
     * tv_stepped: this session's one step down was used. pi: the PlaybackInfo reply scanner (jf_pi_feed).
     * thin_seen / frame_new: pictures delivered in this stream, and "this one was not yet counted" (jf_thin). */
    char source_id[JF_SOURCE_ID],live_id[JF_LIVE_ID],tv_closed_id[2][33];
    uint8_t tv_state,tv_close,tv_opened,tv_exit,tv_banner_armed,tv_closed_next,playinfo_for_play,tv_list_err,tv_stepped,tv_resume,frame_new,frame_check,thin_on,tv_starved,tv_step_tune;
    char playinfo_id[33]; /* the title the PlaybackInfo that is out was asked for */
    struct {uint8_t depth,in_str,esc,uni,after_str,expect,in_sources,elem,capture,dest,klen,kover,got_id,got_live,over;
            uint16_t code,id_len,live_len;char key[16];} pi;
    uint32_t tv_at,tv_closed_ms[2],tv_banner_until,tv_open_ms;
    uint32_t thinned,thin_seen; /* pictures of a faster-than-asked stream left out on purpose (jf_thin), this play */
    /* ADPCM bytes and blocks the sink took in this stream. A mono IMA block of n bytes is 2n-7 samples (a 4-byte
     * header carrying one sample), so the samples fed are 2*bytes - 7*blocks; less audio_played: the sound queued. */
    uint32_t audio_fed,audio_fed_blocks;
} jf_app;

/* ---------- small helpers ---------- */
static inline void jf_copy(char *out,size_t cap,const char *in){if(!cap)return;size_t n=strlen(in);if(n>=cap)n=cap-1;memcpy(out,in,n);out[n]=0;}
static inline int jf_hex_id(const char *s){size_t n=strlen(s);if(n!=32)return 0;for(size_t i=0;i<n;i++){char c=s[i];if(!((c>='0'&&c<='9')||(c>='a'&&c<='f')))return 0;}return 1;}
static inline void jf_tv_nosignal(jf_app *a,const char *why);
static inline void jf_tv_tune(jf_app *a);
static inline void jf_fail(jf_app *a,const char *why) {
    /* 1.6.0, TV: whatever goes wrong with a channel is "No signal" and the list, never the error page */
    if(a->cfg.mode==JF_MODE_TV&&a->screen==JF_S_PLAYER){if(a->tv_state!=JF_TV_NOSIGNAL)jf_tv_nosignal(a,"No signal");return;}
    jf_copy(a->message,sizeof(a->message),why);a->detail_line[0]=0;a->err_kind=JF_ERR_OTHER;a->screen=JF_S_ERROR;a->redraw=1;
}
/* 1.6.4: a ChromaTube video that fails before its first picture: the owner's ChromaTube tool (which the server asks for
 * the video) is the likely cause, so it is named with the next step; otherwise `why`, as before. */
static inline void jf_fail_video(jf_app *a,const char *why,int before_picture) {
    if(a->cfg.mode==JF_MODE_TUBE&&before_picture) {
        jf_fail(a,JF_MSG_TUBE_DOWN);jf_copy(a->detail_line,sizeof(a->detail_line),JF_MSG_TUBE_DOWN_STEP);return;
    }
    jf_fail(a,why);
}
/* 1.6.0: an id from the server as it goes into a query (unreserved characters kept, the rest %XX). 0 when it does
 * not fit: a cut id would name something else. */
static inline int jf_urlenc(char *out,size_t cap,const char *in) {
    size_t n=0;
    for(;*in;in++) {
        unsigned char c=(unsigned char)*in;
        int plain=(c>='0'&&c<='9')||(c>='a'&&c<='z')||(c>='A'&&c<='Z')||c=='-'||c=='_'||c=='.'||c=='~';
        if(n+(plain?1u:3u)>=cap){out[0]=0;return 0;}
        if(plain)out[n++]=(char)c;
        else{out[n++]='%';out[n++]="0123456789ABCDEF"[c>>4];out[n++]="0123456789ABCDEF"[c&15];}
    }
    out[n]=0;return 1;
}
/* One request at a time: a new one never starts while another is outstanding, so a reply
 * is never mixed into the next request's buffer. Work that must not be lost goes through jf_do. */
static inline int jf_get(jf_app *a,uint8_t kind,const char *path) {
    if(a->request!=JF_R_NONE)return -1;
    a->request=kind;a->reply_size=0;a->request_ms=a->now;
    if(a->io.request(a->io.ctx,NULL,path,NULL)!=0){a->request=JF_R_NONE;return -1;}
    return 0;
}
static inline int jf_post(jf_app *a,uint8_t kind,const char *path,const char *body) {
    if(a->request!=JF_R_NONE)return -1;
    a->request=kind;a->reply_size=0;a->request_ms=a->now;
    if(a->io.request(a->io.ctx,"POST",path,body)!=0){a->request=JF_R_NONE;return -1;}
    return 0;
}

/* Common query: one image of each kind, user data for resume points, the year. */
#define JF_FIELDS "fields=ProductionYear&enableImageTypes=Primary,Thumb,Backdrop&imageTypeLimit=1&enableUserData=true"
/* Grids never draw a backdrop: leaving its tag (and blur hash) out cuts ~15% of each page. */
#define JF_FIELDS_GRID "fields=ProductionYear&enableImageTypes=Primary,Thumb&imageTypeLimit=1&enableUserData=true"

static inline uint8_t jf_type(const char *t) {
    if(!strcmp(t,"Movie"))return JF_T_MOVIE;
    if(!strcmp(t,"Episode"))return JF_T_EPISODE;
    if(!strcmp(t,"Series"))return JF_T_SERIES;
    if(!strcmp(t,"Season"))return JF_T_SEASON;
    if(!strcmp(t,"Video")||!strcmp(t,"MusicVideo"))return JF_T_VIDEO;
    if(!strcmp(t,"CollectionFolder")||!strcmp(t,"Folder")||!strcmp(t,"UserView")||!strcmp(t,"BoxSet"))return JF_T_FOLDER;
    return JF_T_OTHER;
}
static inline int jf_playable(const jf_item *i){return i->type==JF_T_MOVIE||i->type==JF_T_EPISODE||i->type==JF_T_VIDEO;}
static inline int jf_has_tag(const pj_json *j,int obj,const char *kind) {
    int tags=pj_json_find(j,obj,"ImageTags");return tags>=0&&pj_json_find(j,tags,kind)>=0;
}
static inline int jf_nonempty(const pj_json *j,int obj,const char *key) {
    int t=pj_json_find(j,obj,key);return t>=0&&j->tokens[t].type==PJ_JSON_ARRAY&&j->tokens[t].next>(unsigned)t+1;
}

/* One item object -> jf_item. Artwork follows scripts/jellyfin_client.py artwork(): wide art
 * (Thumb, episode still, backdrop) for thumb cards and the hero; Primary (series poster for
 * episodes) for poster cards. */
static inline int jf_parse_item(const pj_json *j,int obj,jf_item *out,uint8_t shape) {
    memset(out,0,sizeof(*out));
    char type[24];
    if(!pj_json_text(j,pj_json_find(j,obj,"Id"),out->id,sizeof(out->id))||!jf_hex_id(out->id))return 0;
    pj_json_text(j,pj_json_find(j,obj,"Name"),out->name,sizeof(out->name));
    pj_json_text(j,pj_json_find(j,obj,"Type"),type,sizeof(type));
    out->type=jf_type(type);
    if(pj_json_true(j,pj_json_find(j,obj,"IsFolder"))&&out->type==JF_T_OTHER)out->type=JF_T_FOLDER;
    uint64_t n;
    if(pj_json_u64(j,pj_json_find(j,obj,"ProductionYear"),&n)&&n<3000)out->year=(uint16_t)n;
    if(pj_json_u64(j,pj_json_find(j,obj,"RunTimeTicks"),&n))out->runtime_s=(uint32_t)(n/10000000u);
    int ud=pj_json_find(j,obj,"UserData");
    if(ud>=0) {
        if(pj_json_u64(j,pj_json_find(j,ud,"PlaybackPositionTicks"),&n))out->resume_s=(uint32_t)(n/10000000u);
        out->played=(uint8_t)pj_json_true(j,pj_json_find(j,ud,"Played"));
    }
    char series_id[33]="";
    if(out->type==JF_T_EPISODE) {
        char series[32];uint64_t s=0,e=0;
        pj_json_text(j,pj_json_find(j,obj,"SeriesName"),series,sizeof(series));
        pj_json_text(j,pj_json_find(j,obj,"SeriesId"),series_id,sizeof(series_id));
        int hs=pj_json_u64(j,pj_json_find(j,obj,"ParentIndexNumber"),&s),he=pj_json_u64(j,pj_json_find(j,obj,"IndexNumber"),&e);
        if(hs&&he){ /* "S1:E5 - Name", as the Pocket's subtitle */
            snprintf(out->sub,sizeof(out->sub),"S%u:E%u - ",(unsigned)(s%1000),(unsigned)(e%10000));
            size_t used=strlen(out->sub);jf_copy(out->sub+used,sizeof(out->sub)-used,out->name);}
        else jf_copy(out->sub,sizeof(out->sub),out->name);
        if(series[0])jf_copy(out->name,sizeof(out->name),series);
    }
    int primary=jf_has_tag(j,obj,"Primary"),thumb=jf_has_tag(j,obj,"Thumb"),backdrop=jf_nonempty(j,obj,"BackdropImageTags");
    char parent_backdrop[33]="";
    if(jf_nonempty(j,obj,"ParentBackdropImageTags"))pj_json_text(j,pj_json_find(j,obj,"ParentBackdropItemId"),parent_backdrop,sizeof(parent_backdrop));
    /* hero: backdrop (own, then the series'), then wide art */
    if(backdrop){jf_copy(out->hero_id,33,out->id);out->hero_kind=JF_IMG_BACKDROP;}
    else if(jf_hex_id(parent_backdrop)){jf_copy(out->hero_id,33,parent_backdrop);out->hero_kind=JF_IMG_BACKDROP;}
    else if(thumb){jf_copy(out->hero_id,33,out->id);out->hero_kind=JF_IMG_THUMB;}
    /* card */
    jf_copy(out->card_id,33,out->id);
    if(shape==JF_SHAPE_THUMB) {
        if(thumb)out->card_kind=JF_IMG_THUMB;
        else if(out->type==JF_T_EPISODE&&primary)out->card_kind=JF_IMG_PRIMARY;
        else if(backdrop)out->card_kind=JF_IMG_BACKDROP;
        else if(jf_hex_id(parent_backdrop)){jf_copy(out->card_id,33,parent_backdrop);out->card_kind=JF_IMG_BACKDROP;}
        else if(primary)out->card_kind=JF_IMG_PRIMARY;
    } else {
        if(out->type==JF_T_EPISODE&&jf_hex_id(series_id)&&pj_json_find(j,obj,"SeriesPrimaryImageTag")>=0)
            {jf_copy(out->card_id,33,series_id);out->card_kind=JF_IMG_PRIMARY;}
        else if(primary)out->card_kind=JF_IMG_PRIMARY;
        else if(thumb)out->card_kind=JF_IMG_THUMB;
    }
    return 1;
}
/* 1.6.0, TV and ChromaTube (channel and video names come from anywhere): a name as the console's ASCII font can
 * show it. Latin-1 letters lose their accents, the usual typographic marks become plain ones, everything else
 * outside ASCII (other scripts, emoji) is left out; doubled and leading spaces go. In place. */
static inline void jf_ascii(char *s) {
    static const char *const latin[64]={"A","A","A","A","A","A","AE","C","E","E","E","E","I","I","I","I","D","N","O","O","O","O","O","x",
        "O","U","U","U","U","Y","Th","ss","a","a","a","a","a","a","ae","c","e","e","e","e","i","i","i","i","d","n","o","o","o","o","o","/",
        "o","u","u","u","u","y","th","y"};
    const unsigned char *in=(const unsigned char *)s;char *out=s;
    while(*in) {
        uint32_t c=*in++;unsigned more=0;const char *put=NULL;char one[2]={0,0};
        if(c>=0xf0u){c&=7u;more=3;}else if(c>=0xe0u){c&=15u;more=2;}else if(c>=0xc0u){c&=31u;more=1;}else if(c>=0x80u)continue;
        for(;more&&(*in&0xc0u)==0x80u;more--)c=(c<<6)|(*in++&0x3fu);
        if(more)continue; /* a broken sequence */
        if(c<0x80u){one[0]=(char)(c<32u||c==127u?' ':c);put=one;}
        else if(c>=0xc0u&&c<=0xffu)put=latin[c-0xc0u];
        else if(c==0xa0u)put=" ";
        else if(c==0x2018u||c==0x2019u||c==0x2032u)put="'";
        else if(c==0x201cu||c==0x201du||c==0x2033u||c==0xabu||c==0xbbu)put="\"";
        else if(c==0x2010u||c==0x2011u||c==0x2012u||c==0x2013u||c==0x2014u||c==0x2212u)put="-";
        else if(c==0x2026u)put="...";
        else if(c==0x2022u||c==0xb7u)put="-";
        else continue; /* nothing the font has */
        /* never longer than the source: each replacement is at most as many bytes as the UTF-8 it stands for */
        for(;*put;put++){if(*put==' '&&(out==s||out[-1]==' '))continue;*out++=*put;}
    }
    while(out>s&&out[-1]==' ')out--;
    *out=0;
}
/* An items reply ({"Items":[...]} or a bare array) into `items`. Returns the count, -1 bad. */
static inline int jf_parse_list(jf_app *a,jf_item *items,unsigned cap,uint8_t shape,uint32_t *total) {
    pj_json j;
    if(!a->tokens||!pj_json_parse(&j,a->buf,a->reply_size,a->tokens,a->token_cap))return -1;
    int list=0;
    if(j.tokens[0].type==PJ_JSON_OBJECT) {
        list=pj_json_find(&j,0,"Items");
        uint64_t t;if(total&&pj_json_u64(&j,pj_json_find(&j,0,"TotalRecordCount"),&t))*total=(uint32_t)t;
    }
    if(list<0||j.tokens[list].type!=PJ_JSON_ARRAY)return -1;
    unsigned count=0;
    for(unsigned i=(unsigned)list+1;i<j.tokens[list].next&&count<cap;i=j.tokens[i].next) {
        if(j.tokens[i].type!=PJ_JSON_OBJECT)continue;
        if(jf_parse_item(&j,(int)i,&items[count],shape)) {
            if(a->cfg.mode!=JF_MODE_JELLYFIN){jf_ascii(items[count].name);jf_ascii(items[count].sub);} /* 1.6.0: TV and ChromaTube names */
            count++;
        }
    }
    return (int)count;
}

/* ---------- requests ---------- */
enum {JF_W_NONE,JF_W_HOME,JF_W_FOLDER,JF_W_DETAILS,JF_W_PLAY,JF_W_TRACKS,JF_W_CHANNELS};
static inline void jf_play_start(jf_app *a);
static inline void jf_switch_finish(jf_app *a);
/* Starts the work now if the request slot is free, else when it frees (newest wins). */
static inline void jf_do(jf_app *a,uint8_t want) {
    char path[300];
    a->busy=0; /* newer work replaces a pending offline retry */
    if(want==JF_W_HOME&&a->cfg.mode==JF_MODE_TV){want=JF_W_CHANNELS;a->screen=JF_S_CHANNELS;a->redraw=1;} /* 1.6.0: TV's Home is the channel list */
    if(a->request!=JF_R_NONE){a->want=want;return;}
    a->want=JF_W_NONE;
    switch(want) {
    case JF_W_HOME:
        a->row_count=0;a->row_focus=0;a->views.count=0;a->view_next=0;
        snprintf(path,sizeof(path),"/UserViews?userId=%s",a->cfg.user);jf_get(a,JF_R_VIEWS,path);break;
    case JF_W_FOLDER:
        snprintf(path,sizeof(path),"/Items?userId=%s&parentId=%s&startIndex=%u&limit=%u&sortBy=SortName&sortOrder=Ascending&%s",
                 a->cfg.user,a->folder_id,(unsigned)a->folder_start,JF_PAGE_ITEMS,JF_FIELDS_GRID);
        jf_get(a,JF_R_FOLDER,path);break;
    case JF_W_DETAILS:
        /* The list form: /Items/{id} sends the whole record (cast, streams, chapters), up to 60 KB on the
         * owner's server; this asks only for what Details shows (~2.5 KB). */
        snprintf(path,sizeof(path),"/Items?userId=%s&ids=%s&fields=Overview,Genres,ProductionYear&enableUserData=true"
                 "&enableImageTypes=Primary,Thumb,Backdrop&imageTypeLimit=1",a->cfg.user,a->detail.id);
        jf_get(a,JF_R_DETAILS,path);break;
    case JF_W_CHANNELS: /* 1.6.0: one page of the channel list: no images, no user data, no programme (~0.5 KB a channel) */
        snprintf(path,sizeof(path),"/LiveTv/Channels?userId=%s&startIndex=%u&limit=%u&enableImages=false&enableUserData=false"
                 "&addCurrentProgram=false",a->cfg.user,(unsigned)a->folder_start,JF_TV_PAGE);
        jf_get(a,JF_R_CHANNELS,path);break;
    case JF_W_PLAY:
        if(a->cfg.mode!=JF_MODE_JELLYFIN) {
            /* 1.6.0, TV and ChromaTube: PlaybackInfo first (it opens a live channel; it makes the server learn a remote
             * video's size and sound), then the stream with the media source it names. Never while a close is owed. */
            char body[440];
            if(a->tv_close!=JF_CLOSE_NONE){a->want=want;break;} /* the tick asks again */
            if(!a->bitrate||a->bitrate>JF_RATE_START_MODE(a))a->bitrate=JF_RATE_START_MODE(a); /* review B1 */
            jf_copy(a->playinfo_id,sizeof(a->playinfo_id),a->detail.id);
            snprintf(path,sizeof(path),"/Items/%s/PlaybackInfo?userId=%s",a->detail.id,a->cfg.user);
            snprintf(body,sizeof(body),JF_PLAYINFO_BODY,a->cfg.user,(unsigned)a->bitrate,(unsigned)a->bitrate,
                     a->cfg.audio?"adpcm_ima_wav":"mp3");
            a->source_id[0]=0;a->live_id[0]=0;a->playinfo_for_play=1;
            memset(&a->pi,0,sizeof(a->pi)); /* its reply is read as it arrives (jf_pi_feed), not kept */
            if(jf_post(a,JF_R_PLAYINFO,path,body)!=0) {
                a->playinfo_for_play=0;
                if(a->cfg.mode==JF_MODE_TV)jf_tv_nosignal(a,"No signal");
                else{a->screen=JF_S_DETAILS;jf_fail(a,"Could not start the video");}
            }
            break;
        }
        /* English audio (and, when the owner turns them on, English subtitles) need the track list.
         * One lean request, only MediaStreams, so it fits the reply buffer; parsed in jf_pick_streams.
         * Only when it can matter: the FPGA can play sound, or subtitles are on (burned into the video). */
        if((a->cfg.audio||a->subs)&&!a->streams_done&&a->detail.id[0]) {
            a->streams_for_play=1;
            snprintf(path,sizeof(path),"/Items?userId=%s&ids=%s&fields=MediaStreams&enableUserData=false",a->cfg.user,a->detail.id);
            if(jf_get(a,JF_R_STREAMS,path)==0)break;
        }
        jf_play_start(a);break;
    case JF_W_TRACKS: /* 1.3.2: mid-play switch without a track list (the first fetch failed): fetch it now */
        snprintf(path,sizeof(path),"/Items?userId=%s&ids=%s&fields=MediaStreams&enableUserData=false",a->cfg.user,a->detail.id);
        if(jf_get(a,JF_R_STREAMS,path)!=0){a->switching=0;jf_switch_finish(a);}
        break;
    }
}
static inline void jf_load_home(jf_app *a){jf_do(a,JF_W_HOME);}
static inline void jf_load_folder(jf_app *a){jf_do(a,JF_W_FOLDER);}
static inline void jf_open_folder(jf_app *a,const jf_item *it) {
    if(a->screen==JF_S_FOLDER&&a->depth<4) {
        jf_copy(a->parents[a->depth],33,a->folder_id);jf_copy(a->parent_titles[a->depth],32,a->folder_title);
        a->parent_start[a->depth]=a->folder_start;a->depth++;
    } else if(a->screen!=JF_S_FOLDER)a->depth=0;
    jf_copy(a->folder_id,sizeof(a->folder_id),it->id);jf_copy(a->folder_title,sizeof(a->folder_title),it->name);
    a->folder_start=0;a->folder_total=0;a->folder.count=0;a->folder.focus=0;a->screen=JF_S_FOLDER;a->redraw=1;
    jf_load_folder(a);
}
static inline void jf_open_details(jf_app *a,const jf_item *it) {
    if(a->screen==JF_S_HOME||a->screen==JF_S_FOLDER)a->detail_from=a->screen;
    a->detail=*it;a->overview[0]=a->genres[0]=0;a->detail_action=0;a->screen=JF_S_DETAILS;a->redraw=1;
    a->streams_done=0;a->audio_index=-1;a->sub_index=-1; /* a different title: its track list is fetched before play */
    a->tracks_ok=0;a->audio_count=a->sub_count=a->audio_total=a->sub_total=0;a->subs_failed=0;
    jf_do(a,JF_W_DETAILS);
}

/* ---------- artwork ---------- */
static inline void jf_shape_size(uint8_t shape,int *w,int *h) {
    if(shape==JF_SHAPE_THUMB){*w=48;*h=27;}else if(shape==JF_SHAPE_POSTER){*w=32;*h=48;}
    else if(shape==JF_SHAPE_THUMB_L){*w=72;*h=40;}else if(shape==JF_SHAPE_POSTER_L){*w=42;*h=63;}
    else{*w=JF_W;*h=JF_HERO_H;}
}
/* The shape a row or page is drawn (and its artwork fetched) at under the current layout. */
static inline uint8_t jf_display_shape(const jf_app *a,uint8_t shape) {
    if(!a->big_cards)return shape;
    return shape==JF_SHAPE_THUMB?JF_SHAPE_THUMB_L:shape==JF_SHAPE_POSTER?JF_SHAPE_POSTER_L:shape;
}
static inline int jf_is_thumb(uint8_t shape){return shape==JF_SHAPE_THUMB||shape==JF_SHAPE_THUMB_L;}
static inline const jf_art *jf_art_find(const jf_app *a,const char *id,uint8_t kind,uint8_t shape) {
    for(unsigned i=0;i<JF_ART_SLOTS;i++)
        if(a->art[i].used&&a->art[i].kind==kind&&a->art[i].shape==shape&&!strcmp(a->art[i].id,id))return &a->art[i];
    return NULL;
}
/* The binding hands the artwork arena over (or takes it away while a video plays): every
 * cached picture is forgotten and fetched again when a screen needs it. */
static inline void jf_art_reset(jf_app *a,uint8_t *bytes,uint16_t cap) {
    memset(a->art,0,sizeof(a->art));a->art_used=0;a->art_slot=0;
    a->art_bytes=bytes;a->art_cap=bytes&&cap<=JF_ART_MAX?cap:0;a->redraw=1;
}
/* Folder grid geometry (the Pocket's library grid, scaled): 3 thumbs or 4 posters across;
 * "Bigger cards": 2 thumbs (2 rows) or 3 posters (1 row) across. */
typedef struct {int w,h,left,top,pitch_x,pitch_y;unsigned cols,rows;uint8_t shape;} jf_grid;
static inline jf_grid jf_grid_of(const jf_app *a,uint8_t base) {
    jf_grid g;g.shape=jf_display_shape(a,base);jf_shape_size(g.shape,&g.w,&g.h);g.top=19;g.pitch_y=g.h+14;
    switch(g.shape) {
    case JF_SHAPE_THUMB:g.cols=3;g.rows=3;g.left=4;g.pitch_x=52;break;
    case JF_SHAPE_THUMB_L:g.cols=2;g.rows=2;g.left=6;g.pitch_x=76;break;
    case JF_SHAPE_POSTER_L:g.cols=3;g.rows=1;g.left=8;g.pitch_x=51;break;
    default:g.cols=4;g.rows=2;g.left=5;g.pitch_x=38;break;
    }
    return g;
}
static inline unsigned jf_columns(const jf_app *a,uint8_t shape){return jf_grid_of(a,shape).cols;}
static inline unsigned jf_first_row(const jf_app *a,const jf_page *p) {
    jf_grid g=jf_grid_of(a,p->shape);unsigned row=p->focus/g.cols;
    return g.rows<=1?row:row?row-1:0;
}
/* Home row geometry: cards visible across and their pitch (one more peeks in from the right). */
typedef struct {int w,h,pitch;unsigned visible,first;uint8_t shape;} jf_strip;
static inline jf_strip jf_strip_of(const jf_app *a,const jf_row *r) {
    jf_strip s;s.shape=jf_display_shape(a,r->shape);jf_shape_size(s.shape,&s.w,&s.h);
    s.visible=s.shape==JF_SHAPE_THUMB?3u:s.shape==JF_SHAPE_THUMB_L?2u:s.shape==JF_SHAPE_POSTER_L?3u:4u;
    s.pitch=s.w+(jf_is_thumb(s.shape)?4:6);
    s.first=r->focus>=s.visible?r->focus-(s.visible-1):0;
    return s;
}
typedef struct {const char *id;uint8_t kind,shape;} jf_want;
static inline unsigned jf_wanted(const jf_app *a,jf_want *out,unsigned cap) {
    unsigned n=0;
    /* Under the Jellyfin menu, the screen it came from keeps its pictures (and keeps loading them). */
    uint8_t screen=a->screen==JF_S_MENU?a->menu_from:a->screen;
#define JF_WANT(it_id,it_kind,it_shape) do{if((it_kind)&&n<cap){out[n].id=(it_id);out[n].kind=(it_kind);out[n].shape=(it_shape);n++;}}while(0)
    if(screen==JF_S_HOME&&a->row_count) {
        const jf_row *r=&a->rows[a->row_focus];const jf_item *f=&r->items[r->focus];
        JF_WANT(f->hero_id,f->hero_kind,JF_SHAPE_HERO);
        jf_strip s=jf_strip_of(a,r);
        for(unsigned i=s.first;i<r->count&&i<s.first+s.visible+1;i++)JF_WANT(r->items[i].card_id,r->items[i].card_kind,s.shape);
    } else if(screen==JF_S_FOLDER) {
        const jf_page *p=&a->folder;jf_grid g=jf_grid_of(a,p->shape);unsigned first=jf_first_row(a,p);
        for(unsigned i=first*g.cols;i<p->count&&i<(first+g.rows)*g.cols;i++)
            JF_WANT(p->items[i].card_id,p->items[i].card_kind,g.shape);
    } else if(screen==JF_S_DETAILS) {
        JF_WANT(a->detail.hero_id,a->detail.hero_kind,JF_SHAPE_HERO);
        JF_WANT(a->detail.card_id,a->detail.card_kind,JF_SHAPE_POSTER);
    }
#undef JF_WANT
    return n;
}
/* Makes room for `size` bytes: drops artwork not on screen, oldest first, then packs. */
static inline int jf_art_room(jf_app *a,size_t size,const jf_want *want,unsigned wanted) {
    if(size>a->art_cap)return -1;
    for(;;) {
        if((size_t)(a->art_cap-a->art_used)>=size) {
            for(unsigned i=0;i<JF_ART_SLOTS;i++)if(!a->art[i].used)return (int)i;
        }
        int victim=-1;
        for(unsigned i=0;i<JF_ART_SLOTS;i++) {
            if(!a->art[i].used)continue;
            int needed=0;for(unsigned k=0;k<wanted;k++)if(a->art[i].kind==want[k].kind&&a->art[i].shape==want[k].shape&&!strcmp(a->art[i].id,want[k].id))needed=1;
            if(!needed&&(victim<0||a->art[i].touched<a->art[victim].touched))victim=(int)i;
        }
        if(victim<0)return -1;
        /* remove and pack the bytes after it */
        jf_art *v=&a->art[victim];uint16_t off=v->off,len=v->len;
        memmove(a->art_bytes+off,a->art_bytes+off+len,a->art_used-off-len);a->art_used=(uint16_t)(a->art_used-len);
        for(unsigned i=0;i<JF_ART_SLOTS;i++)if(a->art[i].used&&a->art[i].off>off)a->art[i].off=(uint16_t)(a->art[i].off-len);
        memset(v,0,sizeof(*v));
    }
}
/* Starts the next missing artwork for what is on screen. */
static inline void jf_next_image(jf_app *a) {
    if(a->request!=JF_R_NONE||a->playing||a->busy||a->want||!a->art_cap)return;
    jf_want want[12];unsigned wanted=jf_wanted(a,want,12);
    for(unsigned k=0;k<wanted;k++) {
        if(jf_art_find(a,want[k].id,want[k].kind,want[k].shape))continue;
        unsigned slot=JF_ART_SLOTS;for(unsigned i=0;i<JF_ART_SLOTS;i++)if(!a->art[i].used){slot=i;break;}
        if(slot==JF_ART_SLOTS){int r=jf_art_room(a,0,want,wanted);if(r<0)return;slot=(unsigned)r;}
        jf_art *s=&a->art[slot];memset(s,0,sizeof(*s));
        jf_copy(s->id,sizeof(s->id),want[k].id);s->kind=want[k].kind;s->shape=want[k].shape;s->used=1;a->art_slot=(uint8_t)slot;
        static const char *const kinds[]={"","Primary","Thumb","Backdrop"};
        int w,h;jf_shape_size(want[k].shape,&w,&h);
        char path[160];
        snprintf(path,sizeof(path),"/Items/%s/Images/%s?fillWidth=%d&fillHeight=%d&quality=%d&format=Jpg",
                 want[k].id,kinds[want[k].kind],w,h,want[k].shape==JF_SHAPE_HERO?60:80);
        if(jf_get(a,JF_R_IMAGE,path)!=0)s->used=0;
        return;
    }
}
static inline void jf_art_store(jf_app *a,int ok) {
    jf_art *s=&a->art[a->art_slot];
    if(!s->used)return;
    jf_want want[12];unsigned wanted=jf_wanted(a,want,12);
    s->used=0; /* not counted while making room */
    int room=ok&&a->reply_size>=4&&a->buf[0]==0xff&&a->buf[1]==0xd8?jf_art_room(a,a->reply_size,want,wanted):-1;
    s->used=1;s->touched=++a->art_clock;
    if(room<0){s->ok=0;s->len=0;s->off=a->art_used;return;} /* placeholder: not retried this screen */
    memcpy(a->art_bytes+a->art_used,a->buf,a->reply_size);
    s->off=a->art_used;s->len=(uint16_t)a->reply_size;s->ok=1;a->art_used=(uint16_t)(a->art_used+a->reply_size);
}

/* ---------- playback ---------- */
static inline void jf_hex16(char *out,uint32_t x) {
    for(int i=0;i<16;i++){x^=x<<13;x^=x>>17;x^=x<<5;out[i]="0123456789abcdef"[x&15];}
    out[16]=0;
}
static inline void jf_session_id(jf_app *a){jf_hex16(a->session,a->now*2654435761u^(uint32_t)(uintptr_t)a^0x9e3779b9u);}
/* A fresh playSessionId for every stream (re)open. The sequence number makes it unique even when
 * two opens fall in the same millisecond (a seek right after an adapt), so Jellyfin never matches a
 * reopen to an existing transcode file and replay its first segment. */
static inline void jf_stream_session_id(jf_app *a){a->stream_seq++;jf_hex16(a->stream_session,(a->now*2654435761u)^(a->stream_seq*2246822519u)^(uint32_t)(uintptr_t)a);}
static inline uint32_t jf_position_s(const jf_app *a) {
    uint32_t us=jf_avi_frame_us(&a->avi);
    uint64_t frames=a->at_frame>a->base_frame?a->at_frame-a->base_frame:0;
    return a->start_s+(uint32_t)(us?frames*us/1000000u:0);
}
static inline void jf_report(jf_app *a,uint8_t kind) {
    if(a->cfg.mode==JF_MODE_TV)return; /* 1.6.0: live channels are never reported (no watch history) */
    if(a->request!=JF_R_NONE)return; /* best effort; the next report carries the position */
    char body[256];
    uint64_t ticks=(uint64_t)jf_position_s(a)*10000000u;
    snprintf(body,sizeof(body),"{\"ItemId\":\"%s\",\"MediaSourceId\":\"%s\",\"PlaySessionId\":\"%s\",\"PositionTicks\":%llu,\"IsPaused\":%s,\"CanSeek\":true,\"PlayMethod\":\"Transcode\"}",
             a->detail.id,a->detail.id,a->session,(unsigned long long)ticks,a->paused?"true":"false");
    jf_post(a,kind,kind==JF_R_PLAYING?"/Sessions/Playing":kind==JF_R_PROGRESS?"/Sessions/Playing/Progress":"/Sessions/Playing/Stopped",body);
    a->last_progress_ms=a->now;
}
/* ---------- track selection (English audio, English subtitles) ---------- */
/* A Jellyfin language tag (ISO 639, e.g. "eng"/"en", "jpn"/"ja") against a wanted language. */
static inline int jf_lang_is(const char *tag,const char *want2,const char *want3) {
    char t[8];size_t i=0;for(;tag[i]&&i<sizeof(t)-1;i++)t[i]=(char)(tag[i]>='A'&&tag[i]<='Z'?tag[i]+32:tag[i]);t[i]=0;
    return !strncmp(t,want3,3)||(!strncmp(t,want2,2)&&(!t[2]||t[2]=='-'));
}
/* A language tag as the 3-letter code the diagnostics line shows: ISO 639-2 kept, the common 2-letter ones mapped,
 * "und" when empty or not letters. Never a name. */
static inline void jf_lang3(const char *tag,char out[4]) {
    static const char *const two[][2]={{"en","eng"},{"ja","jpn"},{"fr","fre"},{"de","ger"},{"es","spa"},{"it","ita"},
        {"ko","kor"},{"zh","chi"},{"pt","por"},{"ru","rus"}};
    char t[4]={0};size_t n=0;
    for(;tag[n]&&n<3;n++){char c=(char)(tag[n]>='A'&&tag[n]<='Z'?tag[n]+32:tag[n]);if(c<'a'||c>'z')break;t[n]=c;}
    if(n==2&&(!tag[2]||tag[2]=='-')){for(unsigned i=0;i<sizeof(two)/sizeof(two[0]);i++)if(!strcmp(t,two[i][0])){memcpy(out,two[i][1],4);return;}}
    if(n==3&&(!tag[3]||tag[3]=='-')){memcpy(out,t,4);return;}
    memcpy(out,"und",4);
}
/* The track to ask for from the list: audio_lang 0 English, 1 Original (the server default, -1), 2 Japanese;
 * subs 1 English (a text track first: burned in by the server either way). The wanted language's default track
 * wins, else its first. Returns 1 when a wanted language is absent (the note says so). */
static inline int jf_choose_tracks(jf_app *a) {
    a->audio_index=-1;a->sub_index=-1;
    int missing=0;
    if(a->audio_lang!=1) {
        /* Exactly the 1.2.6 rule (the owner's server plays English with it): in list order, the first
         * track in the wanted language, replaced by a later one of that language marked default. */
        uint8_t want=a->audio_lang==2?2u:1u;
        for(unsigned i=0;i<a->audio_count;i++)if((a->audio_tracks[i].want&want)&&(a->audio_index<0||a->audio_tracks[i].def))
            a->audio_index=a->audio_tracks[i].index;
        missing=a->tracks_ok&&a->audio_count&&a->audio_index<0;
    }
    if(a->subs==1&&!a->subs_failed) {
        int best_text=0;
        for(unsigned i=0;i<a->sub_count;i++) {
            const jf_track *t=&a->sub_tracks[i];
            if(!(t->want&1u))continue;
            if(a->sub_index<0||(t->text&&!best_text)||(t->text==best_text&&t->def)){a->sub_index=t->index;best_text=t->text;}
        }
    }
    return missing;
}
/* Reads the MediaStreams reply ({"Items":[{"MediaStreams":[...]}]}) into the title's track list (indexes, codes,
 * default and text flags only), then chooses. */
static inline void jf_pick_streams(jf_app *a) {
    a->audio_index=-1;a->sub_index=-1;a->streams_done=1;
    a->tracks_ok=0;a->audio_count=a->sub_count=a->audio_total=a->sub_total=0;
    pj_json j;
    if(!a->tokens||!pj_json_parse(&j,a->buf,a->reply_size,a->tokens,a->token_cap))return;
    int obj=0;
    if(j.tokens[0].type==PJ_JSON_OBJECT) {
        int list=pj_json_find(&j,0,"Items");
        if(list>=0&&j.tokens[list].type==PJ_JSON_ARRAY&&(unsigned)list+1<j.tokens[list].next)obj=list+1;else obj=0;
    }
    int ms=pj_json_find(&j,obj,"MediaStreams");
    if(ms<0||j.tokens[ms].type!=PJ_JSON_ARRAY)return;
    for(unsigned i=(unsigned)ms+1;i<j.tokens[ms].next;i=j.tokens[i].next) {
        if(j.tokens[i].type!=PJ_JSON_OBJECT)continue;
        char type[12]="",lang[12]="";uint64_t idx=0;
        pj_json_text(&j,pj_json_find(&j,(int)i,"Type"),type,sizeof(type));
        pj_json_text(&j,pj_json_find(&j,(int)i,"Language"),lang,sizeof(lang));
        if(!pj_json_u64(&j,pj_json_find(&j,(int)i,"Index"),&idx)||idx>32767u)continue;
        int audio=!strcmp(type,"Audio"),sub=!strcmp(type,"Subtitle");
        if(!audio&&!sub)continue;
        if(audio&&a->audio_total<255)a->audio_total++;
        if(sub&&a->sub_total<255)a->sub_total++;
        jf_track *t=audio?(a->audio_count<JF_TRACKS?&a->audio_tracks[a->audio_count++]:NULL):
                          (a->sub_count<JF_TRACKS?&a->sub_tracks[a->sub_count++]:NULL);
        if(!t)continue;
        t->index=(int16_t)idx;jf_lang3(lang,t->lang);t->def=(uint8_t)pj_json_true(&j,pj_json_find(&j,(int)i,"IsDefault"));
        t->want=(uint8_t)(jf_lang_is(lang,"en","eng")|(jf_lang_is(lang,"ja","jpn")<<1));
        if(t->want&1u)memcpy(t->lang,"eng",4);else if(t->want&2u)memcpy(t->lang,"jpn",4); /* "English", "en-US": as matched */
        t->text=(uint8_t)pj_json_true(&j,pj_json_find(&j,(int)i,"IsTextSubtitleStream"));
    }
    a->tracks_ok=1;
    jf_choose_tracks(a);
    if(a->audio_total>JF_TRACKS||a->sub_total>JF_TRACKS) { /* more than the list keeps: the 1.2.6 pass over all of them */
        int16_t ai=-1,si=-1;
        for(unsigned i=(unsigned)ms+1;i<j.tokens[ms].next;i=j.tokens[i].next) {
            if(j.tokens[i].type!=PJ_JSON_OBJECT)continue;
            char type[12]="",lang[12]="";uint64_t idx=0;
            pj_json_text(&j,pj_json_find(&j,(int)i,"Type"),type,sizeof(type));
            pj_json_text(&j,pj_json_find(&j,(int)i,"Language"),lang,sizeof(lang));
            if(!pj_json_u64(&j,pj_json_find(&j,(int)i,"Index"),&idx)||idx>32767u)continue;
            int def=pj_json_true(&j,pj_json_find(&j,(int)i,"IsDefault"));
            if(!strcmp(type,"Audio")) {
                if(a->audio_lang==0&&jf_lang_is(lang,"en","eng")){if(ai<0||def)ai=(int16_t)idx;}
                else if(a->audio_lang==2&&jf_lang_is(lang,"ja","jpn")){if(ai<0||def)ai=(int16_t)idx;}
            } else if(!strcmp(type,"Subtitle")&&a->subs==1&&jf_lang_is(lang,"en","eng")){if(si<0||def)si=(int16_t)idx;}
        }
        a->audio_index=ai;a->sub_index=si;
    }
    a->chosen_lang=a->audio_lang;a->chosen_subs=a->subs;
}
/* The diagnostics line for the launcher: counts and codes only, never a title or a track name.
 * audio: the audioStreamIndex asked for (-1: the server's default); lang: that track's code (the default track's
 * when -1; "und" when unknown); of: audio tracks listed; subs: the subtitleStreamIndex asked for (-1 none). */
static inline void jf_tracks_line(const jf_app *a,char *out,size_t size) {
    const char *lang="und";
    for(unsigned i=0;i<a->audio_count;i++) {
        const jf_track *t=&a->audio_tracks[i];
        if(a->audio_index>=0?t->index==a->audio_index:t->def){lang=t->lang;break;}
    }
    if(a->audio_index<0&&!strcmp(lang,"und")&&a->audio_count&&!a->audio_tracks[0].def){
        int any_def=0;for(unsigned i=0;i<a->audio_count;i++)any_def|=a->audio_tracks[i].def;
        if(!any_def)lang=a->audio_tracks[0].lang; /* no default flagged: the server plays the first */
    }
    snprintf(out,size,"jellyfin tracks audio=%d lang=%s of=%u subs=%d",(int)a->audio_index,lang,(unsigned)a->audio_total,
             a->stream_subs?(int)a->sub_index:-1);
}
/* A note on the picture for JF_NOTE_MS (warn: yellow, something did not go as asked). */
static inline void jf_note(jf_app *a,const char *text,int warn) {
    jf_copy(a->note,sizeof(a->note),text);a->note_warn=(uint8_t)warn;a->note_until=a->now+JF_NOTE_MS;if(!a->note_until)a->note_until=1;a->redraw=1;
}
/* After a choice: the result for the owner (a missing language in yellow). */
static inline void jf_tracks_result(jf_app *a,int missing,int switched) {
    if(a->note_until&&a->note_warn)return; /* this stream start already said something went wrong (subtitles) */
    if(missing)jf_note(a,a->audio_lang==2?JF_MSG_NO_JAPANESE:JF_MSG_NO_ENGLISH,1);
    else if(a->subs==1&&!a->subs_failed&&a->tracks_ok&&a->sub_index<0&&switched)jf_note(a,JF_MSG_NO_SUBS,1);
    else if(switched)jf_note(a,a->audio_lang==0?"Audio: English":a->audio_lang==2?"Audio: Japanese":"Audio: Original",0);
}

/* ---------- sound ---------- */
/* The demuxer's audio sink: starts the platform's sound on the first block if the stream's
 * track is IMA ADPCM mono (it is, when the platform asked for it), then passes bytes on.
 * Returns what the sink took; the demuxer offers the rest later (JF_AVI_AUDIO_FULL). */
static inline size_t jf_audio_sink(void *ctx,const uint8_t *data,size_t size,int first) {
    jf_app *a=ctx;
    if(first&&!a->audio_on&&!a->audio_tried) {
        const jf_avi *v=&a->avi;
        int fits=a->cfg.audio&&a->io.audio_start&&a->io.audio_write&&a->io.audio_played&&v->audio_format==JF_WAVE_IMA_ADPCM&&
                 v->audio_channels==1&&v->audio_rate>=8000u&&v->audio_rate<=48000u;
        if(!a->audio_try_ms)a->audio_try_ms=a->now?a->now:1;
        else a->audio_retries++;
        if(fits&&a->io.audio_start(a->io.ctx,v->audio_rate)==0) {
            /* This block belongs to the pictures from here on (FFmpeg writes it before the first picture
             * with a later time), so the sound clock starts at their media time: 0 at a stream start, later
             * when the sink refused the first blocks. */
            uint32_t us=jf_avi_frame_us(v);
            a->audio_base_ms=v->frames&&us?(uint32_t)(((uint64_t)v->frames*us-us/2)/1000u):0;
            a->audio_on=1;a->audio_rate=v->audio_rate;a->audio_played=0;a->audio_played_at=a->now;a->audio_full_at=0;
        } else if(!fits||(uint32_t)(a->now-a->audio_try_ms)>=JF_AUDIO_TRY_MS)a->audio_tried=1; /* this play: video alone */
    }
    if(!a->audio_on||a->audio_dead)return size; /* no sound here: skipped, as before */
    if(first)a->audio_blocks++;
    size_t took=a->io.audio_write(a->io.ctx,data,size,first);
    if(took>size)took=size;
    /* 1.6.0, TV and ChromaTube: the sink takes at most one message (256 bytes, 32 ms of sound) a call, and a short
     * take ends the loop pass as "ring full"; with a picture decoded in most passes that feeds the sound more slowly
     * than it plays. So the rest is offered again at once until the ring really has no room. Jellyfin mode: one
     * call, as before. */
    if(a->thin_on)while(took&&took<size) { /* only for a stream faster than asked (N1) */
        size_t more=a->io.audio_write(a->io.ctx,data+took,size-took,0);
        if(!more||more>size-took)break;
        took+=more;
    }
    a->audio_fed+=(uint32_t)took;if(first&&took)a->audio_fed_blocks++;
    if(took<size){if(!a->audio_full_at)a->audio_full_at=a->now?a->now:1;}
    else a->audio_full_at=0;
    return took;
}
static inline void jf_audio_stop(jf_app *a) {
    if(a->audio_on&&a->io.audio_stop)a->io.audio_stop(a->io.ctx);
    a->audio_on=0;a->audio_tried=0;a->audio_dead=0;a->audio_clock=0;a->audio_try_ms=0;a->audio_base_ms=0;
}
/* Polls the sink's clock; decides whether it is the playback clock right now. */
static inline void jf_audio_poll(jf_app *a) {
    if(!a->audio_on||a->audio_dead){a->audio_clock=0;return;}
    uint32_t played=a->io.audio_played(a->io.ctx);
    if(played!=a->audio_played){a->audio_played=played;a->audio_played_at=a->now;}
    uint32_t still=a->now-a->audio_played_at;
    /* Not while the link is down or a reopen is pending: nothing feeds the ring then, so a sink that is
     * merely waiting must not be declared dead (before 1.2.9: silence for the rest of the title). */
    if(a->audio_full_at&&!a->link_down&&!a->reconnect_at&&(uint32_t)(a->now-a->audio_full_at)>=JF_AUDIO_DEAD_MS&&still>=JF_AUDIO_DEAD_MS) {
        a->audio_dead=1;a->audio_clock=0;a->audio_fallbacks++;a->anchored=0;
        if(a->io.audio_stop)a->io.audio_stop(a->io.ctx); /* the sink is dead: let its I2S channel go (the task and ring stay) */
        return; /* the video goes on alone */
    }
    /* Trusted while samples move (from audio_start on: the clock reads 0 until the first block
     * plays, so the first pictures wait for the sound). Stale: the frame clock, re-anchored, until
     * sound moves again (a Wi-Fi dip starves both; a silent tail or a stuck sink starves only this). */
    int live=still<JF_AUDIO_STALE_MS;
    if(a->audio_clock&&!live){a->audio_clock=0;a->audio_fallbacks++;a->anchored=0;}
    else if(!a->audio_clock&&live){a->audio_clock=1;a->starve_mark=played;a->starve_ms=a->now;}
}
/* Per mille of the wall time since starve_ms that the sound did NOT play (0 while it keeps up, or
 * before JF_AUDIO_JUDGE_MS of live sound clock have passed to judge by). */
static inline uint32_t jf_audio_deficit(const jf_app *a) {
    if(!a->audio_on||a->audio_dead||!a->audio_clock||!a->audio_rate)return 0;
    uint32_t span=a->now-a->starve_ms;
    if(span<JF_AUDIO_JUDGE_MS)return 0;
    uint64_t asked=(uint64_t)span*a->audio_rate/1000u,got=a->audio_played-a->starve_mark;
    return got>=asked?0:(uint32_t)((asked-got)*1000u/asked);
}
/* Media time (ms since the stream start) according to the sound played. */
static inline uint32_t jf_audio_ms(const jf_app *a){return a->audio_base_ms+(uint32_t)((uint64_t)a->audio_played*1000u/a->audio_rate);}
/* With a picture held, run the demuxer on through the sound (and headers) in front of the
 * next picture, so the ring is fed ahead; stops before the next video chunk or when the sink
 * is full. Stream end or failure is left for the main path. */
static inline void jf_audio_ahead(jf_app *a) {
    if(!a->audio_on||a->audio_dead)return;
    a->avi.hold=1;
    for(int guard=0;guard<16;guard++) {
        if(a->chunk_used==a->chunk_size) {
            int n=a->io.stream_read(a->io.ctx,a->chunk,sizeof(a->chunk));
            if(n==0)a->window_waits++;
            if(n<=0)break;
            a->chunk_size=(size_t)n;a->chunk_used=0;
        }
        size_t used;int r=jf_avi_feed(&a->avi,a->chunk+a->chunk_used,a->chunk_size-a->chunk_used,&used);
        a->chunk_used+=used;
        if(r!=JF_AVI_MORE)break;
    }
    a->avi.hold=0;
}

static inline int jf_stream_start(jf_app *a,uint32_t start_s) {
    char path[640];
    if(a->stream_state)a->io.stream_close(a->io.ctx);
    jf_avi_init(&a->avi,a->buf,JF_BUF);jf_avi_audio(&a->avi,jf_audio_sink,a);a->chunk_size=a->chunk_used=0;a->frame_ready=0;a->ended=0;
    a->start_s=start_s;a->base_frame=a->at_frame=0;a->clock_ms=a->now;a->stall_ms=a->now;a->stream_state=1;
    a->anchored=0;a->frame_waited=0;a->drops_in_row=0;a->measuring=0;a->thin_seen=0;a->frame_new=0;a->audio_fed=0;a->audio_fed_blocks=0;a->thin_on=0;
    /* A new segment: queued sound is dropped and the sound clock restarts at 0 with the pictures. A sink
     * declared dead (it refused bytes for a second: an outage, a stall) is revived: its stop was already
     * sent, so it starts again on the segment's first block like a fresh play. */
    if(a->audio_on&&!a->audio_dead&&a->io.audio_flush)a->io.audio_flush(a->io.ctx);
    if(a->audio_dead){a->audio_on=0;a->audio_dead=0;a->audio_clock=0;}
    a->audio_played=0;a->audio_played_at=a->now;a->audio_full_at=0;a->audio_base_ms=0;a->audio_try_ms=0;
    a->starve_mark=0;a->starve_ms=a->now;
    if(!a->audio_on)a->audio_tried=0; /* a sink that could not start (or was declared dead) gets fresh tries with the new segment */
    if(a->fps_cap<JF_FPS_MIN||a->fps_cap>JF_FPS_MAX)a->fps_cap=JF_FPS_START;
    jf_stream_session_id(a); /* fresh playSessionId: a reopen gets its own transcode, never a replay of the first segment */
    /* 1.6.0: the same path as before for Jellyfin (source_id is only ever set by PlaybackInfo, which Jellyfin mode
     * never asks). TV: PlaybackInfo's media source and live stream. ChromaTube: its media source. Neither ever
     * carries startTimeTicks (the proven requests have none; a position is not proven for remote streams). */
    int n=snprintf(path,sizeof(path),"/Videos/%s/stream.avi?mediaSourceId=%s&playSessionId=%s&deviceId=%s"
             "&videoCodec=mjpeg&%s&maxWidth=%d&maxHeight=%u&maxFramerate=%u&videoBitRate=%u",
             a->detail.id,a->source_id[0]?a->source_id:a->detail.id,a->stream_session,a->cfg.device,
             a->cfg.audio?JF_STREAM_AUDIO_IMA:JF_STREAM_AUDIO_MP3,
             JF_W,JF_VIDEO_H,(unsigned)a->fps_cap,(unsigned)a->bitrate);
    if(n>0&&n<(int)sizeof(path)&&a->cfg.mode==JF_MODE_JELLYFIN)
        n+=snprintf(path+n,sizeof(path)-(size_t)n,"&startTimeTicks=%llu",(unsigned long long)start_s*10000000ull);
    if(n>0&&n<(int)sizeof(path)&&a->cfg.mode!=JF_MODE_JELLYFIN&&a->live_id[0])
        n+=snprintf(path+n,sizeof(path)-(size_t)n,"&liveStreamId=%s",a->live_id);
    /* Chosen English audio track, and English subtitles burned in by the server (the console cannot
     * render subtitle files, so SubtitleMethod=Encode draws them into the MJPEG). -1: the server default. */
    if(n>0&&n<(int)sizeof(path)&&a->audio_index>=0)
        n+=snprintf(path+n,sizeof(path)-(size_t)n,"&audioStreamIndex=%d",(int)a->audio_index);
    a->stream_subs=(uint8_t)(a->sub_index>=0&&a->subs==1&&!a->subs_failed);
    if(n>0&&n<(int)sizeof(path)&&a->stream_subs)
        snprintf(path+n,sizeof(path)-(size_t)n,"&subtitleStreamIndex=%d&subtitleMethod=Encode",(int)a->sub_index);
    a->open_ms=a->now;a->open_shown=a->shown;a->tracks_seq++; /* a new diagnostics line for this stream's choice */
    a->window_ms=a->now;a->window_shown=a->window_dropped=a->window_rebased=0;a->window_waits=0;
    if(a->cfg.mode!=JF_MODE_JELLYFIN)a->tv_opened=1; /* 1.6.0: a conversion may exist on the server from here on */
    if(a->io.stream_open(a->io.ctx,path)!=0) {
        a->stream_state=0;
        if(a->stream_subs){a->subs_failed=1;a->sub_index=-1;jf_note(a,JF_MSG_SUBS_FAILED,1);return jf_stream_start(a,start_s);} /* once: without */
        return -1;
    }
    return 0;
}
/* Shows Loading at once; the stream starts once the request slot is free, because the
 * shared buffer then becomes the video's (no reply may still be writing into it). */
static inline void jf_play(jf_app *a,uint32_t from_s) {
    a->play_from=from_s;a->screen=JF_S_PLAYER;a->playing=0;a->paused=0;a->osd=1;a->redraw=1;
    if(a->cfg.fb)memset(a->cfg.fb,0,sizeof(uint16_t)*JF_W*JF_H);
    jf_copy(a->message,sizeof(a->message),"Loading");
    jf_do(a,JF_W_PLAY);
}
static inline void jf_play_start(jf_app *a) {
    uint32_t from_s=a->play_from;
    jf_session_id(a);a->reconnects=0;a->reconnect_at=0;
    if(a->cfg.mode!=JF_MODE_JELLYFIN){if(!a->bitrate||a->bitrate>JF_RATE_START_MODE(a))a->bitrate=JF_RATE_START_MODE(a);} /* review B1: never more */
    else if(!a->bitrate)a->bitrate=JF_RATE_START;
    else if(a->smooth_ms>=60000u&&a->bitrate<JF_RATE_MAX) {
        uint32_t next=a->bitrate*3/2>JF_RATE_MAX?JF_RATE_MAX:a->bitrate*3/2;
        if(!a->rate_cap||next<a->rate_cap)a->bitrate=next; /* never back to a rate the link could not carry */
    }
    if(!a->fps_cap||a->cfg.mode!=JF_MODE_JELLYFIN)a->fps_cap=JF_FPS_START; /* 1.6.0: TV and ChromaTube always ask for 15 */
    /* 20 s smooth with the cycle under 60% of the frame time: ask for a third more pictures next */
    else if(a->smooth_ms>=20000u&&a->cycle_ms&&a->play_frame_ms&&a->cycle_ms*10<a->play_frame_ms*6&&a->fps_cap<JF_FPS_MAX)
        a->fps_cap=a->fps_cap*4/3>JF_FPS_MAX?JF_FPS_MAX:a->fps_cap*4/3;
    a->smooth_ms=0;a->cycle_ms=0;a->adapt_at=a->now-JF_ADAPT_MS;
    if(a->cfg.fb)memset(a->cfg.fb,0,sizeof(uint16_t)*JF_W*JF_H);
    a->playing=1;a->paused=0;a->shown=a->dropped=a->rebased=0;a->pictured=0;a->reconnect_mark=0;a->audio_catchup=0;a->thinned=0;
    jf_audio_stop(a);a->audio_blocks=0;a->audio_fallbacks=0; /* sound starts with the stream's first block */
    a->screen=JF_S_PLAYER;a->redraw=1;a->osd=1;a->osd_until=a->now+3000;
    jf_copy(a->message,sizeof(a->message),"Loading");
    a->note_until=0;a->note[0]=0;a->switching=0;
    /* The parse's choice stands; chosen again only if the Audio (or Subtitles) row changed since that parse. */
    int missing=0;
    if(a->tracks_ok&&(a->chosen_lang!=a->audio_lang||a->chosen_subs!=a->subs)){missing=jf_choose_tracks(a);a->chosen_lang=a->audio_lang;a->chosen_subs=a->subs;}
    else if(a->tracks_ok&&a->audio_lang!=1&&a->audio_count&&a->audio_index<0)missing=1;
    if(jf_stream_start(a,from_s)!=0){a->playing=0;jf_fail(a,"Could not start the video");return;}
    jf_tracks_result(a,missing,0);
    jf_report(a,JF_R_PLAYING);
}
static inline void jf_server_close(jf_app *a,int encoding);
static inline void jf_stop(jf_app *a,int report) {
    /* 1.6.0, TV: the player's own stops (ended, lost, unreadable) are a lost channel; B goes through jf_tv_to_list */
    if(a->cfg.mode==JF_MODE_TV){jf_tv_nosignal(a,"No signal");return;}
    a->playinfo_for_play=0; /* 1.6.0: a PlaybackInfo still out is no longer wanted (its live stream, if any, is closed on reply) */
    if(a->cfg.mode==JF_MODE_TUBE&&a->live_id[0]&&a->tv_close==JF_CLOSE_NONE)jf_server_close(a,0);
    a->tv_opened=0;
    if(a->want==JF_W_PLAY||a->want==JF_W_TRACKS)a->want=JF_W_NONE; /* B while Loading: never starts */
    a->switching=0;a->menu_paused=0;a->note_until=0;
    if(a->stream_state){a->io.stream_close(a->io.ctx);a->stream_state=0;}
    jf_audio_stop(a);
    if(a->playing&&report) {
        a->detail.resume_s=a->ended?0:jf_position_s(a);
        if(a->request==JF_R_NONE)jf_report(a,JF_R_STOPPED);
    }
    a->playing=0;a->paused=0;a->screen=JF_S_DETAILS;a->redraw=1;
}
/* Reopens the stream where it is for a quality or frame-rate change (the picture stays up,
 * the strip says why). Rate-limited by JF_ADAPT_MS: every reopen restarts the transcoder. */
static inline void jf_adapt(jf_app *a,const char *why) {
    uint32_t at=jf_position_s(a);
    a->adapt_at=a->now;a->smooth_ms=0;
    jf_copy(a->message,sizeof(a->message),why);a->redraw=1;
    if(jf_stream_start(a,at)!=0){{int unseen=!a->pictured;jf_stop(a,1);jf_fail_video(a,"Lost the connection to the server",unseen);};}
}
/* Paces pictures to the title's own cadence. With sound playing, the sound is the clock:
 * frame n is due when n frame-times of samples have played (both count from the stream
 * start, which is the seek point); pictures wait for it, late ones are shown or skipped by
 * the same rules as below, and the clock never jumps. Without sound (today's FPGA, a silent
 * title, or a sink that stopped), frame n is due n frame-times after the picture the clock
 * was anchored to; there is nothing to keep in step with, so a late picture is only skipped
 * when skipping helps (the decoder is behind and the next picture is already here), at most
 * JF_DROPS_MAX in a row; a picture that is late because its bytes arrived late re-anchors
 * the clock and is shown. Playback therefore runs on at whatever rate the link and the
 * decoder allow instead of skipping everything once behind (the 1.2.0 failure: 35 shown,
 * 485 skipped, one picture per reopen). */
/* 1.6.0, TV and ChromaTube: a stream that comes faster than the cap that was asked for (a live channel: 24 pictures
 * a second measured although 15 were asked) cannot be slowed by asking again, and the console cannot decode it all:
 * with sound, the sound reaches the ring only as fast as the pictures in front of it are dealt with, so it starves and
 * everything plays slow with gaps (PC model: 19 of 24 pictures a second, 20% silence, at 54 ms a picture). So only
 * one picture in `thin` is decoded: at least so many left out that what is shown is no more than the cap asked for,
 * and so many that the measured cost of a picture fits in 80% of the time between two shown ones; at most one in 8
 * (a 60-a-second channel: one in 5). Counted by pictures delivered in this stream (review M2), never by the
 * container's frame number. 1: show them all (the stream keeps to the cap; always in Jellyfin mode). */
static inline unsigned jf_thin(const jf_app *a,uint32_t frame_us) {
    if(!frame_us||a->cfg.mode==JF_MODE_JELLYFIN||!a->fps_cap)return 1u;
    /* the rate pictures really arrive at: a stream that leaves frame numbers empty delivers fewer than its header says */
    if(a->thin_seen>=8u&&a->avi.frame_index>a->thin_seen)frame_us=(uint32_t)((uint64_t)frame_us*a->avi.frame_index/a->thin_seen);
    uint32_t fps100=100000000u/frame_us;
    if(fps100<=(a->fps_cap+2u)*100u)return 1u;
    unsigned thin=(unsigned)((fps100+(a->fps_cap+1u)*100u-1u)/((a->fps_cap+1u)*100u));
    if(a->cycle_ms) {
        unsigned need=(unsigned)(((uint64_t)a->cycle_ms*10000u+(uint64_t)frame_us*8u-1u)/((uint64_t)frame_us*8u));
        if(need>thin)thin=need;
    }
    return thin<1u?1u:thin>8u?8u:thin;
}
static inline void jf_player_tick(jf_app *a) {
    if(!a->playing||a->paused||a->switching){a->measuring=0;return;} /* switching: no stream until the track list is in */
    if(a->cfg.mode==JF_MODE_TV&&a->link_down){jf_tv_nosignal(a,"Wi-Fi dropped");return;} /* 1.6.0: a live channel never reconnects */
    if(a->cfg.mode==JF_MODE_TUBE&&a->link_down){jf_stop(a,1);jf_fail(a,"Wi-Fi dropped: the video stopped");return;} /* nor a remote video (review H3) */
    /* Subtitles asked for and no picture yet after JF_SUBS_WAIT_MS: the server cannot burn them in here. Never
     * break playback for them: reopen at the same place without, and say so in yellow (1.3.2). */
    if(a->stream_subs&&a->stream_state&&a->shown==a->open_shown&&!a->reconnect_at&&(uint32_t)(a->now-a->open_ms)>=JF_SUBS_WAIT_MS) {
        a->subs_failed=1;a->sub_index=-1;
        if(jf_stream_start(a,a->start_s)!=0){{int unseen=!a->pictured;jf_stop(a,1);jf_fail_video(a,"Lost the connection to the server",unseen);};return;}
        jf_note(a,JF_MSG_SUBS_FAILED,1);return;
    }
    jf_audio_poll(a);
    if(a->measuring) { /* the loop after a shown picture: decode + overlay send + loop period */
        uint32_t c=a->now-a->shown_at;a->measuring=0;
        /* One stalled loop (a Wi-Fi task hogging the CPU, a long request) must not read as a slow
         * decoder: each measurement may at most double the running figure (a decoder that really is
         * slow still gets there within a few pictures). */
        uint32_t cap=a->cycle_ms?a->cycle_ms*2:a->play_frame_ms?a->play_frame_ms*4:2000u;
        if(a->play_frame_ms&&cap<a->play_frame_ms*2)cap=a->play_frame_ms*2;
        if(c>cap)c=cap;
        a->cycle_ms=a->cycle_ms?(a->cycle_ms*7+c+4)/8:c;
    }
    /* The link went down under an open stream: say so at once and reopen when it is back, instead of
     * waiting for the socket to time out and burning reconnect tries against no network. */
    if(a->link_down&&a->stream_state&&!a->reconnect_at) {
        a->reconnect_pos=jf_position_s(a);a->io.stream_close(a->io.ctx);a->stream_state=0;
        a->reconnect_at=a->now+500u;a->reconnect_mark=a->shown;a->redraw=1;
    }
    if(a->reconnect_at) {
        if(a->link_down) { /* no link: wait for it (the shell rejoins), without spending a try; bounded */
            if(!a->link_lost_ms)a->link_lost_ms=a->now?a->now:1;
            if((uint32_t)(a->now-a->link_lost_ms)>=JF_LINK_WAIT_MS) {
                jf_stop(a,1);jf_fail(a,"Wi-Fi was lost for too long");jf_copy(a->detail_line,sizeof(a->detail_line),"Back on Wi-Fi? Press A");return;
            }
            a->reconnects=0;a->reconnect_at=a->now+500u;
            if(strcmp(a->message,JF_MSG_LINK_DOWN)){jf_copy(a->message,sizeof(a->message),JF_MSG_LINK_DOWN);a->redraw=1;}
            return;
        }
        if((int32_t)(a->now-a->reconnect_at)<0)return;
        a->reconnect_at=0;a->link_lost_ms=0;
        if(jf_stream_start(a,a->reconnect_pos)!=0){{int unseen=!a->pictured;jf_stop(a,1);jf_fail_video(a,"Lost the connection to the server",unseen);};}
        return;
    }
    for(int guard=0;guard<64;guard++) {
        if(!a->frame_ready) {
            if(a->chunk_used==a->chunk_size) {
                int n=a->io.stream_read(a->io.ctx,a->chunk,sizeof(a->chunk));
                if(n<0&&a->stream_subs&&a->shown==a->open_shown) { /* ended or failed before any picture with subtitles: without them */
                    a->subs_failed=1;a->sub_index=-1;
                    if(jf_stream_start(a,a->start_s)!=0){{int unseen=!a->pictured;jf_stop(a,1);jf_fail_video(a,"Lost the connection to the server",unseen);};return;}
                    jf_note(a,JF_MSG_SUBS_FAILED,1);return;
                }
                if(n==-1){a->ended=1;break;}
                if(n<0) { /* lost: reopen where we are, with back-off, at most 5 times in a row */
                    /* 1.6.0, TV: no reopen (the same channel is never opened twice within JF_TV_REOPEN_MS): No signal.
                     * ChromaTube: no reopen at a position (review H3): the video stops and says so. */
                    if(++a->reconnects>5||a->cfg.mode!=JF_MODE_JELLYFIN){{int unseen=!a->pictured;jf_stop(a,1);jf_fail_video(a,"Lost the connection to the server",unseen);};return;}
                    a->reconnect_pos=jf_position_s(a);a->io.stream_close(a->io.ctx);a->stream_state=0;
                    a->reconnect_at=a->now+(500u<<(a->reconnects-1));a->reconnect_mark=a->shown;
                    jf_copy(a->message,sizeof(a->message),a->link_down?JF_MSG_LINK_DOWN:JF_MSG_SERVER_LOST);a->redraw=1;return;
                }
                if(n==0){a->frame_waited=1;a->window_waits++;break;} /* the network has not delivered this picture yet */
                a->chunk_size=(size_t)n;a->chunk_used=0;
            }
            size_t used;int r=jf_avi_feed(&a->avi,a->chunk+a->chunk_used,a->chunk_size-a->chunk_used,&used);
            a->chunk_used+=used;
            if(r<0){int unseen=!a->pictured;jf_stop(a,1);jf_fail_video(a,"The video stream could not be read",unseen);return;}
            if(r==JF_AVI_FRAME){a->frame_ready=1;a->frame_new=1;a->stall_ms=a->now;}
            if(r==JF_AVI_AUDIO_FULL)break; /* the sound ring is full: the next picture waits behind this sound */
            continue;
        }
        jf_audio_ahead(a); /* sound in front of the next picture goes to the ring now */
        uint32_t frame_us=jf_avi_frame_us(&a->avi);if(!frame_us)frame_us=41708;
        uint32_t frame_ms=frame_us/1000u;a->play_frame_ms=frame_ms;
        a->at_frame=a->avi.frame_index;
        if(a->frame_new) { /* 1.6.0: each delivered picture is counted once, when it is first looked at */
            unsigned thin=jf_thin(a,frame_us);
            a->frame_new=0;a->frame_check=1;a->thin_on=(uint8_t)(thin>1u); /* the guards below: only for a stream faster than asked */
            if(thin>1u&&(a->thin_seen++%thin)){a->thinned++;a->frame_ready=0;continue;}
        }
        uint64_t due;uint32_t elapsed;
        if(a->audio_clock) { /* both count from the stream start */
            a->anchored=1;a->base_frame=0;a->clock_ms=a->now-jf_audio_ms(a);
            due=(uint64_t)a->avi.frame_index*frame_us/1000u;elapsed=jf_audio_ms(a);
        } else {
            if(!a->anchored) { /* the clock only: base_frame stays, so the position never jumps back */
                a->anchored=1;
                a->clock_ms=a->now-(uint32_t)((uint64_t)(a->avi.frame_index-(uint32_t)a->base_frame)*frame_us/1000u);
            }
            due=(uint64_t)(a->avi.frame_index-(uint32_t)a->base_frame)*frame_us/1000u;
            elapsed=a->now-a->clock_ms;
        }
        /* 1.6.0, TV and ChromaTube, sound first. Decoding a picture keeps the loop away from the stream for a whole
         * cycle, and the stream carries the sound in blocks of an eighth of a second between the pictures: the next
         * block lies behind the pictures of the current one. So a picture is only kept when the sound already
         * queued reaches a cycle (and 20 ms) past the picture's own time; otherwise it is left out at once, undecoded,
         * and the stream read on to the next block; at most four in a row. Looked at once per picture. (PC model,
         * 30 pictures a second at the console's figures: 0.6 sound gaps a second without this.) Never in Jellyfin mode. */
        if(a->frame_check) {
            a->frame_check=0;
            if(a->thin_on&&a->audio_clock&&a->audio_rate&&a->cycle_ms&&a->drops_in_row<4u) {
                uint32_t fed=a->audio_fed*2u-a->audio_fed_blocks*7u,queued=fed>a->audio_played?(uint32_t)((uint64_t)(fed-a->audio_played)*1000u/a->audio_rate):0;
                uint32_t wait=due>elapsed?(uint32_t)(due-elapsed):0;
                if(queued<wait+a->cycle_ms+20u){a->thinned++;a->drops_in_row++;a->frame_ready=0;continue;}
            }
        }
        if(elapsed+1<due)break; /* not yet */
        if(elapsed>due+frame_ms*2) { /* late */
            if(a->audio_clock) {
                /* Sound rules the clock (it never jumps). A late picture is behind the sound, never the
                 * decoder's doing while sound plays: skip it to catch up (counted as catch-up, which is
                 * neither "dropped" nor a network re-anchor, so it can lower neither the frame rate nor
                 * the quality), and after JF_DROPS_MAX in a row show one anyway so the picture keeps moving. */
                if(a->drops_in_row<JF_DROPS_MAX){a->rebased++;a->audio_catchup++;a->drops_in_row++;a->frame_ready=0;continue;}
            } else {
                /* No sound (stock FPGA, a silent title, or a sink that stopped): the decoder's fault only
                 * if its measured cycle nearly fills a frame time (skipping then gains time); otherwise the
                 * bytes came late (a burst after a stall) and the picture is the network's: shown and the
                 * frame clock re-anchored. */
                int dec_busy=a->cycle_ms&&a->cycle_ms*10>frame_ms*JF_DEC_BUSY_TENTHS;
                if(dec_busy&&!a->frame_waited&&a->drops_in_row<JF_DROPS_MAX)
                    {a->dropped++;a->window_dropped++;a->drops_in_row++;a->frame_ready=0;continue;}
                a->clock_ms=a->now-(uint32_t)due;
                if(a->frame_waited||!dec_busy){a->rebased++;a->window_rebased++;}
            }
        }
        a->drops_in_row=0;a->frame_waited=0;
        if(a->cfg.fb&&a->io.decode(a->io.ctx,a->avi.frame,a->avi.frame_size,a->cfg.fb,JF_W,JF_W,(int)JF_VIDEO_H)==0) {
            a->shown++;a->window_shown++;a->redraw=1;a->message[0]=0;a->pictured=1;
            a->shown_at=a->now;a->measuring=1;
            if(a->shown-a->reconnect_mark>=48)a->reconnects=0;
        } else a->dropped++;
        a->frame_ready=0;
        break;
    }
    if(a->ended&&!a->frame_ready&&a->chunk_used==a->chunk_size){jf_stop(a,1);return;}
    if((uint32_t)(a->now-a->stall_ms)>8000u&&!a->ended&&!a->reconnect_at&&strcmp(a->message,JF_MSG_WAITING))
        {jf_copy(a->message,sizeof(a->message),JF_MSG_WAITING);a->redraw=1;}
    /* Adapt every 5 s, by cause. Decoder behind (pictures skipped because the next was already
     * here, or the measured cycle near the frame time): ask for fewer pictures per second.
     * Network behind (pictures re-anchored because their bytes came late, or 2.5 s with none):
     * halve the quality. One reopen per JF_ADAPT_MS at most; at the floor it plays on as is. */
    if((uint32_t)(a->now-a->window_ms)>=5000u&&a->anchored&&!a->ended) {
        uint32_t total=a->window_shown+a->window_dropped;
        int net_slow=(total&&a->window_rebased*4>total)||(uint32_t)(a->now-a->stall_ms)>2500u;
        uint32_t deficit=jf_audio_deficit(a); /* sound starved: the link cannot carry the stream (see JF_AUDIO_STARVE_PERMILLE) */
        /* While sound is the clock the measured cycle includes waiting for the audio, so it must not read
         * as a slow decoder (the owner's "fps dropped to 6" with sound): catch-up skips are not counted in
         * window_dropped, and the cycle test is used only when the pictures run on their own clock. */
        int dec_slow=(total&&a->window_dropped*4>total)||(!a->audio_clock&&a->cycle_ms&&a->cycle_ms*10>a->play_frame_ms*9);
        /* 1.6.0: only in Jellyfin mode. ChromaTube never reopens (review H3: a reopen needs a position, which is
         * not proven for remote videos; at 500 kbit/s none is expected). TV, review B1: once in a session, when
         * the link starves the sound or the pictures come late, the channel is closed and tuned again at
         * JF_RATE_LOW_TV, under the reopen rule (about 10 s of "Adjusting quality..."); never again after that. */
        int may=(uint32_t)(a->now-a->adapt_at)>=JF_ADAPT_MS&&a->cfg.mode==JF_MODE_JELLYFIN;
        if(a->cfg.mode==JF_MODE_TV) {
            /* N3: a starved window is one in which pictures still came (not a stream that ended or whose pictures
             * stopped: that is "No signal") and either a quarter of them came late over the network or the sound
             * lost JF_AUDIO_STARVE_PERMILLE with the network empty-handed. Two in a row, for this channel only. */
            int coming=a->window_shown>0&&!a->ended&&(uint32_t)(a->now-a->stall_ms)<2500u;
            int starving=coming&&((total&&a->window_rebased*4>total)||(deficit>=JF_AUDIO_STARVE_PERMILLE&&a->window_waits>=JF_AUDIO_STARVE_WAITS));
            a->tv_starved=(uint8_t)(starving?a->tv_starved+1u:0u);
            if(a->tv_starved>=2u&&!a->tv_stepped&&a->bitrate>JF_RATE_LOW_TV) {
                a->tv_stepped=1;a->tv_starved=0;a->bitrate=JF_RATE_LOW_TV;a->softened++;
                a->tv_step_tune=1;jf_tv_tune(a);a->tv_step_tune=0;
                jf_copy(a->message,sizeof(a->message),"Adjusting quality...");return;
            }
        }
        if(may&&dec_slow&&a->fps_cap>JF_FPS_MIN) {
            uint32_t fit=a->cycle_ms?850u/a->cycle_ms:a->fps_cap*2/3; /* pictures the cycle can take, 15% spare */
            if(fit>=a->fps_cap)fit=a->fps_cap*2/3;
            if(fit<JF_FPS_MIN)fit=JF_FPS_MIN;
            a->fps_cap=fit;a->slowed++;jf_adapt(a,"Adjusting frame rate");return;
        }
        int starved=!dec_slow&&deficit>=JF_AUDIO_STARVE_PERMILLE&&a->window_waits>=JF_AUDIO_STARVE_WAITS;
        if(may&&(net_slow||starved)&&a->bitrate>JF_RATE_MIN) {
            if(starved){a->starved++;a->rate_cap=a->bitrate;}
            a->bitrate=a->bitrate/2<JF_RATE_MIN?JF_RATE_MIN:a->bitrate/2;a->softened++;
            jf_adapt(a,"Adjusting quality");return;
        }
        if(!net_slow&&!dec_slow&&!starved)a->smooth_ms+=5000u;
        a->window_ms=a->now;a->window_shown=a->window_dropped=a->window_rebased=0;a->window_waits=0;
        if(a->audio_clock){a->starve_mark=a->audio_played;a->starve_ms=a->now;} /* each window judges its own sound */
    }
    if((uint32_t)(a->now-a->last_progress_ms)>=JF_PROGRESS_MS)jf_report(a,JF_R_PROGRESS);
}
static inline void jf_seek(jf_app *a,int delta_s) {
    int64_t at=(int64_t)jf_position_s(a)+delta_s;if(at<0)at=0;
    if(a->detail.runtime_s&&at>=(int64_t)a->detail.runtime_s)at=a->detail.runtime_s>5?a->detail.runtime_s-5:0;
    a->paused=0;a->pictured=0;jf_copy(a->message,sizeof(a->message),"Loading");a->osd=1;a->osd_until=a->now+3000;a->redraw=1;
    if(jf_stream_start(a,(uint32_t)at)!=0){jf_stop(a,1);jf_fail(a,"Could not seek");}
}
/* 1.3.2: the Audio (or Subtitles) row changed while a video plays. The stream reopens where it is with the new
 * track and a fresh playSessionId, like a seek; the sign says what is being changed, the note then the result.
 * Without a track list (its first fetch failed) the stream closes, the list is fetched, then it reopens. */
static inline void jf_switch_tracks(jf_app *a) {
    a->switch_pos=jf_position_s(a);
    a->paused=0;a->menu_paused=0;a->pictured=0;a->osd=1;a->osd_until=a->now+3000;a->redraw=1;a->note_until=0;
    if(a->audio_on&&!a->audio_dead&&a->io.audio_pause)a->io.audio_pause(a->io.ctx,0);
    snprintf(a->message,sizeof(a->message),"%s...",a->menu_lang!=a->audio_lang?
             (a->audio_lang==0?"Audio: English":a->audio_lang==2?"Audio: Japanese":"Audio: Original"):
             a->subs?"Subtitles: English":"Subtitles: Off");
    if(a->tracks_ok||(!a->cfg.audio&&!a->subs)){jf_switch_finish(a);return;}
    if(a->stream_state){a->io.stream_close(a->io.ctx);a->stream_state=0;}
    a->switching=1;a->streams_done=0;
    jf_do(a,JF_W_TRACKS); /* now, or as soon as an outstanding report is answered */
}
static inline void jf_switch_finish(jf_app *a) {
    int missing=jf_choose_tracks(a);a->chosen_lang=a->audio_lang;a->chosen_subs=a->subs;
    a->switching=0;
    if(jf_stream_start(a,a->switch_pos)!=0){{int unseen=!a->pictured;jf_stop(a,1);jf_fail_video(a,"Lost the connection to the server",unseen);};return;}
    jf_tracks_result(a,missing,1);
}

/* ---------- 1.6.0: PlaybackInfo, the server closes, the TV app ---------- */
/* PlaybackInfo's reply, {"MediaSources":[{"Id":"..","LiveStreamId":"..",...}],...}, read as it arrives (review H2):
 * a small JSON walker that keeps nothing but MediaSources[0].Id and MediaSources[0].LiveStreamId, so the reply may be
 * of any size (a title with twenty tracks is 20 KB) and a reply that breaks off after the ids still names the live
 * stream the server opened. The two values are collected raw in the app's buffer, which is free for this (nothing
 * else is asked or played while a PlaybackInfo is out), at 0 and at JF_PI_ID_RAW. */
static inline void jf_pi_char(jf_app *a,unsigned c) { /* one decoded character of the string being read */
    if(a->pi.dest==1){if(a->pi.id_len+1u<JF_PI_ID_RAW)a->buf[a->pi.id_len++]=(uint8_t)c;else a->pi.over|=1u;}
    else if(a->pi.dest==2){if(a->pi.live_len+1u<JF_PI_LIVE_RAW)a->buf[JF_PI_ID_RAW+a->pi.live_len++]=(uint8_t)c;else a->pi.over|=2u;}
    else if(a->pi.klen+1u<sizeof(a->pi.key))a->pi.key[a->pi.klen++]=(char)c;
    else a->pi.kover=1;
}
static inline void jf_pi_code(jf_app *a,unsigned c) { /* a \uXXXX code point as UTF-8 (surrogate halves are left out) */
    if(c<0x80u)jf_pi_char(a,c);
    else if(c<0x800u){jf_pi_char(a,0xc0u|(c>>6));jf_pi_char(a,0x80u|(c&63u));}
    else if(c<0xd800u||c>0xdfffu){jf_pi_char(a,0xe0u|(c>>12));jf_pi_char(a,0x80u|((c>>6)&63u));jf_pi_char(a,0x80u|(c&63u));}
}
static inline void jf_pi_feed(jf_app *a,const uint8_t *data,size_t size) {
    for(size_t i=0;i<size;i++) {
        unsigned c=data[i];
        if(a->pi.in_str) {
            if(a->pi.uni) { /* the four hex digits of \uXXXX */
                unsigned v=c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10u:c>='A'&&c<='F'?c-'A'+10u:0u;
                a->pi.code=(uint16_t)((a->pi.code<<4)|v);
                if(--a->pi.uni==0)jf_pi_code(a,a->pi.code);
            } else if(a->pi.esc) {
                a->pi.esc=0;
                if(c=='u'){a->pi.uni=4;a->pi.code=0;}
                else jf_pi_char(a,c=='n'||c=='r'||c=='t'||c=='b'||c=='f'?' ':c);
            } else if(c=='\\')a->pi.esc=1;
            else if(c=='"') {
                a->pi.in_str=0;
                if(a->pi.dest==1)a->pi.got_id=1;
                else if(a->pi.dest==2)a->pi.got_live=1;
                else a->pi.after_str=1; /* a key, if a colon follows */
                a->pi.dest=0;
            } else jf_pi_char(a,c);
            continue;
        }
        if(c==' '||c=='\t'||c=='\r'||c=='\n')continue;
        if(a->pi.after_str) {
            a->pi.after_str=0;
            if(c==':') {
                a->pi.key[a->pi.klen]=0;
                if(!a->pi.kover&&a->pi.depth==1&&!strcmp(a->pi.key,"MediaSources"))a->pi.expect=1;
                else if(!a->pi.kover&&a->pi.depth==3&&a->pi.in_sources&&a->pi.elem==1) {
                    if(!strcmp(a->pi.key,"Id")&&!a->pi.got_id)a->pi.capture=1;
                    else if(!strcmp(a->pi.key,"LiveStreamId")&&!a->pi.got_live)a->pi.capture=2;
                }
                continue;
            }
        }
        if(c=='"') {
            a->pi.in_str=1;a->pi.esc=0;a->pi.uni=0;a->pi.klen=0;a->pi.kover=0;
            a->pi.dest=a->pi.capture;a->pi.capture=0;
            if(a->pi.dest==1)a->pi.id_len=0;else if(a->pi.dest==2)a->pi.live_len=0;
            continue;
        }
        a->pi.capture=0; /* the value after the key was not a string (null, a number) */
        if(c=='{'||c=='[') {
            if(c=='['&&a->pi.expect&&a->pi.depth==1)a->pi.in_sources=1;
            a->pi.expect=0;
            if(a->pi.depth<255u)a->pi.depth++;
            if(c=='{'&&a->pi.in_sources&&a->pi.depth==3&&a->pi.elem<255u)a->pi.elem++;
        } else if(c=='}'||c==']') {
            if(a->pi.depth)a->pi.depth--;
            if(a->pi.in_sources&&a->pi.depth<=1)a->pi.in_sources=0;
        } else a->pi.expect=0;
    }
}
/* What jf_pi_feed found, into source_id and live_id (URL-encoded). Bit 0: a media source id; bit 1: a live stream
 * id that fits (held in live_id: it must be closed); bit 2: a live stream id too long to play with: its close is
 * sent from here at once (the request slot is free in the reply handler), or it is cut off, which only a server
 * sending ids of several hundred characters could cause. */
static inline int jf_pi_take(jf_app *a) {
    int got=0;
    a->source_id[0]=0;a->live_id[0]=0;
    if(a->pi.got_id&&a->pi.id_len&&!(a->pi.over&1u)) {
        a->buf[a->pi.id_len]=0;
        if(jf_urlenc(a->source_id,sizeof(a->source_id),(const char *)a->buf))got|=1;
    }
    if(a->pi.got_live&&a->pi.live_len) {
        a->buf[JF_PI_ID_RAW+a->pi.live_len]=0;
        if(!(a->pi.over&2u)&&jf_urlenc(a->live_id,sizeof(a->live_id),(const char *)a->buf+JF_PI_ID_RAW))got|=2;
        else {
            char path[400];int n=snprintf(path,sizeof(path),"/LiveStreams/Close?liveStreamId=");
            got|=4;
            if(!(a->pi.over&2u)&&jf_urlenc(path+n,sizeof(path)-(size_t)n,(const char *)a->buf+JF_PI_ID_RAW)&&
               a->request==JF_R_NONE&&jf_post(a,JF_R_LIVE_CLOSE,path,NULL)==0)a->tv_close=JF_CLOSE_LIVE_OUT;
        }
    }
    memset(&a->pi,0,sizeof(a->pi));
    return got;
}
/* The closes owed for the title in a->detail after its stream was closed here: DELETE /Videos/ActiveEncodings for the
 * stream's playSessionId (`encoding`: TV, when a stream was asked for), then POST /LiveStreams/Close when PlaybackInfo
 * opened a live stream. Sent one at a time by jf_close_pump; nothing is opened until both are answered. */
static inline void jf_server_close(jf_app *a,int encoding) {
    if(a->cfg.mode==JF_MODE_TV) {
        unsigned k=a->tv_closed_next&1u;
        jf_copy(a->tv_closed_id[k],33,a->detail.id);a->tv_closed_ms[k]=a->now;a->tv_closed_next=(uint8_t)(k^1u);
    }
    a->tv_close=(uint8_t)(encoding?JF_CLOSE_ENCODING:a->live_id[0]?JF_CLOSE_LIVE:JF_CLOSE_NONE);
}
static inline void jf_close_pump(jf_app *a) {
    char path[JF_LIVE_ID+128];
    if(a->request!=JF_R_NONE)return;
    if(a->tv_close==JF_CLOSE_ENCODING) {
        snprintf(path,sizeof(path),"/Videos/ActiveEncodings?deviceId=%s&playSessionId=%s",a->cfg.device,a->stream_session);
        a->request=JF_R_ENCODING_STOP;a->reply_size=0;a->request_ms=a->now;
        if(a->io.request(a->io.ctx,"DELETE",path,NULL)!=0){a->request=JF_R_NONE;return;} /* again next tick */
        a->tv_close=JF_CLOSE_ENCODING_OUT;
    } else if(a->tv_close==JF_CLOSE_LIVE) {
        if(!a->live_id[0]){a->tv_close=JF_CLOSE_NONE;return;}
        snprintf(path,sizeof(path),"/LiveStreams/Close?liveStreamId=%s",a->live_id);
        if(jf_post(a,JF_R_LIVE_CLOSE,path,NULL)!=0)return;
        a->tv_close=JF_CLOSE_LIVE_OUT;
    }
}
/* The list: on its first page the built-in channel (when there is one) is the first row, the server's follow. */
static inline unsigned jf_tv_extra(const jf_app *a){return a->cfg.wx_row&&a->folder_start==0?1u:0u;}
/* With the list not readable (tv_list_err) the rows are the weather row and "Try again". */
static inline unsigned jf_tv_rows(const jf_app *a){return jf_tv_extra(a)+(a->tv_list_err?1u:a->folder.count);}
static inline int jf_tv_loading(const jf_app *a){return a->request==JF_R_CHANNELS||a->want==JF_W_CHANNELS||a->busy==JF_R_CHANNELS;}
static inline void jf_load_channels(jf_app *a){a->folder.count=0;a->redraw=1;jf_do(a,JF_W_CHANNELS);}
/* One page of GET /LiveTv/Channels into the folder page: id, name, and the channel number in `sub`. */
static inline int jf_parse_channels(jf_app *a,uint32_t *total) {
    pj_json j;
    if(!a->tokens||!pj_json_parse(&j,a->buf,a->reply_size,a->tokens,a->token_cap)||j.tokens[0].type!=PJ_JSON_OBJECT)return -1;
    int list=pj_json_find(&j,0,"Items");
    uint64_t t;if(pj_json_u64(&j,pj_json_find(&j,0,"TotalRecordCount"),&t))*total=(uint32_t)t;
    if(list<0||j.tokens[list].type!=PJ_JSON_ARRAY)return -1;
    unsigned count=0;
    for(unsigned i=(unsigned)list+1;i<j.tokens[list].next&&count<JF_TV_PAGE&&count<JF_PAGE_ITEMS;i=j.tokens[i].next) {
        if(j.tokens[i].type!=PJ_JSON_OBJECT)continue;
        jf_item *it=&a->folder.items[count];
        if(!jf_parse_item(&j,(int)i,it,JF_SHAPE_THUMB))continue;
        jf_ascii(it->name);
        it->sub[0]=0;it->card_kind=it->hero_kind=JF_IMG_NONE;
        if(!pj_json_text(&j,pj_json_find(&j,(int)i,"ChannelNumber"),it->sub,8))pj_json_text(&j,pj_json_find(&j,(int)i,"Number"),it->sub,8);
        count++;
    }
    return (int)count;
}
/* The focused row one step on (dir 1) or back (-1), across pages, wrapping at both ends of the whole list. */
static inline void jf_tv_move(jf_app *a,int dir) {
    unsigned rows=jf_tv_rows(a);
    if(jf_tv_loading(a)) { /* a page is on its way: only among the rows that are there (the weather row stays reachable) */
        if(rows&&a->folder.focus>=rows)a->folder.focus=(uint8_t)(rows-1u);
        else if(dir>0&&a->folder.focus+1u<rows)a->folder.focus++;
        else if(dir<0&&a->folder.focus)a->folder.focus--;
        a->redraw=1;return;
    }
    uint32_t last=a->folder_total?((a->folder_total-1u)/JF_TV_PAGE)*JF_TV_PAGE:0;
    if(dir>0) {
        if(a->folder.focus+1u<rows)a->folder.focus++;
        else if(a->folder_start+a->folder.count<a->folder_total){a->folder_start+=JF_TV_PAGE;a->folder.focus=0;jf_load_channels(a);}
        else if(a->folder_start){a->folder_start=0;a->folder.focus=0;jf_load_channels(a);}
        else a->folder.focus=0;
    } else {
        if(a->folder.focus)a->folder.focus--;
        else if(a->folder_start){a->folder_start=a->folder_start>JF_TV_PAGE?a->folder_start-JF_TV_PAGE:0;a->folder.focus=255;jf_load_channels(a);}
        else if(last){a->folder_start=last;a->folder.focus=255;jf_load_channels(a);}
        else a->folder.focus=(uint8_t)(rows?rows-1u:0u);
    }
    a->redraw=1;
}
/* Whatever is on goes off: the stream, the sound, and the server closes are queued. */
static inline void jf_tv_leave(jf_app *a) {
    a->playinfo_for_play=0;
    if(a->want==JF_W_PLAY)a->want=JF_W_NONE;
    if(a->stream_state){a->io.stream_close(a->io.ctx);a->stream_state=0;}
    jf_audio_stop(a); /* the channel's sound, or the Local Weather channel's */
    a->playing=0;a->paused=0;a->switching=0;a->reconnect_at=0;a->note_until=0;a->tv_banner_until=0;a->tv_banner_armed=0;
    if(a->tv_close==JF_CLOSE_NONE&&(a->tv_opened||a->live_id[0]))jf_server_close(a,a->tv_opened);
    a->tv_opened=0;
}
static inline void jf_tv_black(jf_app *a){if(a->cfg.fb)memset(a->cfg.fb,0,sizeof(uint16_t)*JF_W*JF_H);}
static inline void jf_tv_nosignal(jf_app *a,const char *why) {
    jf_tv_leave(a);
    a->tv_state=JF_TV_NOSIGNAL;a->tv_at=a->now+JF_TV_NOSIGNAL_MS;a->screen=JF_S_PLAYER;
    jf_tv_black(a);jf_copy(a->message,sizeof(a->message),why);a->pictured=0;a->redraw=1;
}
static inline void jf_tv_to_list(jf_app *a){jf_tv_leave(a);a->tv_state=JF_TV_IDLE;a->screen=JF_S_CHANNELS;a->redraw=1;}
/* Tunes the focused row: the built-in channel at once; a server channel once the closes owed are out and, for a
 * channel closed less than JF_TV_REOPEN_MS ago, once that time is over (jf_tv_tick). */
static inline void jf_tv_tune(jf_app *a) {
    unsigned extra=jf_tv_extra(a);
    jf_tv_leave(a);
    if(a->folder.focus<extra) { /* the built-in channel: the launcher shows it once this app has let go (JF_EXIT_WEATHER) */
        a->tv_state=JF_TV_IDLE;a->screen=JF_S_CHANNELS;a->tv_exit=2;a->tv_at=a->now;a->redraw=1;return;
    }
    if(a->folder.focus-extra>=a->folder.count){a->tv_state=JF_TV_IDLE;a->screen=JF_S_CHANNELS;a->redraw=1;return;}
    a->detail=a->folder.items[a->folder.focus-extra];
    a->tv_starved=0;
    if(!a->tv_step_tune){a->tv_stepped=0;a->bitrate=JF_RATE_START_TV;} /* N3: the step down was the last channel's */
    a->screen=JF_S_PLAYER;jf_tv_black(a);a->pictured=0;a->osd=0;a->redraw=1;a->tv_banner_armed=1;
    jf_copy(a->message,sizeof(a->message),"Tuning...");
    a->tv_state=JF_TV_WAIT;a->tv_at=a->now;
    for(unsigned k=0;k<2;k++)
        if(a->tv_closed_id[k][0]&&!strcmp(a->tv_closed_id[k],a->detail.id)&&(uint32_t)(a->now-a->tv_closed_ms[k])<JF_TV_REOPEN_MS)
            a->tv_at=a->tv_closed_ms[k]+JF_TV_REOPEN_MS;
}
/* Up/Down while watching (a channel, the Local Weather channel, No signal): what is on goes off at once, the target
 * steps, and it is tuned JF_TV_SURF_MS after the last press. Up is the next channel, Down the one before. */
static inline void jf_tv_surf(jf_app *a,int dir) {
    if(jf_tv_loading(a))return;
    jf_tv_leave(a);
    a->screen=JF_S_PLAYER;jf_tv_black(a);a->pictured=0;a->message[0]=0;
    jf_tv_move(a,dir);
    a->tv_state=JF_TV_SURF;a->tv_at=a->now+JF_TV_SURF_MS;a->redraw=1;
}
static inline void jf_tv_tick(jf_app *a) {
    jf_close_pump(a);
    if(a->tv_exit) { /* B on the list: out once the closes are answered, or after JF_TV_EXIT_MS */
        if((a->tv_close==JF_CLOSE_NONE&&a->request==JF_R_NONE)||(uint32_t)(a->now-a->tv_at)>=JF_TV_EXIT_MS)
            {a->exit_to=(uint8_t)(a->tv_exit==2?JF_EXIT_WEATHER:JF_EXIT_MENU);a->screen=JF_S_EXIT;}
        return;
    }
    switch(a->tv_state) {
    case JF_TV_SURF:
        if((int32_t)(a->now-a->tv_at)>=0&&!jf_tv_loading(a))jf_tv_tune(a);
        break;
    case JF_TV_WAIT:
        if((int32_t)(a->now-a->tv_at)>=0&&a->tv_close==JF_CLOSE_NONE&&a->request==JF_R_NONE&&!a->want)
            {a->tv_state=JF_TV_OPENING;a->tv_open_ms=a->now;jf_do(a,JF_W_PLAY);}
        break;
    case JF_TV_PLAYING:
        if(a->playing&&a->pictured&&a->tv_banner_armed){a->tv_banner_armed=0;a->tv_banner_until=a->now+JF_TV_BANNER_MS;if(!a->tv_banner_until)a->tv_banner_until=1;}
        if(a->playing&&!a->pictured&&(uint32_t)(a->now-a->tv_open_ms)>=JF_TV_FIRST_MS)jf_tv_nosignal(a,"No signal");
        else if(a->playing&&a->pictured&&(uint32_t)(a->now-a->stall_ms)>=JF_TV_STALL_MS)jf_tv_nosignal(a,"No signal");
        break;
    case JF_TV_NOSIGNAL:
        if((int32_t)(a->now-a->tv_at)>=0)jf_tv_to_list(a);
        break;
    default:break; /* idle; opening (the PlaybackInfo reply moves on, the platform bounds the wait) */
    }
    if(a->tv_banner_until&&(int32_t)(a->now-a->tv_banner_until)>=0){a->tv_banner_until=0;a->redraw=1;}
}

/* ---------- replies ---------- */
static inline void jf_after_home_step(jf_app *a);
static inline void jf_add_row(jf_app *a,const char *title,uint8_t shape) {
    if(a->row_count>=JF_ROWS)return;
    jf_row *r=&a->rows[a->row_count];memset(r,0,sizeof(*r));
    int n=jf_parse_list(a,r->items,JF_ROW_ITEMS,shape,NULL);
    if(n>0){r->count=(uint8_t)n;r->shape=shape;jf_copy(r->title,sizeof(r->title),title);a->row_count++;}
}
static inline void jf_app_reply(jf_app *a,int status) {
    uint8_t kind=a->request;a->request=JF_R_NONE;
    if(kind==JF_R_PLAYING||kind==JF_R_PROGRESS||kind==JF_R_STOPPED)return; /* best effort */
    /* 1.6.0: the closes of a channel, in order, whatever the server answered (it also cleans up by itself) */
    if(kind==JF_R_ENCODING_STOP){a->tv_close=(uint8_t)(a->live_id[0]?JF_CLOSE_LIVE:JF_CLOSE_NONE);return;}
    if(kind==JF_R_LIVE_CLOSE){a->tv_close=JF_CLOSE_NONE;a->live_id[0]=0;return;}
    if(kind==JF_R_PLAYINFO) {
        int go=a->playinfo_for_play&&a->screen==JF_S_PLAYER&&!a->playing; /* not if B (or another channel) came meanwhile */
        /* What the reply named, read as it arrived (also from a reply that broke off after the ids, or that nobody
         * waits for any more): bit 0 a media source, bit 1 a live stream now held in live_id, bit 2 a live stream
         * whose id is too long to play with (its close is already out). */
        int got=jf_pi_take(a);
        int ok=status==200&&(got&1)&&!(got&4)&&(a->cfg.mode!=JF_MODE_TV||(got&2));
        a->playinfo_for_play=0;a->redraw=1;
        if(!ok)a->source_id[0]=0;
        if(!(go&&ok)&&(got&2)&&a->tv_close==JF_CLOSE_NONE)jf_server_close(a,0); /* opened for nobody: closed again */
        /* No answer (in time, or given up because nobody waited: N4): the server may have opened the channel all the
         * same and its id is unknown, so that channel is not asked again within the reopen time (review H2). */
        if(a->cfg.mode==JF_MODE_TV&&status<0&&!(got&6)) {
            unsigned k=a->tv_closed_next&1u;
            jf_copy(a->tv_closed_id[k],33,a->playinfo_id);a->tv_closed_ms[k]=a->now;a->tv_closed_next=(uint8_t)(k^1u);
        }
        if(!go)return;
        if(ok) {
            if(a->want==JF_W_PLAY)a->want=JF_W_NONE;
            if(a->cfg.mode==JF_MODE_TV){a->tv_state=JF_TV_PLAYING;a->tv_open_ms=a->now;}
            jf_play_start(a);
            if(a->cfg.mode==JF_MODE_TV&&a->playing){jf_copy(a->message,sizeof(a->message),"Tuning...");a->osd=0;}
            return;
        }
        if(a->cfg.mode==JF_MODE_TV&&status!=401) {
            /* No answer in time: the server may have opened the channel all the same, and its id is unknown, so the
             * same channel is not asked again within the reopen time (review H2). */
            jf_tv_nosignal(a,status<0?"The server did not answer":"No signal");return;
        }
        if(a->cfg.mode==JF_MODE_TV){a->tv_state=JF_TV_IDLE;a->screen=JF_S_CHANNELS;} /* 401: the sign-in page below */
        else {
            a->screen=JF_S_DETAILS;
            if(status!=401){if(status<0)jf_fail(a,"The server did not answer");else jf_fail_video(a,"Could not start the video",1);return;}
        }
    }
    if(kind==JF_R_CHANNELS&&status!=200) {
        if(a->screen==JF_S_PLAYER){a->tv_state=JF_TV_IDLE;a->screen=JF_S_CHANNELS;} /* a page asked for while stepping channels failed */
        a->tv_resume=JF_TV_RESUME_NONE;
        /* Review M4: the list never ends on the error page, where the Local Weather row could not be reached. After
         * the usual five tries (or at once for a refusal) the list shows that row, "Try again", and why. */
        if(!(status<0&&status!=-3&&(a->retries<5||a->link_down))) { /* -3: a page that does not fit will not fit next time either */
            a->tv_list_err=(uint8_t)(status==401?JF_TV_ERR_SIGNIN:status<0?JF_TV_ERR_UNREACHABLE:JF_TV_ERR_OTHER);
            a->folder.count=0;a->folder_total=0;a->folder_start=0;a->folder.focus=0;a->retries=0;a->offline=0;a->redraw=1;
            return;
        }
    }
    if(kind==JF_R_IMAGE){jf_art_store(a,status==200);a->redraw=1;return;}
    if(kind==JF_R_STREAMS&&a->switching) { /* 1.3.2: the list for a mid-play switch: reopen with the choice */
        if(status==200)jf_pick_streams(a);else a->streams_done=1;
        a->redraw=1;
        if(a->playing)jf_switch_finish(a);else a->switching=0;
        return;
    }
    if(kind==JF_R_STREAMS) { /* track list fetched (or failed): pick English, then start the video */
        int go=a->streams_for_play&&a->screen==JF_S_PLAYER&&!a->playing; /* not if B cancelled while it was out */
        a->streams_for_play=0;
        if(status==200)jf_pick_streams(a);else{a->audio_index=-1;a->sub_index=-1;a->streams_done=1;}
        a->redraw=1; /* the parse used the screen as scratch: whatever screen is up is drawn again (1.2.6 review, note 2) */
        if(go){if(a->want==JF_W_PLAY)a->want=JF_W_NONE;jf_play_start(a);} /* a queued Play is this one, not a second start (note 1) */
        return;
    }
    if(status==401){jf_copy(a->message,sizeof(a->message),"Jellyfin sign-in expired");a->screen=JF_S_ERROR;a->err_kind=JF_ERR_SIGNIN;
        jf_copy(a->detail_line,sizeof(a->detail_line),"Select: sign in again here.");a->redraw=1;return;}
    if(status<200||status>299) {
        if(status==-3){jf_fail(a,"The server sent more than fits");return;}
        if(status<0&&(a->retries<5||a->link_down)) { /* the link is down: keep trying (the shell rejoins), the count waits */
            if(!a->link_down)a->retries++;
            a->offline=1;a->retry_at=a->now+(a->link_down?2000u:1000u*a->retries);a->busy=kind;a->redraw=1;return;
        }
        jf_fail(a,status<0?"Can't reach the Jellyfin server":"The server refused the request");
        if(status<0) { /* 1.3.0: the server may have a new address: Select finds it again on this network */
            a->err_kind=JF_ERR_UNREACHABLE;jf_copy(a->detail_line,sizeof(a->detail_line),"Moved? Select finds it again.");
        }
        return;
    }
    a->retries=0;a->offline=0;a->redraw=1; /* the parse below used the screen as scratch */
    char path[200];
    if(kind==JF_R_CHANNELS) { /* 1.6.0: one page of the TV channel list */
        uint32_t total=0;int n=jf_parse_channels(a,&total);
        if(n<0) {
            a->screen=JF_S_CHANNELS;a->tv_state=JF_TV_IDLE;a->tv_resume=JF_TV_RESUME_NONE;a->tv_list_err=JF_TV_ERR_OTHER;
            a->folder.count=0;a->folder_total=0;a->folder_start=0;a->folder.focus=0;a->redraw=1;return;
        }
        a->tv_list_err=JF_TV_ERR_NONE;
        if(!n&&a->folder_start){a->folder_start=0;a->folder.focus=0;jf_load_channels(a);return;} /* the list got shorter */
        a->folder.count=(uint8_t)n;a->folder.shape=JF_SHAPE_THUMB;a->folder_total=total;
        unsigned rows=jf_tv_rows(a);
        if(a->folder.focus>=rows)a->folder.focus=(uint8_t)(rows?rows-1:0); /* also "the last row" asked for when paging up */
        a->redraw=1;
        if(a->tv_resume!=JF_TV_RESUME_NONE&&a->screen==JF_S_CHANNELS&&a->tv_state==JF_TV_IDLE) {
            /* back from the Local Weather channel by Up or Down: the step is taken from its row, as if it were on */
            int dir=a->tv_resume==JF_TV_RESUME_NEXT?1:-1;
            a->tv_resume=JF_TV_RESUME_NONE;
            if(a->folder_start==0&&jf_tv_extra(a)&&a->folder_total){a->folder.focus=0;jf_tv_surf(a,dir);}
        }
        return;
    }
    if(kind==JF_R_VIEWS&&a->cfg.mode==JF_MODE_TUBE) { /* 1.6.0: ChromaTube's root is the library of that name */
        jf_item root;memset(&root,0,sizeof(root));
        pj_json j;
        if(!a->tokens||!pj_json_parse(&j,a->buf,a->reply_size,a->tokens,a->token_cap)){jf_fail(a,"Unexpected reply from the server");return;}
        int list=j.tokens[0].type==PJ_JSON_OBJECT?pj_json_find(&j,0,"Items"):0;
        if(list<0||j.tokens[list].type!=PJ_JSON_ARRAY){jf_fail(a,"Unexpected reply from the server");return;}
        for(unsigned i=(unsigned)list+1;i<j.tokens[list].next&&!root.id[0];i=j.tokens[i].next) {
            char name[32]="",id[40]="";
            if(j.tokens[i].type!=PJ_JSON_OBJECT)continue;
            pj_json_text(&j,pj_json_find(&j,(int)i,"Name"),name,sizeof(name));
            pj_json_text(&j,pj_json_find(&j,(int)i,"Id"),id,sizeof(id));
            if(!strcmp(name,JF_TUBE_LIBRARY)&&jf_hex_id(id))jf_copy(root.id,sizeof(root.id),id);
        }
        if(!root.id[0]){jf_fail(a,JF_MSG_TUBE_MISSING);jf_copy(a->detail_line,sizeof(a->detail_line),JF_MSG_TUBE_MISSING_STEP);return;}
        jf_copy(root.name,sizeof(root.name),JF_TUBE_LIBRARY);root.type=JF_T_FOLDER;
        a->screen=JF_S_CONNECT;jf_open_folder(a,&root);return; /* from Connect: depth 0, so B there leaves the app */
    }
    if(kind==JF_R_VIEWS) {
        memset(&a->views,0,sizeof(a->views));
        int n=jf_parse_list(a,a->views.items,JF_ROW_ITEMS,JF_SHAPE_THUMB,NULL);
        if(n<0){jf_fail(a,"Unexpected reply from the server");return;}
        a->views.count=(uint8_t)n;a->views.shape=JF_SHAPE_THUMB;jf_copy(a->views.title,sizeof(a->views.title),"My Media");
        snprintf(path,sizeof(path),"/UserItems/Resume?userId=%s&limit=%u&mediaTypes=Video&%s",a->cfg.user,JF_ROW_LIMIT,JF_FIELDS);
        jf_get(a,JF_R_RESUME,path);return;
    }
    if(kind==JF_R_RESUME) {
        /* Jellyfin's Home order: My Media, Continue Watching, Next Up, Latest per library. */
        if(a->views.count){a->rows[0]=a->views;a->row_count=1;}
        jf_add_row(a,"Continue Watching",JF_SHAPE_THUMB);
        snprintf(path,sizeof(path),"/Shows/NextUp?userId=%s&limit=%u&%s",a->cfg.user,JF_ROW_LIMIT,JF_FIELDS);
        jf_get(a,JF_R_NEXTUP,path);return;
    }
    if(kind==JF_R_NEXTUP){jf_add_row(a,"Next Up",JF_SHAPE_THUMB);a->view_next=0;jf_after_home_step(a);return;}
    if(kind==JF_R_LATEST) {
        char title[28]="Latest ";jf_copy(title+7,sizeof(title)-7,a->views.items[a->view_next].name);
        jf_add_row(a,title,JF_SHAPE_POSTER);a->view_next++;jf_after_home_step(a);return;
    }
    if(kind==JF_R_FOLDER) {
        uint32_t total=0;
        int n=jf_parse_list(a,a->folder.items,JF_PAGE_ITEMS,JF_SHAPE_POSTER,&total);
        if(n<0){jf_fail(a,"Unexpected reply from the server");return;}
        a->folder.shape=JF_SHAPE_POSTER;
        /* Episodes show their own stills, as the Pocket's library grid does. */
        if(n&&a->folder.items[0].type==JF_T_EPISODE){jf_parse_list(a,a->folder.items,JF_PAGE_ITEMS,JF_SHAPE_THUMB,&total);a->folder.shape=JF_SHAPE_THUMB;}
        a->folder.count=(uint8_t)n;if(a->folder.focus>=a->folder.count)a->folder.focus=a->folder.count?a->folder.count-1:0;
        a->folder_total=total;a->redraw=1;jf_next_image(a);return;
    }
    if(kind==JF_R_DETAILS) {
        pj_json j;
        int obj=-1;
        if(a->tokens&&pj_json_parse(&j,a->buf,a->reply_size,a->tokens,a->token_cap)&&j.tokens[0].type==PJ_JSON_OBJECT) {
            int list=pj_json_find(&j,0,"Items"); /* {"Items":[{...}]}: the one item asked for */
            if(list<0)obj=0; /* a single record, as /Items/{id} sends */
            else if(j.tokens[list].type==PJ_JSON_ARRAY&&(unsigned)list+1<j.tokens[list].next&&
                    j.tokens[list+1].type==PJ_JSON_OBJECT)obj=list+1;
        }
        if(obj>=0) {
            jf_item full;
            if(jf_parse_item(&j,obj,&full,JF_SHAPE_POSTER)) {
                if(!full.card_kind){jf_copy(full.card_id,33,a->detail.card_id);full.card_kind=a->detail.card_kind;}
                if(!full.hero_kind){jf_copy(full.hero_id,33,a->detail.hero_id);full.hero_kind=a->detail.hero_kind;}
                if(!full.sub[0])jf_copy(full.sub,sizeof(full.sub),a->detail.sub);
                a->detail=full;
            }
            pj_json_text(&j,pj_json_find(&j,obj,"Overview"),a->overview,sizeof(a->overview));
            int g=pj_json_find(&j,obj,"Genres");a->genres[0]=0;
            if(g>=0&&j.tokens[g].type==PJ_JSON_ARRAY)
                for(unsigned i=(unsigned)g+1,n=0;i<j.tokens[g].next&&n<3;i=j.tokens[i].next,n++) {
                    char one[20];
                    if(pj_json_text(&j,(int)i,one,sizeof(one))) {
                        if(a->genres[0])strncat(a->genres,", ",sizeof(a->genres)-strlen(a->genres)-1);
                        strncat(a->genres,one,sizeof(a->genres)-strlen(a->genres)-1);
                    }
                }
        }
        a->redraw=1;jf_next_image(a);return;
    }
}
static inline void jf_after_home_step(jf_app *a) {
    while(a->view_next<a->views.count&&a->row_count<JF_ROWS) {
        const jf_item *v=&a->views.items[a->view_next];
        char path[220];
        snprintf(path,sizeof(path),"/Items/Latest?userId=%s&parentId=%s&limit=%u&%s",a->cfg.user,v->id,JF_ROW_LIMIT,JF_FIELDS);
        if(jf_get(a,JF_R_LATEST,path)==0)return;
        a->view_next++;
    }
    a->screen=JF_S_HOME;a->redraw=1;jf_next_image(a);
}
/* The platform appends reply bytes; returns 0 when they no longer fit (it then reports a
 * failure status). Replies to playback reports are not kept: the buffer holds the video. */
static inline int jf_app_reply_data(jf_app *a,const uint8_t *data,size_t size) {
    if(a->request==JF_R_PLAYING||a->request==JF_R_PROGRESS||a->request==JF_R_STOPPED)return 1;
    if(a->request==JF_R_ENCODING_STOP||a->request==JF_R_LIVE_CLOSE)return 1; /* 1.6.0: nothing of these is kept either */
    if(a->request==JF_R_PLAYINFO){jf_pi_feed(a,data,size);return 1;} /* read as it arrives, whatever its size (review H2) */
    if(size>JF_BUF-a->reply_size)return 0;
    memcpy(a->buf+a->reply_size,data,size);a->reply_size+=size;return 1;
}

#include "jf_menu.h"
/* ---------- input ---------- */
static inline void jf_back_from_folder(jf_app *a) {
    if(a->depth) {
        a->depth--;jf_copy(a->folder_id,33,a->parents[a->depth]);jf_copy(a->folder_title,32,a->parent_titles[a->depth]);
        a->folder_start=a->parent_start[a->depth];a->folder.count=0;a->folder.focus=0;a->redraw=1;jf_load_folder(a);
    } else if(a->cfg.mode==JF_MODE_TUBE){a->exit_to=JF_EXIT_MENU;a->screen=JF_S_EXIT;} /* 1.6.0: ChromaTube's root: back to the start-up menu */
    else {a->screen=JF_S_HOME;a->redraw=1;}
}
static inline void jf_activate(jf_app *a,const jf_item *it) {
    if(it->type==JF_T_SERIES||it->type==JF_T_SEASON||it->type==JF_T_FOLDER)jf_open_folder(a,it);
    else jf_open_details(a,it);
}
static inline void jf_folder_page(jf_app *a,int forward) {
    if(forward&&a->folder_start+a->folder.count<a->folder_total){a->folder_start+=JF_PAGE_ITEMS;a->folder.focus=0;jf_load_folder(a);}
    else if(!forward&&a->folder_start){a->folder_start=a->folder_start>JF_PAGE_ITEMS?a->folder_start-JF_PAGE_ITEMS:0;
        a->folder.focus=JF_PAGE_ITEMS-1;jf_load_folder(a);}
}
/* Details offers Resume next to "From start" for a title left part-way; ChromaTube (remote streams) always starts
 * at the beginning in 1.6.0. */
static inline unsigned jf_detail_actions(const jf_app *a) {
    return a->cfg.mode!=JF_MODE_TUBE&&a->detail.resume_s>30&&!a->detail.played?2u:1u;
}
static inline void jf_app_press(jf_app *a,uint16_t pressed) {
    if(!pressed)return;
    if(a->cfg.mode==JF_MODE_TV&&(a->screen==JF_S_PLAYER||a->screen==JF_S_CHANNELS)) { /* 1.6.0 */
        if(a->tv_exit)return; /* leaving: the closes are going out */
        if(a->screen==JF_S_CHANNELS) {
            unsigned extra=jf_tv_extra(a);
            if(pressed&JF_BTN_B){a->tv_exit=1;a->tv_at=a->now;a->redraw=1;} /* back to the start-up menu */
            else if(pressed&(JF_BTN_A|JF_BTN_START)) {
                if(a->tv_list_err&&a->folder.focus>=extra){a->tv_list_err=JF_TV_ERR_NONE;a->retries=0;a->folder_start=0;jf_load_channels(a);} /* Try again */
                else if(a->folder.focus<extra||(!jf_tv_loading(a)&&a->folder.focus<jf_tv_rows(a)))jf_tv_tune(a); /* the weather row at any time */
            }
            else if((pressed&JF_BTN_SELECT)&&a->tv_list_err==JF_TV_ERR_SIGNIN){a->exit_to=JF_EXIT_SETUP;a->screen=JF_S_EXIT;}     /* sign in again */
            else if((pressed&JF_BTN_SELECT)&&a->tv_list_err==JF_TV_ERR_UNREACHABLE){a->exit_to=JF_EXIT_FIND;a->screen=JF_S_EXIT;} /* find my server again */
            else if(pressed&JF_BTN_UP)jf_tv_move(a,-1);
            else if(pressed&JF_BTN_DOWN)jf_tv_move(a,1);
            return;
        }
        if(pressed&JF_BTN_B)jf_tv_to_list(a);                           /* B: the list, whatever is on */
        else if(pressed&(JF_BTN_UP|JF_BTN_DOWN))jf_tv_surf(a,pressed&JF_BTN_UP?1:-1);
        else if(pressed&(JF_BTN_A|JF_BTN_START|JF_BTN_SELECT)) {         /* which channel is this: the banner again */
            a->tv_banner_until=a->now+JF_TV_BANNER_MS;if(!a->tv_banner_until)a->tv_banner_until=1;a->redraw=1;
        }
        return;
    }
    /* Select, or the Menu button when the media FPGA forwards it: the Jellyfin menu (jf_menu.h). */
    if((pressed&(JF_BTN_SELECT|JF_BTN_MENU))&&(a->screen==JF_S_HOME||a->screen==JF_S_FOLDER||a->screen==JF_S_DETAILS)) {
        jf_menu_open(a);jf_next_image(a);return;
    }
    switch(a->screen) {
    case JF_S_MENU:jf_menu_press(a,pressed);break;
    case JF_S_HOME: {
        if(!a->row_count){if(pressed&JF_BTN_B)a->screen=JF_S_EXIT;break;}
        jf_row *r=&a->rows[a->row_focus];
        if(pressed&JF_BTN_UP&&a->row_focus){a->row_focus--;a->redraw=1;}
        else if(pressed&JF_BTN_DOWN&&a->row_focus+1<a->row_count){a->row_focus++;a->redraw=1;}
        else if(pressed&JF_BTN_LEFT&&r->focus){r->focus--;a->redraw=1;}
        else if(pressed&JF_BTN_RIGHT&&r->focus+1<r->count){r->focus++;a->redraw=1;}
        else if(pressed&(JF_BTN_A|JF_BTN_START)){if(r->count)jf_activate(a,&r->items[r->focus]);}
        else if(pressed&JF_BTN_B)a->screen=JF_S_EXIT;
        break;
    }
    case JF_S_FOLDER: {
        jf_page *p=&a->folder;int cols=(int)jf_columns(a,p->shape),f=p->focus;
        if(pressed&(JF_BTN_A|JF_BTN_START)){if(p->count)jf_activate(a,&p->items[p->focus]);break;}
        if(pressed&JF_BTN_B){jf_back_from_folder(a);break;}
        if(!p->count)break;
        int step=pressed&JF_BTN_LEFT?-1:pressed&JF_BTN_RIGHT?1:pressed&JF_BTN_UP?-cols:pressed&JF_BTN_DOWN?cols:0;
        if(!step)break;
        int to=f+step;
        if(to<0)jf_folder_page(a,0);
        else if(to>=(int)p->count){if(a->folder_start+p->count<a->folder_total)jf_folder_page(a,1);
            else if(step>1)p->focus=(uint8_t)(p->count-1);}
        else p->focus=(uint8_t)to;
        a->redraw=1;break;
    }
    case JF_S_DETAILS: {
        unsigned actions=jf_detail_actions(a);
        if(pressed&JF_BTN_LEFT){if(a->detail_action)a->detail_action--;a->redraw=1;}
        else if(pressed&JF_BTN_RIGHT){if(a->detail_action+1u<actions)a->detail_action++;a->redraw=1;}
        else if(pressed&(JF_BTN_A|JF_BTN_START)){if(jf_playable(&a->detail))jf_play(a,actions==2&&a->detail_action==0?a->detail.resume_s:0);}
        else if(pressed&JF_BTN_B){a->screen=a->detail_from==JF_S_FOLDER?JF_S_FOLDER:JF_S_HOME;a->redraw=1;}
        break;
    }
    case JF_S_PLAYER:
        if(pressed&JF_BTN_B)jf_stop(a,1);
        else if(!a->playing||a->reconnect_at||a->switching)break; /* still loading: only B counts */
        else if((pressed&JF_BTN_MENU)&&a->cfg.mode==JF_MODE_TUBE){} /* 1.6.0: no menu over a ChromaTube video (its Audio row reopens at a position) */
        else if(pressed&JF_BTN_MENU)jf_menu_open(a); /* 1.3.2: the menu over the video (Audio row focused), media FPGA */
        else if(pressed&(JF_BTN_A|JF_BTN_START)) {
            a->paused=!a->paused;a->osd=1;a->osd_until=a->now+3000;a->redraw=1;
            if(a->audio_on&&!a->audio_dead&&a->io.audio_pause)a->io.audio_pause(a->io.ctx,a->paused);
            if(!a->paused){a->clock_ms=a->now-(uint32_t)((uint64_t)(a->avi.frame_index-(uint32_t)a->base_frame)*jf_avi_frame_us(&a->avi)/1000u);a->audio_played_at=a->now;
                a->starve_mark=a->audio_played;a->starve_ms=a->now;a->window_ms=a->now;} /* the pause is nobody's deficit */
            jf_report(a,JF_R_PROGRESS);
        }
        else if((pressed&(JF_BTN_LEFT|JF_BTN_RIGHT))&&a->cfg.mode==JF_MODE_TUBE){a->osd=1;a->osd_until=a->now+3000;a->redraw=1;} /* 1.6.0, review H3: no seek on ChromaTube */
        else if(pressed&JF_BTN_LEFT)jf_seek(a,-(int)JF_SEEK_S);
        else if(pressed&JF_BTN_RIGHT)jf_seek(a,(int)JF_SEEK_S);
        else if(pressed&(JF_BTN_SELECT|JF_BTN_UP|JF_BTN_DOWN)){a->osd=!a->osd;a->osd_until=a->now+4000;a->redraw=1;}
        break;
    case JF_S_ERROR:
        if(pressed&(JF_BTN_A|JF_BTN_START)){a->screen=JF_S_CONNECT;a->retries=0;a->redraw=1;jf_load_home(a);}
        else if((pressed&JF_BTN_SELECT)&&a->err_kind==JF_ERR_UNREACHABLE){a->exit_to=JF_EXIT_FIND;a->screen=JF_S_EXIT;} /* Find my server again */
        else if((pressed&JF_BTN_SELECT)&&a->err_kind==JF_ERR_SIGNIN){a->exit_to=JF_EXIT_SETUP;a->screen=JF_S_EXIT;}   /* sign in again */
        else if(pressed&JF_BTN_B){a->exit_to=JF_EXIT_MENU;a->screen=JF_S_EXIT;}
        break;
    case JF_S_CONNECT:
        if(pressed&JF_BTN_B)a->screen=JF_S_EXIT;
        break;
    }
    jf_next_image(a);
}

/* ---------- lifecycle ---------- */
static inline void jf_app_start(jf_app *a,const jf_platform *io,const jf_config *cfg,uint32_t now) {
    memset(a,0,sizeof(*a));a->io=*io;a->cfg=*cfg;a->now=now;a->screen=JF_S_CONNECT;a->redraw=1;
    a->audio_index=-1;a->sub_index=-1; /* -1: the server's default track until a MediaStreams reply picks one */
    a->art_bytes=cfg->art;a->art_cap=cfg->art&&cfg->art_cap<=JF_ART_MAX?cfg->art_cap:0;
    a->tokens=cfg->scratch;a->token_cap=cfg->scratch?(unsigned)(cfg->scratch_bytes/sizeof(pj_json_token)):0;
    if(a->token_cap>65534u)a->token_cap=65534u;
    if(a->cfg.mode>JF_MODE_TUBE)a->cfg.mode=JF_MODE_JELLYFIN;
    if(a->cfg.mode!=JF_MODE_TV){a->cfg.wx_row=0;a->cfg.tv_resume=JF_TV_RESUME_NONE;}
    a->tv_resume=a->cfg.tv_resume;
    jf_load_home(a); /* TV: the channel list; ChromaTube: the views, then its library; Jellyfin: Home */
}
/* 1.6.0 (review H2): the console is about to leave for Games with this app open. TV and ChromaTube: what plays is
 * stopped and the server closes are queued; the platform then ticks the app for a short bounded time while
 * jf_app_owes says something is still owed (a close, or a PlaybackInfo whose live stream must be closed when its
 * reply comes). Jellyfin mode: nothing, as before. */
static inline void jf_app_quiesce(jf_app *a) {
    if(a->cfg.mode==JF_MODE_TV)jf_tv_leave(a);
    else if(a->cfg.mode==JF_MODE_TUBE&&(a->playing||a->screen==JF_S_PLAYER))jf_stop(a,1);
}
/* N4: the request that is out is a PlaybackInfo nobody waits for any more (B, another channel, the weather row).
 * The platform gives such a request a few seconds more (its ids, if they come, close the live stream) and then
 * drops it, so the app's one request slot is not held for up to 30 s. */
static inline int jf_app_request_unwanted(const jf_app *a){return a->request==JF_R_PLAYINFO&&!a->playinfo_for_play;}
static inline int jf_app_owes(const jf_app *a) {
    if(a->cfg.mode==JF_MODE_JELLYFIN)return 0;
    return a->tv_close!=JF_CLOSE_NONE||a->request==JF_R_PLAYINFO||a->request==JF_R_ENCODING_STOP||a->request==JF_R_LIVE_CLOSE;
}
/* The platform's Wi-Fi link: down while the shell rejoins (a lost stream then waits for it), up again. */
static inline void jf_app_set_link(jf_app *a,int up) {
    if(a->link_down==(uint8_t)!up)return;
    a->link_down=(uint8_t)!up;a->redraw=1;
    if(up&&a->playing&&a->reconnect_at){a->reconnect_at=a->now;jf_copy(a->message,sizeof(a->message),JF_MSG_LINK_BACK);} /* reopen now */
    if(up&&a->busy)a->retry_at=a->now;
}
/* Before leaving the app (B on Home, or the overlay closing): stop and report. */
static inline void jf_app_close(jf_app *a) {
    if(a->cfg.mode==JF_MODE_TV){jf_tv_leave(a);return;} /* 1.6.0: the stream closes; closes still owed are the server's to clean up */
    if(a->playing)jf_stop(a,1);
}
/* Call often (every loop). Returns 1 when the screen should be redrawn. */
static inline int jf_app_tick(jf_app *a,uint32_t now) {
    a->now=now;
    if(a->busy&&(a->playing||a->screen==JF_S_PLAYER))a->busy=0; /* never fire a browse retry into playback */
    if(a->busy&&a->request==JF_R_NONE&&(int32_t)(now-a->retry_at)>=0) {
        uint8_t step=a->busy;a->busy=0; /* only if that screen is still up: never pull the user back */
        if(step==JF_R_FOLDER){if(a->screen==JF_S_FOLDER)jf_load_folder(a);}
        else if(step==JF_R_DETAILS){if(a->screen==JF_S_DETAILS)jf_open_details(a,&a->detail);}
        else if(step==JF_R_CHANNELS){if(a->screen==JF_S_CHANNELS)jf_load_channels(a);} /* 1.6.0 */
        else if(a->screen==JF_S_HOME||a->screen==JF_S_CONNECT)jf_load_home(a);
    }
    if(a->cfg.mode==JF_MODE_TV)jf_tv_tick(a);            /* 1.6.0: the closes, the surf and reopen waits, No signal, the weather channel */
    else if(a->cfg.mode==JF_MODE_TUBE)jf_close_pump(a);  /* a live stream PlaybackInfo opened for a video, if ever */
    if(a->want&&a->request==JF_R_NONE)jf_do(a,a->want);
    if(a->playing)jf_player_tick(a);
    else jf_next_image(a);
    if(a->playing&&a->osd&&!a->paused&&(int32_t)(now-a->osd_until)>=0&&a->shown){a->osd=0;a->redraw=1;}
    if(a->playing&&a->osd&&(uint32_t)(now-a->osd_drawn)>=500u){a->osd_drawn=now;a->redraw=1;} /* the clock on the strip */
    if(a->note_until&&a->pictured&&(int32_t)(now-a->note_until)>=0){a->note_until=0;a->note[0]=0;a->redraw=1;} /* the note's time is up */
    if(a->note_until&&!a->pictured)a->note_until=now+JF_NOTE_MS; /* it counts from the first picture */
    int r=a->redraw;a->redraw=0;
    return jf_scroll_tick(a,r); /* a full redraw, or only the focused card's scrolling name */
}
#include "jf_ui.h"
#endif
