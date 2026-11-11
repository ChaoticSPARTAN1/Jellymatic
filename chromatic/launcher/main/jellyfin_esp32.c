/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * ESP32 binding of the Chromatic Jellyfin app (chromatic/jellyfin/jf_app.h).
 *
 *  - Memory: everything here (the app, its artwork cache, network buffers, task stacks and
 *    the stream buffer) is allocated when Jellyfin opens and freed when it closes, so the
 *    menu keeps its usual headroom. Video decodes straight into the launcher's overlay
 *    frame, and JSON is parsed in that same frame while browsing (each browsing screen is
 *    redrawn in full afterwards). The artwork cache takes what is left after a reserve.
 *  - Network: one HTTP task for library/artwork/report requests and one stream task for
 *    the video, each with its own buffers, so the main loop (which feeds the watchdog)
 *    never waits on the network. Plain HTTP on the owner's LAN, as the server is set up;
 *    the console's own token goes only in the Authorization header, never a URL or a log.
 *  - Pictures: the ESP32 ROM's TJpgDec (baseline JPEG, which is what Jellyfin sends for
 *    MJPEG and for images), output converted to RGB565, fitted and centred.
 *  - Sound (docs/Design-notes.md), only when pj_jf_begin is told the FPGA has media
 *    audio: the app hands IMA ADPCM bytes to a 1.5 KB ring; a small task decodes them
 *    (jf_audio.h) and writes 16-bit stereo (the sample in both slots) into an I2S DMA ring,
 *    ESP32 as I2S master on IO25 (bit clock), IO26 (word select), IO27 (data), the pins the
 *    stock FPGA wires to inputs it never reads. An empty ring never stalls anything: the task
 *    waits for it while the DMA plays what it already holds, and the driver's auto-clear plays
 *    zeros only once the DMA really runs out (launchers up to 1.2.6 wrote a buffer of silence the
 *    moment the ring was empty, even with 24 ms of sound still queued: a hitch per short gap).
 *    Everything for sound exists only while a video plays on the media FPGA, about 6.8 KB:
 *    the ring and the task (1.6 KB + 2.3 KB stack + ~0.4 KB TCB) are claimed with the stream
 *    buffer when the video opens, out of the hole the artwork cache leaves, before the first
 *    second of the stream (when Wi-Fi and lwIP hold their most: the first sound build claimed
 *    its memory at the first audio block, inside that burst, and drove the heap to 0.4 KB);
 *    the I2S channel (4 x 512 B DMA + ~0.8 KB driver objects) is opened at the same time, before
 *    the burst, so the DMA is primed when the first block arrives; it is freed when the sound stops
 *    or the sink is declared dead, and the ring and task go back when the video ends.
 * Launcher 1.3.0, the Pocket link (docs/Design-notes.md): the same binding also carries the link
 * cable gateway (shared/pocket_link.h) INSTEAD of the app, never beside it. pj_jl_begin allocates one pj_plink (about
 * 40 KB, heap; 1.3.7: in four pieces of at most 12 KB, pocket_link.h PJ_PLINK_PIECE_LIMIT) and the same buffers and two tasks the app uses; the HTTP task, the stream task and TJpgDec serve the
 * gateway (artwork in row windows); the gateway writes the FPGA UART (UART1) directly, never blocking, and switches it
 * to the fast rate only after the FPGA said so (keyed, reverted on any fault and on leaving). It runs only on the link
 * FPGA (18.41): the launcher checks pj_fpga_pocket_link before calling pj_jl_begin. pj_jl_end leaves the link mode
 * (the FPGA hands the cable back to the game) and frees everything through pj_jf_poll once the tasks are gone.
 * Nothing here writes flash or NVS.
 */
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "lwip/tcpip.h"           /* 1.3.8: the link's TIME_WAIT count, taken in the TCP/IP task */
#include "lwip/priv/tcp_priv.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "rom/tjpgd.h"
#include "driver/i2s_std.h"
#include "driver/uart.h"
#include "hal/uart_ll.h"
#include "soc/uart_struct.h"
#include "ui_font.h"
#include "../../jellyfin/jf_app.h"
#include "pocket_link.h"
#include "launcher_diag.h" /* 1.3.5: pj_diag_token for the plink trace line */
#include "jellyfin_esp32.h"

/* Between the stream task's 1 KB socket reads and the player's 512 B reads; the server runs ahead
 * and TCP's 5.7 KB window paces it, so this only needs to ride out the 10 ms loop (43 ms of the
 * stream at 95 KB/s). It was 8 KB; the 4 KB saved stay free for Wi-Fi. */
#define PJ_JF_STREAM_BUFFER 4096u
/* 1.3.3: the shortest real sleep, never 0 ticks (vTaskDelay(0) only yields; see stream_task). */
#define PJ_JF_WAIT_TICK ((TickType_t)1)
/* Sound: pins are the FPGA's I2S_WS / I2S_DIN / I2S_DOUT (top.v), unused inputs on the stock image,
 * read by chromatic/fpga-media/chromatic_media.v as bit clock / word select / data. */
#define PJ_JF_I2S_BCLK GPIO_NUM_25
#define PJ_JF_I2S_WS GPIO_NUM_26
#define PJ_JF_I2S_DOUT GPIO_NUM_27
#define PJ_JF_AUDIO_RING 2048u        /* ADPCM: two 1,024-byte blocks (256 ms): rides out a long main-loop cycle */
/* The sound task's own measured use was 1,452 B of 4,096 with 64-byte pieces,
 * so 2,304 B leaves it about 850 B in hand. */
#define PJ_JF_AUDIO_STACK 2304u
#define PJ_JF_AUDIO_DMA_BUFS 4u       /* the driver needs 2; 4 gives the task three buffers (24 ms) to refill one */
#define PJ_JF_AUDIO_DMA_FRAMES 128u   /* stereo 16-bit frames per DMA buffer: 512 B, 8 ms at 16 kHz (4 x = 32 ms) */
#define PJ_JF_AUDIO_PIECE 64u         /* ADPCM bytes decoded per I2S write: 128 samples, 8 ms (fewer writes to refill the DMA) */
#define PJ_JF_AUDIO_MSG 256u          /* largest ADPCM piece the app hands the ring in one message */
#define PJ_JF_AUDIO_RATE 16000u       /* the rate the stream is asked for: the channel opens at it before the burst */
/* esp_http_client: URL parse, headers, chunked bodies. Measured on the console: the HTTP
 * task used 1,800 B of 5,120 and the stream task 1,812 B, both after DNS, connects and chunked
 * replies; 3,584 B keeps about 1.8 KB (one full extra use) in hand on each. */
#define PJ_JF_STACK 3584u
/* Left free for Wi-Fi, lwIP, the HTTP clients and Remote Pilot. The first console run had a
 * 16 KB reserve and fell to 0.5 KB free while browsing (requests failed, art and paging
 * stalled), so the reserve is larger and the artwork cache is capped below JF_ART_MAX. */
#define PJ_JF_RESERVE 24576u
#define PJ_JF_ART_MIN 4096u
#define PJ_JF_ART_CAP 12288u
#define PJ_JF_TIMEOUT_MS 10000
/* The video stream's socket: connect, headers and every read. 10 s (the request timeout) meant a stalled
 * server or link froze the picture for 10 s before the first reopen (launcher 1.2.8 on the console); 6 s
 * still leaves the transcoder time to start answering. */
#define PJ_JF_STREAM_TIMEOUT_MS 6000
/* The Pocket link (1.3.0): its stream is ~25 KB/s (128x96 JPEG + 16 kHz IMA), so 2 KB rides out the 10 ms loop with
 * room; the session starts only with this much free heap besides it (the app's reserve is 24 KB with an artwork cache
 * that shrinks; the link has no such slack, see docs/Design-notes.md "Launcher 1.3.0: memory"). */
#define PJ_JL_STREAM_BUFFER 2048u
#define PJ_JL_RESERVE 20480u
#define PJ_JL_RETRY_MS 3000u /* a stopped link mode is entered again this long after it ended */
#define PJ_JL_TX_RING 1024u   /* UART1's driver transmit ring: launcher_main.c buses_init, uart_driver_install(UART_NUM_1,4096,1024,...) */
#define PJ_JL_SURE_MS 120u    /* the wait for the line to drain (1.3.6: was 30, 20 and 50 ms): a full ring and FIFO is 100 ms at 115,200 */
#define PJ_JL_TX_ROOM 504u    /* 1.3.6: most bytes left waiting for the wire (7 data packets): the driver's ring never fills, 44 ms at 115,200 */
/* 1.3.8 (the stream failed with 532 B free). A connection the console closes first stays in lwIP's
 * TIME_WAIT for 2 x CONFIG_LWIP_TCP_MSL = 120 s, a whole tcp_pcb on the heap each (MEMP_MEM_MALLOC: no count limit; the
 * oldest is only reclaimed when the heap itself is out). A connection per artwork request left ~90 of them (~22 KB)
 * after an art sync. The link's requests now share one kept connection; the server closes it, or it goes after this
 * long idle (Jellyfin/Kestrel keeps an idle connection 130 s) and while a title plays. See
 * docs/Design-notes.md "Launcher 1.3.8: memory while streaming". */
#define PJ_JL_KEEP_MS 15000u
#define PJ_JL_STREAM_WAIT_MS 2000u /* the longest a link request waits for a closed stream to let its connection go */
/* 1.4.0: the link's stream open is tried this often, this far apart (stream_task). Three tries of at most the 6 s
 * stream timeout and two gaps stay inside the Pocket's 30 s "no stream data" limit (live_player.c). */
#define PJ_JL_OPEN_TRIES 3
#define PJ_JL_OPEN_GAP_MS 700u

typedef struct {
    uint32_t jpeg_work[3100/4];               /* TJpgDec R0.01 pool: FIRST and word-aligned (it holds 16/32-bit tables) */
    char base_url[129],auth[384];
    char http_url[560],stream_url[560];       /* one per task: they run at the same time */
    char req_method[8],req_path[420],req_body[300],stream_path[420];
    uint8_t http_chunk[1024],stream_chunk[1024];
} pj_jf_buffers;

static jf_app *app;
static pj_plink *plink; /* 1.3.0: the Pocket link gateway, only while the app is NOT running */
/* 1.3.7: the gateway's three large buffers, each its own heap block (pocket_link.h PJ_PLINK_PIECE_LIMIT): taken and
 * freed with plink, wired into it before pj_plink_init (which keeps them across the 3 s retry). */
static uint8_t *plink_buf,*plink_response;static pj_plink_scratch *plink_scratch;
static uint8_t *art;
static pj_jf_buffers *bufs;
static SemaphoreHandle_t req_go;
static volatile int req_status,req_done,req_busy,tasks_stop;
static StreamBufferHandle_t stream_sb; /* exists only while a video plays: it takes the artwork's place */
static SemaphoreHandle_t stream_lock; /* generation check + send, and bump + reset, are atomic */
static volatile int stream_generation,stream_state; /* 0 idle, 1 open, -1 ended, -2 failed */
static volatile int http_alive,stream_alive;
static volatile int stream_conn; /* 1.3.8: the stream task holds a server connection (open or still closing) */
/* 1.3.8 diagnostics of the link (pj_jl_net): connections opened / reused by the HTTP task, free heap when the stream's
 * headers came in, the loop's lowest free heap since the last line, lwIP's TIME_WAIT / active pcbs (counted in the
 * TCP/IP task, so as of the line before). */
static volatile uint32_t jl_http_new,jl_http_reused,jl_open_heap;
/* 1.4.0: the link's stream opens tried (all titles) and the last one's HTTP status (-1: no answer: connection or timeout) */
static volatile uint32_t jl_open_tries;static volatile int jl_open_status;
static uint32_t jl_low;
static volatile uint16_t jl_tw_pcbs,jl_tcp_pcbs;
static int closing; /* 1: pj_jf_end started, memory freed by pj_jf_poll once both tasks are gone;
                     * 2: freed, waiting for the idle task to free the tasks' stacks too */
static TickType_t closed_at;
static TaskHandle_t http_handle,stream_handle;
static size_t art_size;
static volatile uint32_t net_bytes; /* video bytes received, for the diagnostics line */
static uint32_t decode_us_sum,decode_us_max,decodes; /* since the last diagnostics line */
static int art_wanted; /* a video ended: claim the artwork arena at once */
static uint32_t art_tried_at; /* otherwise try again every 3 s while there is none */
/* Sound. The ring carries messages: [len low 8][start:1 | generation:3 | len high 4][bytes], each
 * written in one send (the reader never sees a header without its body); a flush bumps the
 * generation and the task throws away older messages, so no reader/writer race.
 * Lifetime: the ring and the task live from the video's stream open to its end (audio_prime /
 * audio_teardown); the I2S channel from the app's audio_start to its audio_stop. The task sets
 * audio_idle before every check of audio_armed and the main loop clears audio_armed before every
 * check of audio_idle, so the channel is only ever deleted while the task is provably not writing. */
static int sound_fpga; /* pj_jf_begin: the FPGA plays the I2S stream */
static i2s_chan_handle_t audio_tx;
static StreamBufferHandle_t audio_ring;
static TaskHandle_t audio_handle;
static volatile int audio_alive,audio_quit,audio_armed,audio_idle,audio_paused_flag,audio_release; /* release: task gone, free it */
static volatile int audio_want_channel; /* the I2S channel should exist (stream open -> stop/dead): see audio_service */
static volatile uint8_t audio_gen,audio_seen_gen;
static volatile uint32_t audio_written,audio_underruns; /* real frames handed to the DMA this segment; DMA dry events */
static volatile uint32_t audio_dma_frames,audio_rate_hz;
/* The DMA's own clock and fill, from the driver's callbacks (ISR): eofs counts finished buffers since
 * the channel opened (the real I2S rate: eofs x PJ_JF_AUDIO_DMA_FRAMES per diagnostics interval);
 * ovf fires when the DMA finished a buffer nobody refilled in time, i.e. it is playing zeros now.
 * total: frames written since the channel opened (all segments); eof_base: eofs when the written
 * frames were all played, so total-(eofs-eof_base)*frames is what the DMA still holds. */
static volatile uint32_t audio_eofs,audio_eof_base,audio_total,audio_dry_events;
static volatile uint8_t audio_dry_now; /* set by the ISR on ovf, cleared by the task's next write */
/* Diagnostics since the last pj_jf_status: ring fill min/max (bytes), longest gap between ring writes,
 * longest wait of the task on an empty ring, longest I2S write, DMA dry events by cause (ring empty
 * when the DMA ran dry / the task had data but was late), finished DMA buffers. */
static volatile uint32_t diag_ring_min,diag_ring_max,diag_gap_ms,diag_wait_ms,diag_i2s_us,diag_dry_ring,diag_dry_task,diag_eofs_mark;
static uint32_t audio_last_write_ms;
static int audio_prime(void);
static inline void diag_ring_sample(void) {
    if(!audio_ring)return;
    uint32_t n=(uint32_t)xStreamBufferBytesAvailable(audio_ring);
    if(n<diag_ring_min)diag_ring_min=n;
    if(n>diag_ring_max)diag_ring_max=n;
}

/* ---------- HTTP ---------- */
static esp_http_client_handle_t open_client(char *url,size_t url_size,const char *path,int method_post,int timeout_ms) {
    snprintf(url,url_size,"%s%s",bufs->base_url,path);
    esp_http_client_config_t config={.url=url,.timeout_ms=timeout_ms,.buffer_size=1024,.buffer_size_tx=512,
        .disable_auto_redirect=true,.method=method_post?HTTP_METHOD_POST:HTTP_METHOD_GET};
    esp_http_client_handle_t client=esp_http_client_init(&config);
    if(client)esp_http_client_set_header(client,"Authorization",bufs->auth);
    return client;
}
static void http_drop(esp_http_client_handle_t *client) {
    if(*client){esp_http_client_close(*client);esp_http_client_cleanup(*client);*client=NULL;}
}
/* One request on `client` (its URL and headers set): sent, and the whole reply read. The status (>0), -1 failed,
 * -3 refused by the app. *answered: the reply's headers came back (from then on nothing is sent again). */
static int http_exchange(esp_http_client_handle_t client,int post,int *answered) {
    size_t body=post?strlen(bufs->req_body):0;
    *answered=0;
    if(post)esp_http_client_set_header(client,"Content-Type","application/json");
    if(esp_http_client_open(client,(int)body)!=ESP_OK||
       (body&&esp_http_client_write(client,bufs->req_body,(int)body)!=(int)body)||
       esp_http_client_fetch_headers(client)<0)return -1;
    *answered=1;
    int status=esp_http_client_get_status_code(client);
    int64_t length=esp_http_client_get_content_length(client),got=0;
    for(;;) {
        int n=esp_http_client_read(client,(char *)bufs->http_chunk,sizeof(bufs->http_chunk));
        if(n<0){status=-1;break;}
        if(n==0)break;
        got+=n;
        if(plink)pj_plink_reply_data(plink,bufs->http_chunk,(size_t)n); /* drops what the gateway no longer wants */
        else if(!jf_app_reply_data(app,bufs->http_chunk,(size_t)n)){status=-3;break;}
    }
    /* A connection that ends early (Wi-Fi, low memory) is a failure to retry, not a short page. */
    if(status>0&&(length>0?got!=length:!esp_http_client_is_complete_data_received(client)))status=-1;
    return status;
}
static void http_task(void *unused) {
    (void)unused;
    /* 1.3.8, the Pocket link only: its library and artwork requests share one kept connection (HTTP/1.1 keep-alive),
     * see PJ_JL_KEEP_MS. The Chromatic's own app is unchanged: a connection per request. */
    esp_http_client_handle_t keep=NULL;TickType_t keep_at=0;
    for(;;) {
        if(tasks_stop&&!req_busy)break; /* a request queued before leaving (Stopped) still goes out */
        /* The kept connection goes while a title plays (the stream's own connection has its memory) and when idle long
         * enough that the server may have closed it. */
        if(keep&&(stream_sb||(TickType_t)(xTaskGetTickCount()-keep_at)>=pdMS_TO_TICKS(PJ_JL_KEEP_MS)))http_drop(&keep);
        if(xSemaphoreTake(req_go,pdMS_TO_TICKS(200))!=pdTRUE)continue;
        int post=bufs->req_method[0]=='P',status=-1,answered=0;
        if(!plink||post) {
            esp_http_client_handle_t client=open_client(bufs->http_url,sizeof(bufs->http_url),bufs->req_path,post,PJ_JF_TIMEOUT_MS);
            if(client){status=http_exchange(client,post,&answered);http_drop(&client);}
        } else {
            /* A stream that was just closed lets its connection go first (its unread window is Wi-Fi buffers):
             * never a library request and a stream's connection at once. Bounded: a stuck stream read is 6 s. */
            for(TickType_t t0=xTaskGetTickCount();stream_conn&&(TickType_t)(xTaskGetTickCount()-t0)<pdMS_TO_TICKS(PJ_JL_STREAM_WAIT_MS);)
                vTaskDelay(PJ_JF_WAIT_TICK);
            for(int attempt=0;attempt<2;attempt++) {
                int reused=keep!=NULL;
                if(keep) {
                    snprintf(bufs->http_url,sizeof(bufs->http_url),"%s%s",bufs->base_url,bufs->req_path);
                    if(esp_http_client_set_url(keep,bufs->http_url)!=ESP_OK||esp_http_client_set_method(keep,HTTP_METHOD_GET)!=ESP_OK){http_drop(&keep);reused=0;}
                }
                if(!keep){keep=open_client(bufs->http_url,sizeof(bufs->http_url),bufs->req_path,0,PJ_JF_TIMEOUT_MS);if(!keep)break;jl_http_new++;}
                else jl_http_reused++;
                TickType_t sent_at=xTaskGetTickCount();
                status=http_exchange(keep,0,&answered);
                if(status>0){keep_at=xTaskGetTickCount();break;} /* the whole reply was read: the connection is kept */
                http_drop(&keep);
                /* Only a kept connection that failed at once without an answer (the server had closed it: FIN or RST)
                 * is tried once more, on a new one; GET only, so nothing is done twice. A slow server is not waited
                 * for twice. */
                if(answered||!reused||(TickType_t)(xTaskGetTickCount()-sent_at)>=pdMS_TO_TICKS(2000))break;
            }
        }
        /* Slot free first, then the reply: the main loop may chain the next request the moment
         * it takes req_done (the other core), so req_busy must already read 0. */
        req_status=status;req_busy=0;__sync_synchronize();req_done=1;
    }
    http_drop(&keep);
    http_alive=0;vTaskDelete(NULL);
}
static int platform_request(void *ctx,const char *method,const char *path,const char *body) {
    (void)ctx;
    if(req_busy||req_done||strlen(path)>=sizeof(bufs->req_path)||(body&&strlen(body)>=sizeof(bufs->req_body)))return -1;
    snprintf(bufs->req_method,sizeof(bufs->req_method),"%s",method?method:"GET");
    snprintf(bufs->req_path,sizeof(bufs->req_path),"%s",path);
    snprintf(bufs->req_body,sizeof(bufs->req_body),"%s",body?body:"");
    req_busy=1;req_done=0;xSemaphoreGive(req_go);
    return 0;
}

/* ---------- the video stream ---------- */
static void stream_task(void *unused) {
    (void)unused;
    while(!tasks_stop) {
        int generation=stream_generation;
        if(stream_state!=1){vTaskDelay(pdMS_TO_TICKS(20));continue;}
        stream_conn=1; /* 1.3.8: until the connection is gone (the link's requests wait for it) */
        esp_http_client_handle_t client=NULL;
        int result=-2,opened=0;
        /* 1.4.0 (the first Play over the Pocket link failed at once with "the stream failed", the
         * second, 2 s later, played): the Pocket link opens its stream up to PJ_JL_OPEN_TRIES times, PJ_JL_OPEN_GAP_MS
         * apart, before it fails: the server's first transcode of a title can be slow to answer or refuse while it
         * starts, and a connection can fail once. Only a connection that failed, timed out, or a 5xx answer is tried
         * again (a 4xx such as an expired sign-in is final), only while this stream is still the one wanted. The
         * Chromatic's own app keeps its single open (it has its own reopen). jl_open_* say what happened (pj_jl_net). */
        for(int attempt=0;attempt<(plink?PJ_JL_OPEN_TRIES:1)&&!tasks_stop&&generation==stream_generation;attempt++) {
            if(attempt){vTaskDelay(pdMS_TO_TICKS(PJ_JL_OPEN_GAP_MS));if(tasks_stop||generation!=stream_generation)break;}
            client=open_client(bufs->stream_url,sizeof(bufs->stream_url),bufs->stream_path,0,PJ_JF_STREAM_TIMEOUT_MS);
            int status=-1;
            if(client&&esp_http_client_open(client,0)==ESP_OK&&esp_http_client_fetch_headers(client)>=0)status=esp_http_client_get_status_code(client);
            if(plink){jl_open_tries++;jl_open_status=status;}
            if(status==200){opened=1;break;}
            if(client){esp_http_client_close(client);esp_http_client_cleanup(client);client=NULL;}
            if(status>=400&&status<500)break;
        }
        if(opened) {
            jl_open_heap=(uint32_t)esp_get_free_heap_size(); /* 1.3.8 diagnostics: free heap with the stream's headers in */
            for(;;) {
                if(tasks_stop||generation!=stream_generation){result=0;break;}
                int n=esp_http_client_read(client,(char *)bufs->stream_chunk,sizeof(bufs->stream_chunk));
                if(n<0){result=-2;break;}
                if(n==0){result=-1;break;}
                net_bytes+=(uint32_t)n;
                /* Back-pressure: wait for the player to take bytes (it reads at the title's pace).
                 * 1.3.3: the wait is one whole tick (10 ms at the launcher's 100 Hz tick). It was pdMS_TO_TICKS(5), which
                 * is 0 ticks at 100 Hz: vTaskDelay(0) only yields, so while a video played (the buffer is full nearly
                 * all the time: the server runs ahead) this priority-1 task spun on a core and that core's idle task
                 * never ran. The task watchdog then fired every 5 s and, although nothing is logged, printed a
                 * "Backtrace:" line with esp_rom_printf straight into UART0, in the middle of Remote Pilot's CHR1 bytes:
                 * the USB session broke every few seconds during playback only (docs/Design-notes.md,
                 * "USB during playback"). The buffer is full whenever this waits, and 10 ms of the stream (about 1.2 KB
                 * at 125 KB/s) is well inside the 4 KB it holds, so the player never waits for it. */
                size_t sent=0;
                while(sent<(size_t)n&&!tasks_stop) {
                    xSemaphoreTake(stream_lock,portMAX_DELAY);
                    int current=generation==stream_generation;
                    if(current&&stream_sb)sent+=xStreamBufferSend(stream_sb,bufs->stream_chunk+sent,(size_t)n-sent,0);
                    xSemaphoreGive(stream_lock);
                    if(!current)break;
                    if(sent<(size_t)n)vTaskDelay(PJ_JF_WAIT_TICK);
                }
            }
        }
        if(client){esp_http_client_close(client);esp_http_client_cleanup(client);}
        stream_conn=0;
        if(result!=0) { /* ended or failed; checked and stored under the lock so a newer stream keeps its state */
            xSemaphoreTake(stream_lock,portMAX_DELAY);
            if(generation==stream_generation)stream_state=result;
            xSemaphoreGive(stream_lock);
        }
    }
    stream_alive=0;vTaskDelete(NULL);
}
/* Artwork and the video buffer are never held at once: while a video plays the pictures
 * are not drawn, so the arena goes back to the heap and the stream buffer takes its place. */
static void art_release(void) {
    if(app)jf_art_reset(app,NULL,0);
    if(art){heap_caps_free(art);art=NULL;}
    art_size=0;art_wanted=1;
}
static void art_claim(void) {
    art_wanted=0;
    const uint32_t caps=MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL;
    size_t total=esp_get_free_heap_size(),block=heap_caps_get_largest_free_block(caps);
    size_t want=total>PJ_JF_RESERVE?total-PJ_JF_RESERVE:0;
    if(want>PJ_JF_ART_CAP)want=PJ_JF_ART_CAP;
    if(want>block)want=block;
    art=want>=PJ_JF_ART_MIN?heap_caps_malloc(want,caps):NULL;
    art_size=art?want:0; /* without it the cards show their titles instead of artwork */
    if(art&&app)jf_art_reset(app,art,(uint16_t)art_size); /* a failed try changes nothing on screen */
}
static int platform_stream_open(void *ctx,const char *path) {
    (void)ctx;
    if(strlen(path)>=sizeof(bufs->stream_path))return -1;
    if(!stream_sb) {
        art_release();
        stream_sb=xStreamBufferCreate(plink?PJ_JL_STREAM_BUFFER:PJ_JF_STREAM_BUFFER,1);
        if(!stream_sb)return -1;
        if(sound_fpga)audio_prime(); /* ring and task now, out of the artwork's hole; a failure is retried at the first block */
    }
    xSemaphoreTake(stream_lock,portMAX_DELAY);
    stream_generation++;stream_state=0;
    xStreamBufferReset(stream_sb); /* nobody blocks on it: sends and reads never wait */
    snprintf(bufs->stream_path,sizeof(bufs->stream_path),"%s",path);
    stream_state=1;
    xSemaphoreGive(stream_lock);return 0;
}
static void platform_stream_close(void *ctx) {
    (void)ctx;xSemaphoreTake(stream_lock,portMAX_DELAY);stream_generation++;stream_state=0;xSemaphoreGive(stream_lock);
    /* The buffer stays through reopens (quality, seek, reconnect), so a reopen never has to find 8 KB
     * again; pj_jf_tick frees it once the video has ended, and release() at exit. */
}
static int platform_stream_read(void *ctx,uint8_t *buf,size_t max) {
    (void)ctx;
    size_t n=stream_sb?xStreamBufferReceive(stream_sb,buf,max,0):0;
    if(n)return (int)n;
    if(stream_state==-1)return -1;
    if(stream_state==-2)return -2;
    return 0;
}

/* ---------- JPEG: ROM TJpgDec into RGB565 ---------- */
typedef struct {const uint8_t *data;size_t size,at;uint16_t *dst;int stride,w,h,ox,oy;} pj_jpeg_job;
static UINT jpeg_in(JDEC *jd,BYTE *buf,UINT n) {
    pj_jpeg_job *job=jd->device;
    size_t left=job->size-job->at;if(n>left)n=(UINT)left;
    if(buf)memcpy(buf,job->data+job->at,n);
    job->at+=n;return n;
}
/* The shell's I/O pump (USB and LAN output), run from inside the picture decode: a 160x120 picture is 80
 * MCU blocks and 40-50 ms on the console, during which the main loop pumps nothing; the UART0 transmit
 * FIFO (128 bytes, 11 ms at 115200 baud) then sat empty for most of every pass and Remote Pilot's USB
 * link fell to ~2 KB/s, so a 46 KB screen took ~20 s and the PC gave up and reopened (1.2.8). Called
 * every 8 blocks (about every 4-5 ms); the same task as the main loop, so nothing new runs concurrently. */
static void (*pump_hook)(void);static unsigned pump_count;
void pj_jf_set_pump(void (*hook)(void)){pump_hook=hook;}
static UINT jpeg_out(JDEC *jd,void *bitmap,JRECT *rect) {
    pj_jpeg_job *job=jd->device;const uint8_t *rgb=bitmap;
    if(pump_hook&&((++pump_count)&7u)==0)pump_hook();
    int rw=rect->right-rect->left+1,x0=rect->left,x1=rect->right,y0=rect->top,y1=rect->bottom;
    /* The picture always fits (the scale was chosen for it); clip per block, not per pixel:
     * this callback runs for every one of the 19,200 pixels and is part of the decode time. */
    if(job->ox+x0<0)x0=-job->ox;
    if(job->ox+x1>=job->w)x1=job->w-1-job->ox;
    if(job->oy+y0<0)y0=-job->oy;
    if(job->oy+y1>=job->h)y1=job->h-1-job->oy;
    for(int y=y0;y<=y1;y++) {
        const uint8_t *p=rgb+((y-rect->top)*rw+(x0-rect->left))*3;
        uint16_t *d=job->dst+(job->oy+y)*job->stride+job->ox+x0;
        for(int x=x0;x<=x1;x++,p+=3)*d++=(uint16_t)(((p[0]>>3)<<11)|((p[1]>>2)<<5)|(p[2]>>3));
    }
    return 1;
}
static int platform_decode(void *ctx,const uint8_t *jpeg,size_t size,uint16_t *dst,int stride,int w,int h) {
    (void)ctx;
    pj_jpeg_job job={jpeg,size,0,dst,stride,w,h,0,0};
    JDEC jd;
    if(jd_prepare(&jd,jpeg_in,bufs->jpeg_work,sizeof(bufs->jpeg_work),&job)!=JDR_OK)return -1;
    BYTE scale=0;
    while(scale<3&&((int)(jd.width>>scale)>w||(int)(jd.height>>scale)>h))scale++;
    int iw=(int)(jd.width>>scale),ih=(int)(jd.height>>scale);
    job.ox=(w-iw)/2;job.oy=(h-ih)/2;
    if(iw<w||ih<h)for(int y=0;y<h;y++)memset(dst+y*stride,0,(size_t)w*2); /* black bars */
    int64_t t0=esp_timer_get_time();
    int ok=jd_decomp(&jd,jpeg_out,scale)==JDR_OK;
    uint32_t us=(uint32_t)(esp_timer_get_time()-t0);
    decode_us_sum+=us;decodes++;if(us>decode_us_max)decode_us_max=us;
    return ok?0:-1;
}

/* The Pocket link's artwork: the same ROM TJpgDec fit, but only rows [row0, row0+rows) of the fitted w x h picture
 * are written (dst[0] is row row0), so a 320x120 backdrop needs a 4 KB window, not 75 KB. */
typedef struct {const uint8_t *data;size_t size,at;uint16_t *dst;int stride,w,h,ox,oy,row0,rows;} pj_window_job;
static UINT jpeg_in_window(JDEC *jd,BYTE *buf,UINT n) {
    pj_window_job *job=jd->device;
    size_t left=job->size-job->at;if(n>left)n=(UINT)left;
    if(buf)memcpy(buf,job->data+job->at,n);
    job->at+=n;return n;
}
static UINT jpeg_out_window(JDEC *jd,void *bitmap,JRECT *rect) {
    pj_window_job *job=jd->device;const uint8_t *rgb=bitmap;
    int rw=rect->right-rect->left+1,x0=rect->left,x1=rect->right,y0=rect->top,y1=rect->bottom;
    if(job->ox+x0<0)x0=-job->ox;
    if(job->ox+x1>=job->w)x1=job->w-1-job->ox;
    if(job->oy+y0<job->row0)y0=job->row0-job->oy;                       /* only the window's rows */
    if(job->oy+y1>=job->row0+job->rows)y1=job->row0+job->rows-1-job->oy;
    for(int y=y0;y<=y1;y++) {
        const uint8_t *p=rgb+((y-rect->top)*rw+(x0-rect->left))*3;
        uint16_t *d=job->dst+(job->oy+y-job->row0)*job->stride+job->ox+x0;
        for(int x=x0;x<=x1;x++,p+=3)*d++=(uint16_t)(((p[0]>>3)<<11)|((p[1]>>2)<<5)|(p[2]>>3));
    }
    return 1;
}
static int plink_decode(void *ctx,const uint8_t *jpeg,size_t size,uint16_t *dst,int stride,int w,int h,int row0,int rows) {
    (void)ctx;
    if(!bufs||rows<=0||row0<0||row0+rows>h)return -1;
    pj_window_job job={jpeg,size,0,dst,stride,w,h,0,0,row0,rows};
    JDEC jd;
    if(jd_prepare(&jd,jpeg_in_window,bufs->jpeg_work,sizeof(bufs->jpeg_work),&job)!=JDR_OK)return -1;
    BYTE scale=0;
    while(scale<3&&((int)(jd.width>>scale)>w||(int)(jd.height>>scale)>h))scale++;
    int iw=(int)(jd.width>>scale),ih=(int)(jd.height>>scale);
    job.ox=(w-iw)/2;job.oy=(h-ih)/2;
    for(int y=0;y<rows;y++)memset(dst+y*stride,0,(size_t)w*2); /* black bars (and the rows the picture does not cover) */
    return jd_decomp(&jd,jpeg_out_window,scale)==JDR_OK?0:-1;
}

/* ---------- sound: ADPCM ring -> decoder -> I2S DMA ---------- */
static volatile uint32_t audio_stack_free; /* sound task's own stack mark, for the diagnostics */
static bool IRAM_ATTR audio_on_sent(i2s_chan_handle_t h,i2s_event_data_t *e,void *ctx) {
    (void)h;(void)e;(void)ctx;audio_eofs++;return false;
}
static bool IRAM_ATTR audio_on_ovf(i2s_chan_handle_t h,i2s_event_data_t *e,void *ctx) {
    (void)h;(void)e;(void)ctx;
    /* Nothing of ours is left in the DMA: everything written has played (zeros from here). */
    audio_eof_base=audio_eofs-audio_total/PJ_JF_AUDIO_DMA_FRAMES;
    audio_dry_now=1;audio_dry_events++;return false;
}
/* Frames of ours the DMA still holds ahead of the speaker (0 while it plays zeros). */
static uint32_t audio_in_dma(void) {
    uint32_t total=audio_total,played=(audio_eofs-audio_eof_base)*PJ_JF_AUDIO_DMA_FRAMES;
    uint32_t fill=total>played?total-played:0;
    return fill>audio_dma_frames?audio_dma_frames:fill;
}
static void audio_task(void *unused) {
    (void)unused;
    jf_ima ima;jf_ima_init(&ima);
    uint8_t piece[PJ_JF_AUDIO_PIECE];
    int16_t mono[2*PJ_JF_AUDIO_PIECE+1];          /* jf_ima_feed: a header byte gives 0-1 samples, a data byte 2 */
    int16_t pcm[2*(2*PJ_JF_AUDIO_PIECE+1)];       /* decoded samples, each in both slots */
    uint32_t wait_from=0;int dry_seen=0;
    while(!audio_quit) {
        audio_stack_free=(uint32_t)uxTaskGetStackHighWaterMark(NULL);
        audio_idle=0;__sync_synchronize();
        if(!audio_armed||audio_paused_flag) { /* no channel to play into (before the first block, after a stop), or paused */
            audio_idle=1;__sync_synchronize();
            vTaskDelay(pdMS_TO_TICKS(20));
            wait_from=0;audio_dry_now=0;dry_seen=0; /* zeros meanwhile are nobody's underrun */
            continue;
        }
        uint8_t hdr[2];
        /* Wait for sound (one tick at most): the DMA keeps playing what it holds, and if it runs out the driver's
         * auto-clear plays zeros and the ISR says so (audio_dry_now); no silence is ever written ahead of sound. */
        if(xStreamBufferReceive(audio_ring,hdr,2,pdMS_TO_TICKS(10))!=2) {
            uint32_t now=(uint32_t)(esp_timer_get_time()/1000);
            if(!wait_from)wait_from=now?now:1;
            else if(now-wait_from>diag_wait_ms)diag_wait_ms=now-wait_from;
            if(audio_dry_now&&!dry_seen){dry_seen=1;diag_dry_ring++;audio_underruns++;} /* dry with nothing to play: the feed starved */
            continue;
        }
        wait_from=0;diag_ring_sample();
        if(audio_dry_now&&!dry_seen){dry_seen=1;diag_dry_task++;audio_underruns++;} /* dry while sound was queued: this task was late */
        size_t len=hdr[0]|((hdr[1]&0x0f)<<8);uint8_t gen=(hdr[1]>>4)&7;int start=hdr[1]&0x80;
        if(gen!=audio_seen_gen){audio_written=0;__sync_synchronize();audio_seen_gen=gen;jf_ima_init(&ima);} /* a new segment */
        int keep=gen==audio_gen;
        if(keep&&start)jf_ima_block_start(&ima);
        while(len&&!audio_quit) { /* the body is there: header and body went in with one send */
            size_t take=len<sizeof(piece)?len:sizeof(piece);
            size_t got=xStreamBufferReceive(audio_ring,piece,take,0);
            if(!got)break;
            len-=got;
            if(!keep)continue; /* from before a flush: dropped */
            size_t n=jf_ima_feed(&ima,piece,got,mono,sizeof(mono)/sizeof(mono[0]));
            for(size_t i=0;i<n;i++){pcm[2*i]=mono[i];pcm[2*i+1]=mono[i];}
            if(!n)continue;
            size_t written=0;int64_t t0=esp_timer_get_time();
            if(!audio_total)audio_eof_base=audio_eofs; /* the DMA's clock starts counting against us with the first frames */
            if(i2s_channel_write(audio_tx,pcm,n*4,&written,200)==ESP_OK&&written) {
                audio_total+=(uint32_t)(written/4);audio_written+=(uint32_t)(written/4);
                audio_dry_now=0;dry_seen=0;
            } else vTaskDelay(1); /* an error or nothing taken: the channel is going; yield instead of spinning */
            uint32_t us=(uint32_t)(esp_timer_get_time()-t0);if(us>diag_i2s_us)diag_i2s_us=us;
        }
    }
    audio_alive=0;audio_release=1;vTaskDelete(NULL);
}
static void audio_channel_close(void) {
    if(audio_tx){i2s_channel_disable(audio_tx);i2s_del_channel(audio_tx);audio_tx=NULL;}
}
static int audio_channel_open(uint32_t rate) {
    i2s_chan_config_t chan=I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0,I2S_ROLE_MASTER);
    chan.dma_desc_num=PJ_JF_AUDIO_DMA_BUFS;chan.dma_frame_num=PJ_JF_AUDIO_DMA_FRAMES;chan.auto_clear=true;
    if(i2s_new_channel(&chan,&audio_tx,NULL)!=ESP_OK){audio_tx=NULL;return -1;}
    /* The DMA's own clock and dry signal (see audio_on_sent / audio_on_ovf); registered before enable, as the driver requires. */
    i2s_event_callbacks_t cbs={.on_recv=NULL,.on_recv_q_ovf=NULL,.on_sent=audio_on_sent,.on_send_q_ovf=audio_on_ovf};
    audio_eofs=audio_eof_base=audio_total=0;audio_dry_now=0;diag_eofs_mark=0;
    if(i2s_channel_register_event_callback(audio_tx,&cbs,NULL)!=ESP_OK){audio_channel_close();return -1;}
    i2s_std_config_t std={.clk_cfg=I2S_STD_CLK_DEFAULT_CONFIG(rate),
        .slot_cfg=I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,I2S_SLOT_MODE_STEREO),
        .gpio_cfg={.mclk=I2S_GPIO_UNUSED,.bclk=PJ_JF_I2S_BCLK,.ws=PJ_JF_I2S_WS,.dout=PJ_JF_I2S_DOUT,.din=I2S_GPIO_UNUSED,
                   .invert_flags={.mclk_inv=false,.bclk_inv=false,.ws_inv=false}}};
    if(i2s_channel_init_std_mode(audio_tx,&std)!=ESP_OK||i2s_channel_enable(audio_tx)!=ESP_OK){audio_channel_close();return -1;}
    audio_dma_frames=PJ_JF_AUDIO_DMA_BUFS*PJ_JF_AUDIO_DMA_FRAMES;audio_rate_hz=rate;
    return 0;
}
/* Everything back (only once the task is gone: audio_release, or never started). */
static void audio_free(void) {
    audio_channel_close();
    if(audio_ring){vStreamBufferDelete(audio_ring);audio_ring=NULL;}
    audio_handle=NULL;audio_release=0;audio_armed=0;audio_want_channel=0;audio_stack_free=0;
}
/* The ring, the task and the I2S channel, for one video. 0 ready; -1 not now (the last task is still
 * leaving, or no memory): the caller comes back (the next stream open, or the app's next block). */
static int audio_prime(void) {
    if(audio_alive)return audio_quit?-1:0;
    if(audio_release)audio_free();
    if(!audio_ring&&!(audio_ring=xStreamBufferCreate(PJ_JF_AUDIO_RING,1)))return -1;
    audio_quit=0;audio_armed=0;audio_idle=1;audio_paused_flag=0;audio_release=0;audio_alive=1;__sync_synchronize();
    /* Core 1: the main loop (its 40-50 ms picture decodes), the Wi-Fi task and this loop's ISRs are on core 0;
     * the sound task's 8 ms DMA deadline must not queue behind them. Priority above the loop and the network tasks. */
    if(xTaskCreatePinnedToCore(audio_task,"jf-sound",PJ_JF_AUDIO_STACK,NULL,3,&audio_handle,1)!=pdPASS) {
        audio_alive=0;audio_handle=NULL;
        vStreamBufferDelete(audio_ring);audio_ring=NULL; /* no task will ever drain it: give the ring back */
        return -1;
    }
    /* Open the I2S channel now, before the stream's first-second burst, so the DMA is primed when the
     * first audio block arrives (opening it at the first block allocated ~2.3 KB at the heap's worst
     * moment). It is kept until the sound stops or the sink is declared dead (audio_want_channel). */
    audio_want_channel=1;
    audio_channel_open(PJ_JF_AUDIO_RATE); /* a failure is harmless: platform_audio_start opens it on demand */
    return 0;
}
/* Main loop, every tick: the channel goes back once it is no longer wanted and the task is provably
 * not writing (a stop, or a dead sink); a finished task's ring and channel go back. */
static void audio_service(void) {
    if(audio_release)audio_free();
    else if(audio_tx&&!audio_want_channel&&audio_idle)audio_channel_close();
}
static void audio_teardown(void){audio_armed=0;audio_want_channel=0;audio_quit=1;__sync_synchronize();} /* the task leaves; audio_service frees */
static int platform_audio_start(void *ctx,uint32_t rate) {
    (void)ctx;
    if(!sound_fpga)return -1;
    if(audio_prime()!=0)return -1;        /* asked again at the next block (jf_app.h JF_AUDIO_TRY_MS) */
    if(audio_tx&&audio_rate_hz!=rate) {   /* the pre-opened channel is the wrong rate: reopen it */
        if(!audio_idle)return -1;         /* only while the task is provably not writing, armed or not (1.2.6 review, note 3) */
        audio_channel_close();
    }
    if(!audio_tx&&audio_channel_open(rate)!=0)return -1;
    audio_want_channel=1;
    audio_gen=audio_seen_gen=0;audio_written=0;audio_underruns=0;audio_paused_flag=0;__sync_synchronize();
    diag_ring_min=PJ_JF_AUDIO_RING;diag_ring_max=0;diag_gap_ms=diag_wait_ms=diag_i2s_us=diag_dry_ring=diag_dry_task=0;
    audio_last_write_ms=0;diag_eofs_mark=audio_eofs;
    audio_armed=1;
    return 0;
}
static size_t platform_audio_write(void *ctx,const uint8_t *adpcm,size_t size,int block_start) {
    (void)ctx;
    if(!audio_ring||!audio_armed)return size; /* no sink: swallowed */
    diag_ring_sample();
    size_t space=xStreamBufferSpacesAvailable(audio_ring);
    if(space<3)return 0;
    if(size>space-2)size=space-2;
    if(size>PJ_JF_AUDIO_MSG)size=PJ_JF_AUDIO_MSG;
    uint8_t msg[2+PJ_JF_AUDIO_MSG];
    msg[0]=(uint8_t)size;msg[1]=(uint8_t)((size>>8)|((audio_gen&7)<<4)|(block_start?0x80:0));
    memcpy(msg+2,adpcm,size);
    /* One send, one writer (the main loop), space checked: the task never finds a header whose body
     * is still on its way (two sends used to leave that gap; a 20 ms wait on it could lose framing). */
    if(xStreamBufferSend(audio_ring,msg,2+size,0)!=2+size)return 0;
    uint32_t now=(uint32_t)(esp_timer_get_time()/1000);
    if(audio_last_write_ms&&now-audio_last_write_ms>diag_gap_ms)diag_gap_ms=now-audio_last_write_ms;
    audio_last_write_ms=now?now:1;
    return size;
}
/* Frames out of the speaker: handed to the DMA minus what the DMA still holds (from its own EOF count,
 * so a dry spell reads as fully played instead of a fixed 32 ms behind). */
static uint32_t platform_audio_played(void *ctx) {
    (void)ctx;
    if(audio_seen_gen!=audio_gen)return 0; /* the new segment has not reached the task yet */
    uint32_t w=audio_written,held=audio_in_dma();return w>held?w-held:0;
}
static void platform_audio_pause(void *ctx,int paused){(void)ctx;audio_paused_flag=paused;}
static void platform_audio_flush(void *ctx){(void)ctx;audio_gen=(uint8_t)((audio_gen+1)&7);}
/* The channel goes back from audio_service once the task has seen this (a seek keeps playing and never
 * calls this; the app calls it when the sink is declared dead and at the video's end); the task and
 * ring stay for the video (a start within the same video reuses them). */
static void platform_audio_stop(void *ctx){(void)ctx;audio_want_channel=0;audio_armed=0;__sync_synchronize();}

/* ---------- lifecycle ---------- */
static void release(void) {
    if(!audio_alive)audio_free();
    if(plink){memset(plink,0,sizeof(*plink));heap_caps_free(plink);plink=NULL;} /* 1.3.0: the link gateway, wiped */
    if(plink_buf){memset(plink_buf,0,PJ_PLINK_BUF);heap_caps_free(plink_buf);plink_buf=NULL;} /* 1.3.7: and its pieces */
    if(plink_response){memset(plink_response,0,PJ_PLINK_RESPONSE);heap_caps_free(plink_response);plink_response=NULL;}
    if(plink_scratch){memset(plink_scratch,0,sizeof(*plink_scratch));heap_caps_free(plink_scratch);plink_scratch=NULL;}
    if(req_go){vSemaphoreDelete(req_go);req_go=NULL;}
    if(stream_sb){vStreamBufferDelete(stream_sb);stream_sb=NULL;}
    if(stream_lock){vSemaphoreDelete(stream_lock);stream_lock=NULL;}
    if(bufs){memset(bufs,0,sizeof(*bufs));heap_caps_free(bufs);bufs=NULL;}
    if(art){heap_caps_free(art);art=NULL;}
    art_size=0;art_wanted=0;http_handle=stream_handle=NULL;
    if(app){memset(app,0,sizeof(*app));heap_caps_free(app);app=NULL;}
}
static uint8_t jf_pocket_link; /* 1.3.0: the FPGA has the link bridge: the app's menu offers Pocket link */
void pj_jf_set_pocket_link(int on){jf_pocket_link=(uint8_t)(on!=0);}
int pj_jf_begin(const pj_jf_creds *c,uint16_t *fb,uint32_t now,int sound) {
    if(closing)return -6; /* the previous session is still letting go */
    if(app||bufs||plink||!c||!c->url[0])return -1;
    sound_fpga=sound;
    const uint32_t caps=MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL;
    app=heap_caps_malloc(sizeof(jf_app),caps);bufs=heap_caps_calloc(1,sizeof(pj_jf_buffers),caps);
    req_go=xSemaphoreCreateBinary();stream_lock=xSemaphoreCreateMutex(); /* the stream buffer comes with a video */
    if(!app||!bufs||!req_go||!stream_lock){release();return -2;}
    snprintf(bufs->base_url,sizeof(bufs->base_url),"%s",c->url);
    snprintf(bufs->auth,sizeof(bufs->auth),"MediaBrowser Client=\"Chromatic Jellyfin\", Device=\"Chromatic\", DeviceId=\"%s\", "
             "Version=\"%s\", Token=\"%s\"",c->device,c->version,c->token);
    tasks_stop=0;req_busy=0;req_done=0;stream_state=0;http_alive=stream_alive=1;
    if(xTaskCreate(http_task,"jf-http",PJ_JF_STACK,NULL,1,&http_handle)!=pdPASS){http_alive=stream_alive=0;release();return -4;}
    if(xTaskCreate(stream_task,"jf-stream",PJ_JF_STACK,NULL,1,&stream_handle)!=pdPASS) {
        stream_alive=0;tasks_stop=1;closing=1; /* pj_jf_poll frees everything once the http task is gone */
        return -4;
    }
    jf_platform io={NULL,platform_request,platform_stream_open,platform_stream_close,platform_stream_read,platform_decode,
                    platform_audio_start,platform_audio_write,platform_audio_played,platform_audio_pause,platform_audio_flush,platform_audio_stop};
    jf_config cfg;memset(&cfg,0,sizeof(cfg));
    cfg.audio=(uint8_t)(sound!=0); /* stock FPGA: the MP3 request and skipped audio, exactly as before */
    snprintf(cfg.server_name,sizeof(cfg.server_name),"Jellyfin");
    snprintf(cfg.user,sizeof(cfg.user),"%s",c->user);snprintf(cfg.device,sizeof(cfg.device),"%s",c->device);
    cfg.fb=fb;cfg.scratch=fb;cfg.scratch_bytes=160u*144u*2u;cfg.art=NULL;cfg.art_cap=0;
    cfg.font_alpha=pj_font_alpha;cfg.font_advance=pj_font_advance;
    cfg.pocket_link=jf_pocket_link;
    jf_app_start(app,&io,&cfg,now);
    art_tried_at=now;art_claim(); /* artwork gets what is left above the reserve (largest block, so it really fits) */
    return 0;
}
int pj_jf_active(void){return app!=NULL&&!closing;}
/* Something else drew over the frame (Remote Pilot pairing, the service screen): draw it all again. */
void pj_jf_invalidate(void){if(app&&!closing)app->redraw=1;}
int pj_jf_wants_exit(void){return app&&!closing&&app->screen==JF_S_EXIT;}
int pj_jf_exit_to_setup(void){return app&&!closing&&app->screen==JF_S_EXIT&&app->exit_to==JF_EXIT_SETUP;}
int pj_jf_exit_to_find(void){return app&&!closing&&app->screen==JF_S_EXIT&&app->exit_to==JF_EXIT_FIND;}
int pj_jf_exit_to_plink(void){return app&&!closing&&app->screen==JF_S_EXIT&&app->exit_to==JF_EXIT_PLINK;}
void pj_jf_link(int up){if(app&&!closing)jf_app_set_link(app,up);}
void pj_jf_press(uint16_t pressed){if(app)jf_app_press(app,pressed);}
/* Returns 1 when the frame changed and should be sent to the overlay. */
int pj_jf_tick(uint16_t *fb,uint32_t now) {
    if(!app||closing)return 0;
    if(req_done){int status=req_status;req_done=0;jf_app_reply(app,status);}
    if(audio_alive&&!audio_quit&&!app->playing)audio_teardown(); /* the video ended: the sound task leaves */
    audio_service(); /* a stopped channel, or a finished task's ring and channel, go back */
    if(stream_sb&&!app->playing) { /* the video ended: its buffer goes back, the artwork takes its place */
        xSemaphoreTake(stream_lock,portMAX_DELAY);stream_generation++;stream_state=0;
        vStreamBufferDelete(stream_sb);stream_sb=NULL; /* the stream task checks the generation first */
        xSemaphoreGive(stream_lock);
    }
    if(!art&&!stream_sb&&!app->playing&&app->request==JF_R_NONE&&(art_wanted||(uint32_t)(now-art_tried_at)>=3000u)) {
        art_tried_at=now;art_claim(); /* the video ended, or there was no room before: pictures again */
    }
    if(!jf_app_tick(app,now))return 0;
    jf_app_draw(app,fb);
    return 1;
}
void pj_jf_status(pj_jf_status_t *s) {
    memset(s,0,sizeof(*s));
    if(!app)return;
    s->screen=app->screen;s->playing=app->playing;s->shown=app->shown;s->dropped=app->dropped;
    s->bitrate=app->bitrate;s->position=app->playing?jf_position_s(app):0;s->art=app->art_cap;
    s->largest=(uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL);
    /* stack words never used, for sizing the tasks on the console (0 once a task has gone) */
    s->http_stack=http_alive&&http_handle?(uint32_t)uxTaskGetStackHighWaterMark(http_handle):0;
    s->stream_stack=stream_alive&&stream_handle?(uint32_t)uxTaskGetStackHighWaterMark(stream_handle):0;
    s->net_bytes=net_bytes;s->decodes=decodes;
    s->decode_ms_avg=decodes?decode_us_sum/decodes/1000u:0;s->decode_ms_max=decode_us_max/1000u;
    s->fps_cap=app->fps_cap;s->cycle_ms=app->cycle_ms;s->rebased=app->rebased;s->slowed=app->slowed;
    s->sound=!app->audio_on?0:app->audio_dead?3:app->audio_clock?1:2;
    s->sound_played=app->audio_played;s->sound_underruns=audio_underruns;
    s->sound_stack=audio_stack_free; /* written by the sound task itself */
    s->sound_starved=app->starved;s->sound_catchup=app->audio_catchup;s->sound_rate_cap=app->rate_cap;
    /* The sound path's own figures since the last line (see the diag_ variables). */
    s->sound_ring_min=audio_ring?diag_ring_min:0;s->sound_ring_max=diag_ring_max;
    s->sound_gap_ms=diag_gap_ms;s->sound_wait_ms=diag_wait_ms;s->sound_i2s_ms=(diag_i2s_us+999u)/1000u;
    s->sound_dry_ring=diag_dry_ring;s->sound_dry_task=diag_dry_task;
    uint32_t eofs=audio_eofs;s->sound_eofs=audio_tx?eofs-diag_eofs_mark:0;diag_eofs_mark=eofs;
    s->sound_in_dma=audio_tx?audio_in_dma():0;
    diag_ring_min=PJ_JF_AUDIO_RING;diag_ring_max=0;diag_gap_ms=diag_wait_ms=diag_i2s_us=diag_dry_ring=diag_dry_task=0;
    decode_us_sum=decode_us_max=decodes=0; /* each line covers the time since the last */
}
int pj_jf_playing(void){return app&&!closing&&app->playing;}
/* 1.3.2: the audio/subtitle choice of the stream now playing, as a diagnostics line (counts and codes only, never a
 * title or a track name); returns a number that changes with every new choice (0: none yet). */
uint32_t pj_jf_tracks(char *out,size_t size) {
    if(!app||closing||!app->tracks_seq||!app->playing)return 0;
    jf_tracks_line(app,out,size);
    return app->tracks_seq;
}
/* Leaving never waits here (the main loop must keep feeding the watchdog): stop the stream,
 * let a queued Stopped report go out, and free everything from pj_jf_poll once both tasks exit. */
void pj_jf_end(void) {
    if(!app||closing)return;
    jf_app_close(app);
    if(stream_lock)platform_stream_close(NULL);
    audio_teardown(); /* jf_app_close stopped the sound too; the task leaves within one DMA write */
    closing=1;tasks_stop=1;
}
/* ---------- the Pocket link (1.3.0) ---------- */
static pj_plink_rx jl_rx;           /* the second UART1 frame parser (status packets, address 0x0A) */
static pj_pld_printer jl_said;      /* 1.3.6: which link diagnostics lines have been said, and when */
static char jl_user[33];
static uint32_t jl_failed_at;static uint8_t jl_failed;
static int jl_request(void *ctx,const char *path){return platform_request(ctx,"GET",path,NULL);}
/* 1.3.6: what waits for the wire, counted here. The driver's own figure
 * (uart_get_tx_buffer_free_size, IDF 5.3 uart.c: tx_buf_size - tx_len_tot) knows only the one packet it is moving
 * into the FIFO, not the packets queued behind it in its ring, so it could neither say that the ring is empty nor how
 * much the bridge is still to be sent. The line has no flow control and never pauses while bytes wait, so every byte
 * the gateway queues takes ten bit times: jl_line_until is when the last of them has left the pin. All of it on the
 * launcher's one loop task. */
static int64_t jl_line_until;static uint32_t jl_line_baud=115200u;
static size_t jl_line_pending(void) {
    int64_t left=jl_line_until-esp_timer_get_time();
    return left>0?(size_t)((left*(int64_t)jl_line_baud+9999999)/10000000):0;
}
static void jl_line_add(size_t bytes) {
    int64_t now=esp_timer_get_time();
    if(jl_line_until<now)jl_line_until=now;
    jl_line_until+=((int64_t)bytes*10000000+(int64_t)jl_line_baud-1)/(int64_t)jl_line_baud;
}
/* What the driver can see: the packet it is moving and the hardware FIFO (this also sees the launcher's own FPGA
 * messages, which do not come through the gateway). Unknown: the whole ring. */
static size_t jl_driver_pending(void) {
    size_t room=0;
    if(uart_get_tx_buffer_free_size(UART_NUM_1,&room)!=ESP_OK)return PJ_JL_TX_RING+UART_LL_FIFO_DEF_LEN;
    size_t ring=room<PJ_JL_TX_RING?PJ_JL_TX_RING-room:0,fifo_free=uart_ll_get_txfifo_len(&UART1);
    return ring+(fifo_free<UART_LL_FIFO_DEF_LEN?UART_LL_FIFO_DEF_LEN-fifo_free:0);
}
/* 1.3.1: bytes queued for the FPGA but not on the wire yet, for the credit; 1.3.6: the larger of the two
 * counts above, and 0 only when the ring and the FIFO are empty (the fast rate is asked for and taken only then). */
static size_t jl_tx_pending(void *ctx) {
    (void)ctx;size_t driver=jl_driver_pending(),line=jl_line_pending();
    return driver>line?driver:line;
}
/* One whole 0x8F packet into the UART1 driver's transmit ring, or nothing (offered again next loop): never blocks.
 * 1.3.6: by the count above, with at most PJ_JL_TX_ROOM bytes waiting (the driver's figure let packets through
 * until uart_write_bytes waited for room in its ring). */
static int jl_fpga_send(void *ctx,const uint8_t *packet,size_t size) {
    (void)ctx;
    if(jl_tx_pending(NULL)+size>PJ_JL_TX_ROOM)return 0;
    if(uart_write_bytes(UART_NUM_1,(const char *)packet,size)!=(int)size)return 0;
    jl_line_add(size);return 1;
}
/* 1.3.6: the line is empty: nothing in the driver's ring or the FIFO, the last byte gone from the
 * shifter. Bounded by PJ_JL_SURE_MS a wait; 0 when it did not empty (the UART is stuck).
 * Note: "FIFO empty and tx_len_tot 0 means the ring is empty" holds only because the UART1 interrupt and this
 * task run on the same core. Between two packets the driver's tx_len_tot is 0 while its ring still holds the next
 * one; it is the interrupt that moves it into the FIFO (IDF 5.3 uart.c, UART_INTR_TXFIFO_EMPTY), and on one core a
 * pending interrupt has always run before this task's next instruction, so the task never looks in between. Both are
 * on CPU0 today: the interrupt is allocated on the core that calls uart_driver_install, which is app_main
 * (launcher_main.c buses_init), the main task, pinned by CONFIG_ESP_MAIN_TASK_AFFINITY_CPU0=y; the launcher's loop is
 * that same task. If the driver is ever installed from another task, or the loop moves to the other core, this must
 * be checked again: across cores the look could fall before or inside the interrupt's pass and report a ring that
 * still holds packets as empty. 1.4.0: the line model then says "empty since now" (was 0, long ago), so jl_idle_us
 * never takes a line that has only just emptied for one idle long before. */
static int jl_drained(void) {
    for(unsigned turn=0;turn<3;turn++) {
        if(uart_wait_tx_done(UART_NUM_1,pdMS_TO_TICKS(PJ_JL_SURE_MS))!=ESP_OK)return 0;
        if(!jl_driver_pending()){jl_line_until=esp_timer_get_time();return 1;}
    }
    return 0;
}
/* 1.3.1: the leave must not be dropped: the line drains, the packet is queued, the line drains again.
 * 1.3.6: when the line does not drain first (a stuck UART only) the leave is NOT written at all and 0 is
 * returned. Left that way on purpose: bytes behind a ring that does not move would not reach the bridge either, and
 * uart_write_bytes waits without limit for room in a full ring, which would stop the loop that feeds the watchdog.
 * The bridge ends media mode by itself 3 s after the last heartbeat (and is back at 115,200 250 ms after an
 * unconfirmed rate change). It is said once in the log: jl_leave_unsent, read by pj_jl_trace. */
static uint8_t jl_leave_unsent;
static int jl_send_sure(void *ctx,const uint8_t *packet,size_t size) {
    (void)ctx;
    if(!jl_drained()){if(jl_leave_unsent<255)jl_leave_unsent++;return 0;}
    if(uart_write_bytes(UART_NUM_1,(const char *)packet,size)!=(int)size)return 0;
    jl_line_add(size);
    return jl_drained();
}
/* The one place the UART's rate is set. What still waits (only ever on the way back to 115,200, after a wait that
 * ran out) is counted at the new rate. */
static void jl_rate(uint32_t baud) {
    size_t left=jl_line_pending();
    uart_set_baudrate(UART_NUM_1,baud);jl_line_baud=baud;jl_line_until=esp_timer_get_time();jl_line_add(left);
}
/* The keyed fast UART: changed only once what was queued at the old rate has gone out. 0 = the stock 115,200.
 * 1.3.6: to the fast rate only with the line proven empty (was: a 20 ms wait whose result was not
 * looked at); if it is not, the rate stays at 115,200 and the gateway finds out, because the bridge never confirms
 * (PJ_PF_OFF, 500 ms later). The gateway asks only with its ring empty, so this is a UART that is stuck. Back to
 * 115,200 always, after the same wait: the stock rate must hold whatever happened. */
static void jl_set_baud(void *ctx,int fast) {
    (void)ctx;
    if(!jl_drained()&&fast)return;
    jl_rate(fast?PJ_TETHER_UART_FAST_BAUD:115200u);
}
static const pj_plink_ops jl_ops={NULL,jl_request,plink_decode,jl_fpga_send,
                                  platform_stream_open,platform_stream_read,platform_stream_close,jl_set_baud};
/* 1.4.0: the clock for "plink speed", and how long the line has been idle (pocket_link.h PJ_PLINK_IDLE_BYTES): 0 while
 * the driver holds anything or the line model says bytes are still on their way out. */
static uint32_t jl_micros(void *ctx){(void)ctx;return (uint32_t)esp_timer_get_time();}
static uint32_t jl_idle_us(void *ctx) {
    (void)ctx;
    if(jl_driver_pending()||jl_line_pending())return 0;
    int64_t idle=esp_timer_get_time()-jl_line_until;
    return idle<=0?0u:idle>0x7fffffff?0x7fffffffu:(uint32_t)idle;
}
static const pj_plink_extra jl_extra={jl_send_sure,jl_tx_pending,jl_micros,jl_idle_us};
int pj_jl_begin(const pj_jf_creds *c,uint32_t now) {
    if(closing)return -6;
    if(app||bufs||plink||!c||!c->url[0]||!c->user[0])return -1; /* plink's pieces are taken and freed with it */
    const uint32_t caps=MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL;
    /* Refused before anything is taken when the heap could not keep its reserve with the gateway in it. 1.3.7: the
     * gateway is four pieces (PJ_PLINK_MEMORY, the same total as the one block of 1.3.6), so the largest free block
     * must hold the largest piece, not the whole (1.3.6 refused with 90 KB free: no block above 36,864 B). */
    size_t need=PJ_PLINK_MEMORY+sizeof(pj_jf_buffers)+2u*PJ_JF_STACK+PJ_JL_STREAM_BUFFER+PJ_JL_RESERVE;
    if(esp_get_free_heap_size()<need||heap_caps_get_largest_free_block(caps)<pj_plink_piece_max())return -5;
    sound_fpga=0; /* the console plays no sound in link mode */
    plink_buf=heap_caps_calloc(1,PJ_PLINK_BUF,caps);plink=heap_caps_calloc(1,sizeof(pj_plink),caps); /* largest first */
    plink_response=heap_caps_calloc(1,PJ_PLINK_RESPONSE,caps);plink_scratch=heap_caps_calloc(1,sizeof(pj_plink_scratch),caps);
    bufs=heap_caps_calloc(1,sizeof(pj_jf_buffers),caps);
    req_go=xSemaphoreCreateBinary();stream_lock=xSemaphoreCreateMutex();
    if(!plink||!plink_buf||!plink_response||!plink_scratch||!bufs||!req_go||!stream_lock){release();return -2;} /* any piece missing: all freed */
    plink->buf=plink_buf;plink->response=plink_response;plink->scratch=plink_scratch; /* kept by pj_plink_init */
    snprintf(bufs->base_url,sizeof(bufs->base_url),"%s",c->url);
    snprintf(bufs->auth,sizeof(bufs->auth),"MediaBrowser Client=\"Chromatic Jellyfin\", Device=\"Chromatic\", DeviceId=\"%s\", "
             "Version=\"%s\", Token=\"%s\"",c->device,c->version,c->token);
    snprintf(jl_user,sizeof(jl_user),"%s",c->user);
    tasks_stop=0;req_busy=0;req_done=0;stream_state=0;http_alive=stream_alive=1;
    if(xTaskCreate(http_task,"jf-http",PJ_JF_STACK,NULL,1,&http_handle)!=pdPASS){http_alive=stream_alive=0;release();return -4;}
    if(xTaskCreate(stream_task,"jf-stream",PJ_JF_STACK,NULL,1,&stream_handle)!=pdPASS){stream_alive=0;tasks_stop=1;closing=1;return -4;}
    memset(&jl_rx,0,sizeof(jl_rx));pj_pld_printer_init(&jl_said,now);jl_failed=0;
    jl_http_new=jl_http_reused=jl_open_heap=0;jl_low=UINT32_MAX;jl_open_tries=0;jl_open_status=0;
    if(!pj_plink_init(plink,jl_ops,jl_user,now)){jl_failed=1;jl_failed_at=now;} /* UART busy: tried again by pj_jl_tick */
    pj_plink_extras(plink,jl_extra); /* after init (which clears the struct): the sure leave and the ring-aware credit */
    return 0;
}
int pj_jl_active(void){return plink!=NULL&&!closing;}
int pj_jl_rate_changing(void){return plink!=NULL&&!closing&&!jl_failed&&pj_plink_rate_changing(plink);}
/* UART1 bytes as they arrive, beside the launcher's own pj_fpga_rx_byte: the bridge's status packets. */
void pj_jl_rx(const uint8_t *bytes,int n,uint32_t now) {
    if(!plink||closing)return;
    for(int i=0;i<n;i++) {
        unsigned address;const uint8_t *payload;size_t size;
        if(pj_plink_rx_byte(&jl_rx,bytes[i],&address,&payload,&size))pj_plink_fpga_packet(plink,address,payload,size,now);
    }
}
/* Every loop: replies from the HTTP task, the gateway's own step, and a fresh start 3 s after it stopped
 * (only once no server request is still out: the HTTP task writes into the gateway). -1 while stopped. */
int pj_jl_tick(uint32_t now) {
    if(!plink||closing)return 0;
    {uint32_t f=(uint32_t)esp_get_free_heap_size();if(f<jl_low)jl_low=f;} /* 1.3.8: this line's lowest (pj_jl_net) */
    if(req_done){int status=req_status;req_done=0;pj_plink_reply(plink,status);}
    int r=jl_failed?-1:pj_plink_step(plink,now);
    if(r<0&&!jl_failed){jl_failed=1;jl_failed_at=now;}
    if(jl_failed&&!req_busy&&!req_done&&(uint32_t)(now-jl_failed_at)>=PJ_JL_RETRY_MS) {
        pj_plink_stop(plink); /* leave (twice at the fast rate), back to 115,200, stream closed */
        pj_plink_rx_reset(&jl_rx); /* 1.3.6: a new start for the parser; its counts of damaged packets stay */
        if(pj_plink_init(plink,jl_ops,jl_user,now))jl_failed=0;else jl_failed_at=now;
        pj_plink_extras(plink,jl_extra);
    }
    if(stream_sb&&plink->stream_state==PJ_PS_NONE) { /* no title open: its buffer goes back */
        xSemaphoreTake(stream_lock,portMAX_DELAY);stream_generation++;stream_state=0;
        vStreamBufferDelete(stream_sb);stream_sb=NULL;
        xSemaphoreGive(stream_lock);
    }
    return r;
}
void pj_jl_view(pj_plink_status *out,uint32_t now) {
    pj_plink_status none={PJ_PLV_WAITING,0,0,"",0,0,0,0,{PJ_PLH_NOTHING,0,0,0}};
    *out=plink&&!closing?pj_plink_view(plink,now):none;
}
void pj_jl_counts(uint32_t *state,uint32_t *level,uint32_t *faults,uint32_t *fast,uint32_t *stream) {
    *state=plink?plink->state:0;*level=plink?plink->level:0;*faults=plink?plink->fpga_faults:0;
    *fast=plink?plink->fast:0;*stream=plink?plink->stream_state:0;
}
/* 1.3.8: runs in the TCP/IP task (the only one that changes these lists; no core locking in this build). */
static void jl_count_pcbs(void *unused) {
    (void)unused;unsigned tw=0,active=0;
    for(const struct tcp_pcb *p=tcp_tw_pcbs;p&&tw<0xffffu;p=p->next)tw++;
    for(const struct tcp_pcb *p=tcp_active_pcbs;p&&active<0xffffu;p=p->next)active++;
    jl_tw_pcbs=(uint16_t)tw;jl_tcp_pcbs=(uint16_t)active;
}
/* 1.3.8: one line after the 5 s "plink state=" line, where the link's heap goes: "plink net tw=<TIME_WAIT pcbs>x<bytes
 * each> tcp=<other pcbs> http=<connections opened>/<requests on a kept one> low=<lowest free heap since the last line>
 * open-heap=<free heap when the last stream's headers came in>". Counts only. The pcb counts are as of the line before
 * (taken now by the TCP/IP task, for the next line); a full message box just skips one count. */
int pj_jl_net(char *out,size_t size) {
    if(!plink||closing)return 0;
    /* 1.4.0: open=<stream opens tried, all titles>/<the last one's HTTP status, -1 no answer> */
    snprintf(out,size,"plink net tw=%ux%u tcp=%u http=%lu/%lu low=%lu open-heap=%lu open=%lu/%d",(unsigned)jl_tw_pcbs,(unsigned)sizeof(struct tcp_pcb),
             (unsigned)jl_tcp_pcbs,(unsigned long)jl_http_new,(unsigned long)jl_http_reused,
             (unsigned long)(jl_low==UINT32_MAX?0u:jl_low),(unsigned long)jl_open_heap,(unsigned long)jl_open_tries,jl_open_status);
    jl_low=UINT32_MAX;
    tcpip_try_callback(jl_count_pcbs,NULL);
    return 1;
}
/* 1.4.0: while a title plays, after the 5 s "plink net" line, the two "plink speed" lines of pocket_link.h
 * pj_plink_say_speed: which 0 the throughput one, which 1 (only right after it) the pass/fit/credit one with what went
 * wrong on UART1 since the last such line (status packets with a bad CRC, other packets with a bad CRC or length,
 * hardware FIFO overflows, full receive ring, passes that found the event queue full, framing errors). Counts only.
 * 0: nothing said (the counts restart then too). */
int pj_jl_speed(char *out,size_t size,uint32_t now,const pj_pld_uart *u,int which) {
    static pj_pld_uart was;static int due;
    if(!plink||closing||!u)return 0;
    pj_pld_uart d=*u,diff;memset(&diff,0,sizeof(diff));d.pkt_crc=jl_rx.crc_status;d.pkt_other=jl_rx.crc_other+jl_rx.bad_len;
    if(!which){due=pj_plink_say_speed(plink,now,0,NULL,out,size);if(!due)was=d;return due;}
    if(!due)return 0;
    due=0;
    if(d.pkt_crc<was.pkt_crc||d.pkt_other<was.pkt_other||d.ovf<was.ovf||d.full<was.full||d.evq<was.evq||d.frame<was.frame)
        memset(&was,0,sizeof(was)); /* a new link session cleared the counts */
    diff.pkt_crc=d.pkt_crc-was.pkt_crc;diff.pkt_other=d.pkt_other-was.pkt_other;diff.ovf=d.ovf-was.ovf;
    diff.full=d.full-was.full;diff.evq=d.evq-was.evq;diff.frame=d.frame-was.frame;
    was=d;return pj_plink_say_speed(plink,now,1,&diff,out,size);
}
/* 1.4.0: the end of the launcher's pass (UART1 read again just before): downstream packets the newest bridge statuses
 * allow (pocket_link.h pj_plink_pump). Nothing while the link is stopped or its rate is changing. */
void pj_jl_pump(uint32_t now) {
    if(!plink||closing||jl_failed||pj_plink_rate_changing(plink))return;
    pj_plink_pump(plink,now);
}
/* 1.4.0: a title plays over the link (launcher_main.c throttles Remote Pilot's live frames then, as for the app). */
int pj_jl_streaming(void){return plink!=NULL&&!closing&&plink->stream_state==PJ_PS_OPEN;}
/* 1.3.5: the link's progress through its stages, one line per change (docs/Design-notes.md "Launcher
 * 1.3.5"): stage (1 entering, 2 fast UART, 3 waiting for the Pocket, 4 linked, 5 streaming), the module state, the
 * rate state, what the bridge last reported (mode, faults, status packets seen), and the reason as one token. So a
 * failure on the console says in which stage and on what evidence, instead of "stopped". */
int pj_jl_trace(char *out,size_t size) {
    static uint32_t last_key=0xffffffffu;static const char *last_why;
    if(jl_leave_unsent) { /* 1.3.6: one line for the leaves a stuck UART kept back (jl_send_sure), link open or not */
        snprintf(out,size,"plink leave not-sent n=%u uart1 not drained, bridge leaves media mode by its 3 s guard",(unsigned)jl_leave_unsent);
        jl_leave_unsent=0;return 1;
    }
    if(!plink||closing){last_key=0xffffffffu;last_why=NULL;return 0;}
    unsigned stage=pj_plink_stage(plink);
    uint32_t key=(uint32_t)stage|(uint32_t)plink->state<<4|(uint32_t)plink->fast<<8|(uint32_t)plink->fpga_mode<<12|
                 (uint32_t)plink->fpga_faults<<16|(uint32_t)(plink->status_packets!=0)<<20|(uint32_t)plink->pocket_seen<<21|(uint32_t)jl_failed<<22;
    if(key==last_key&&plink->why==last_why)return 0;
    last_key=key;last_why=plink->why;
    char why[32];pj_diag_token(why,sizeof(why),plink->why&&plink->why[0]?plink->why:"none");
    snprintf(out,size,"plink stage=%u state=%u fast=%u mode=%u faults=%u status-pkts=%lu pocket=%u fast-failed=%u why=%s",
             stage,(unsigned)plink->state,(unsigned)plink->fast,(unsigned)plink->fpga_mode,(unsigned)plink->fpga_faults,
             (unsigned long)plink->status_packets,(unsigned)plink->pocket_seen,(unsigned)plink->fast_failed,why);
    return 1;
}
/* 1.3.6: the link diagnostics lines (shared/pocket_link_diag.h; docs/Design-notes.md "Launcher 1.3.6 link
 * diagnostics"). Counts, a code and bytes of the link protocol only. pj_jl_diag_periodic: line `index` of the
 * PJ_PLD_PERIODIC lines that follow the 5 s "plink state=" line, 0 when it has nothing new; `uart` carries the
 * launcher's UART1 event counts, the damaged-packet counts are added here. pj_jl_diag_event: at most one line a call,
 * the moment a capture completes or a whole frame is refused. Both go through pocket_link.h pj_plink_say_*: once the
 * link works (connected, syncing, playing) they say one short line every 30 s and no bytes, so the console never
 * holds up a stream. */
int pj_jl_diag_periodic(char *out,size_t size,uint32_t now,unsigned index,const pj_pld_uart *uart) {
    if(!plink||closing)return 0;
    pj_pld_uart u=*uart;
    u.pkt_crc=jl_rx.crc_status;u.pkt_other=jl_rx.crc_other;u.pkt_len=jl_rx.bad_len;
    return pj_plink_say_periodic(plink,&jl_said,&u,now,index,out,size); /* gated by the link's view */
}
int pj_jl_diag_event(char *out,size_t size,uint32_t now) {
    if(!plink||closing)return 0;
    return pj_plink_say_event(plink,&jl_said,now,out,size);
}
/* Leaves the link mode now (the FPGA hands the cable back to the game core, the UART is at 115,200 again), stops the
 * tasks, and lets pj_jf_poll free everything once they are gone. Never waits on the network. */
void pj_jl_end(void) {
    if(!plink||closing)return;
    pj_plink_stop(plink);
    jl_drained();jl_rate(115200u); /* whatever happened: stock rate (1.3.6: after the line emptied, up to PJ_JL_SURE_MS) */
    if(stream_lock)platform_stream_close(NULL);
    closing=1;tasks_stop=1;
}
int pj_jf_poll(void) {
    if(!closing)return 0;
    if(req_done)req_done=0; /* the report needs no answer */
    if(closing==2) { /* the exited tasks' stacks are freed by the idle task, not by release() */
        if(xTaskGetTickCount()-closed_at<pdMS_TO_TICKS(300))return 1;
        closing=0;return 0;
    }
    if(http_alive||stream_alive||audio_alive)return 1;
    release();closing=2;closed_at=xTaskGetTickCount();return 1; /* release() frees the sound ring and channel too */
}
