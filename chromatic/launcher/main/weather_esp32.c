/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * ESP32 binding of the Local Weather channel (chromatic/weather/wx.h): the clock and the data.
 *
 *  - When it runs. Only while the weather channel is on screen. The launcher ends the TV app
 *    first (its memory goes back to the heap), calls pj_wx_open and then pj_wx_service every
 *    pass, and draws the channel itself from pj_wx_state(). The pages cycle at once with what is
 *    held; a round of requests fills them as replies arrive ("UPDATING" shows meanwhile). On
 *    leaving, pj_wx_close stops the round; the launcher waits for pj_wx_busy()==0 before it
 *    allocates the TV app again.
 *  - What stays. After a close only the cache: the channel's state with its values and each
 *    request's next due time (one heap block, sizeof(pj_wx_cache), about 2.8 KB), so opening
 *    again within a request's period does not ask again. The radar picture does not stay (its
 *    page is 7.8 KB): the radar request is due again on every open. pj_wx_forget gives the cache
 *    back too. Static RAM of this file: five pointers and a few flags.
 *  - Data: the US National Weather Service, https://api.weather.gov (JSON) and its map server
 *    https://opengeo.ncep.noaa.gov (the radar picture, a small GIF), no key. The requests are the
 *    compiled-in table of wx_location.h: every api.weather.gov request first, most valuable
 *    first, the map server last, so a round is two TLS handshakes (one a host; the connection is
 *    kept for consecutive requests) and never two connections at once. A short-lived task
 *    fetches the requests that are due, feeding each reply to its streaming parser in 1 KB
 *    pieces. No reply is ever held; the radar GIF is decoded as it arrives (wx_radar.h). When a
 *    parser has all it wants, the rest of the reply is read on and dropped (up to
 *    PJ_WX_DRAIN_MAX) so that the connection can be used again: reading on is cheaper than a
 *    handshake.
 *    The server's certificate chain and name are checked against the ESP-IDF certificate bundle
 *    (esp_crt_bundle_attach). Certificate DATES are not checked in this configuration
 *    (CONFIG_MBEDTLS_HAVE_TIME_DATE is off, see sdkconfig.defaults).
 *  - Clock: SNTP (esp_netif_sntp, server pool.ntp.org, nothing personal in the request) runs
 *    while the channel is open. No request waits for it: if the clock is unset when the first
 *    reply comes, it is set from that reply's HTTP Date header (the reply came over the checked
 *    connection), and SNTP corrects it when it answers. The system clock starts at 1970 after
 *    every boot (nothing is stored), so "set" means the year is believable.
 *  - Two owners, no lock: the task touches only the round's block (and, while a radar picture
 *    arrives, the radar page and decoder, which the channel has been told not to show meanwhile);
 *    the main loop touches only the cache. After each reply the task raises reply_ready and
 *    waits; the main loop commits the reply, names the next request (or none) and lowers it.
 *  - Memory (heap, internal RAM), all allocated and freed by the main loop, never by the task:
 *    the cache from the first open; per round the round's block (3.1 KB), the task's stack
 *    (PJ_WX_STACK) and what esp_http_client, mbedTLS and lwIP hold for one HTTPS connection; the
 *    radar page (7.8 KB) from the radar request to the close; its decoder (12.6 KB) only while the
 *    picture arrives. A round starts only when the free heap and the largest free block pass the
 *    gate below. The TLS figures are estimates until a supervised run on the console; pj_wx_diag
 *    reports what was really seen.
 *  - Stopping (pj_wx_pause(1), pj_wx_close, pj_wx_forget): the task looks at the stop flag before
 *    every network call. A body read ends within PJ_WX_READ_MS. A connection being made cannot be
 *    interrupted in this client: the name look-up is lwIP's own (1+1+2+3 = 7 s for each DNS
 *    server the network gave, when none answers), then PJ_WX_CONNECT_MS for TCP and TLS, then
 *    PJ_WX_HEADERS_MS for the reply's first line. pj_wx_busy() is 1 until the task is gone, the
 *    idle task has had 30 ms to free its stack, and the round's block is freed.
 *  - The main loop is never held: every call here returns at once (starting or stopping SNTP is
 *    one short call into the TCP/IP task). The task runs at priority 1 like the other network
 *    tasks. Nothing here writes flash or NVS.
 *  - The place (1.6.3): pj_wx_open reads the weather location record Chromatic Manager may have
 *    written (two 256-byte reads at WX_PLACE_FLASH_A/_B, wx_place.h) and keeps the decoded place
 *    (one small heap block) until the channel is left; the request list and pages are then that
 *    place's. No record, or not a good one, or no memory for it: the default pack (Orlando, FL),
 *    its compiled table exactly. A record naming the default's home shows that pack exactly. It is
 *    read only here, never at boot. The cache remembers which place its values are for and starts
 *    clean for another.
 * Modelled on the PC by chromatic/weather/check_weather_glue.c (this very file, against stub
 * headers and a scripted HTTP client).
 */
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <sys/time.h>
#include <time.h>
#include "esp_crt_bundle.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_netif_sntp.h"
#include "esp_system.h"
#include "spi_flash_mmap.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "weather_esp32.h"

/* A TLS handshake with RSA/ECDHE through mbedTLS: ESP-IDF's own HTTPS examples give their task
 * 8 KB; the launcher's plain-HTTP tasks measured 1.8 KB of 3.6 KB. Unmeasured here: pj_wx_diag
 * reports the task's unused stack so the figure can be cut after a console run. */
#define PJ_WX_STACK 8192u
/* The gate for starting a round. Estimated peak of a round: task stack 8.6 KB, HTTP client 3 KB,
 * TLS session 6-8 KB, one incoming TLS record up to 16.7 KB in one block (the servers do not
 * negotiate a smaller one; with CONFIG_MBEDTLS_DYNAMIC_BUFFER it is held only while a record is
 * read), handshake arithmetic and the kept peer certificate 6-8 KB, TCP 4-6 KB: about 40-48 KB.
 * 52 KB free leaves a little for Wi-Fi's own bursts. */
#define PJ_WX_HEAP_MIN 53248u
#define PJ_WX_BLOCK_MIN 20480u
/* The radar picture is asked for only if, with the round's connection still open, this much is
 * free: its decoder (12.6 KB in one block), about 10 KB of room for the next connection's
 * handshake in place of the one that closes, and its page (7.8 KB) unless that is held already. */
#define PJ_WX_RADAR_HEAP_MIN 22528u
#define PJ_WX_RADAR_BLOCK_MIN 16384u
#define PJ_WX_CONNECT_MS 4000         /* TCP and TLS (the name look-up before it is lwIP's own) */
#define PJ_WX_HEADERS_MS 3000         /* the wait for the reply's first line and headers */
#define PJ_WX_READ_MS 1500            /* each read of the body: also how fast a round can be stopped there */
#define PJ_WX_READ_TRIES 5            /* reads in a row with nothing arriving before the reply is given up */
#define PJ_WX_RETRY_MS 120000u        /* a failed request is tried again this much later */
#define PJ_WX_GAP_OK_MS 5000u         /* between rounds */
#define PJ_WX_GAP_FAIL_MS 60000u      /* after a round that failed */
#define PJ_WX_REPLY_MAX 400000        /* a reply longer than this is cut (the largest wanted is about 150 KB) */
#define PJ_WX_DRAIN_MAX 160000        /* what is read on, unwanted, to keep the connection (the largest reply whole) */
#define PJ_WX_HANDOVER_TICKS 1000u    /* the task leaves if the main loop takes no reply for 10 s */
#define PJ_WX_USER_AGENT "chromatic-weather/1.6"

typedef struct {                  /* kept from the first open to pj_wx_forget */
    wx_state state;
    uint32_t due_ms[WX_REQUESTS];
    uint8_t due_set[WX_REQUESTS]; /* 0: never fetched (or to be fetched again): due at once */
    int64_t radar_taken;
    uint32_t hold_until_ms;
    uint32_t fetch_ok,fetch_failed,rounds,heap_start,heap_low,stack_free,radar_ok,radar_failed,connections;
    int32_t last_status;
    uint32_t place_id;            /* which place the values are for: 0 the default pack, else the record's id (wx_place.h) */
    uint8_t hold_set,gap_pending,round_failed,radar_good;
} pj_wx_cache;
typedef struct {                  /* one round of requests: allocated when it starts, freed when its task is gone */
    wx_fetch fetch;
    uint8_t chunk[1024];
    char url[304];
    volatile int alive,stop,reply_ready,reply_status,next_index,current_index;
    volatile int64_t reply_date;  /* the reply's HTTP Date header as UTC seconds; read by the main loop at the hand-over only */
    volatile uint32_t heap_low,stack_free,connections;
    uint32_t done;
} pj_wx_round;
typedef struct {wx_gif gif;uint8_t table[WX_GIF_TABLE];} pj_wx_radar;

/* All the static RAM of this file: pointers and flags. */
static pj_wx_cache *cache;
static pj_wx_round *round_now;
static uint8_t *radar_page;       /* WX_RADAR_BYTES, from a radar request to the close */
static pj_wx_radar *radar_work;   /* only while a radar picture is being fetched */
static wx_place *place_now;       /* the weather location while the channel is open; NULL = the default pack */
static TaskHandle_t task_handle;
static TickType_t gone_at;
static uint8_t is_open,leaving,sntp_started,paused,gone_seen; /* leaving: 1 closing, 2 forgetting */

int64_t pj_wx_epoch(void) {
    time_t now=time(NULL);
    return wx_clock_ok((int64_t)now)?(int64_t)now:0;
}
static void heap_sample(pj_wx_round *r) {
    uint32_t free_now=(uint32_t)esp_get_free_heap_size();
    if(free_now<r->heap_low)r->heap_low=free_now;
}
static esp_err_t on_http_event(esp_http_client_event_t *event) {
    pj_wx_round *r=event->user_data;
    if(r&&event->event_id==HTTP_EVENT_ON_HEADER&&event->header_key&&event->header_value&&!strcasecmp(event->header_key,"Date")) {
        int64_t when=wx_parse_http_date(event->header_value);
        if(wx_clock_ok(when))r->reply_date=when;
    }
    return ESP_OK;
}
static void client_drop(esp_http_client_handle_t *client) {
    if(*client){esp_http_client_close(*client);esp_http_client_cleanup(*client);*client=NULL;}
}
/* One request: 200 when the reply was read to its end or its parser has all it wants; another
 * HTTP status as sent; -1 no connection (DNS, TCP, TLS, certificate) or it broke; -2 cut short;
 * -3 the radar picture was refused or not whole; -4 stopped. (-5, given by the main loop: a good
 * reply that could not be dated because the clock is not set.) */
static int fetch_one(pj_wx_round *r,esp_http_client_handle_t *client,const char **client_host,unsigned index) {
    wx_req_info req;  /* no record: the default pack's table entry and address exactly; a place: its own (wx_place.h) */
    if(!wx_place_request(place_now,index,&req,r->url,sizeof(r->url)))return -1;
    int radar=req.kind==WX_K_RADAR;
    if(radar&&(!radar_work||!radar_page))return -1;
    if(*client&&*client_host!=req.host)client_drop(client); /* another server: never two connections at once */
    for(int attempt=0;attempt<2;attempt++) {
        int reused=*client!=NULL;
        if(r->stop)return -4;
        wx_fetch_begin_as(&r->fetch,req.kind,req.slot);
        if(place_now)wx_fetch_zone(&r->fetch,place_now->tz);
        wx_fetch_clock(&r->fetch,pj_wx_epoch());
        if(*client) {
            if(esp_http_client_set_url(*client,r->url)!=ESP_OK){client_drop(client);reused=0;}
        }
        if(!*client) {
            esp_http_client_config_t config={.url=r->url,.method=HTTP_METHOD_GET,.timeout_ms=PJ_WX_CONNECT_MS,
                .buffer_size=1024,.buffer_size_tx=512,.disable_auto_redirect=true,.user_agent=PJ_WX_USER_AGENT,
                .event_handler=on_http_event,.user_data=r,.crt_bundle_attach=esp_crt_bundle_attach};
            *client=esp_http_client_init(&config);
            if(!*client)return -1;
            *client_host=req.host;
            r->connections++;
        }
        esp_http_client_set_header(*client,"Accept",radar?"image/gif":"application/geo+json");
        esp_http_client_set_timeout_ms(*client,PJ_WX_CONNECT_MS);
        TickType_t sent_at=xTaskGetTickCount();
        int opened=esp_http_client_open(*client,0)==ESP_OK;
        if(opened&&r->stop){client_drop(client);return -4;}
        if(opened)esp_http_client_set_timeout_ms(*client,PJ_WX_HEADERS_MS);
        if(!opened||esp_http_client_fetch_headers(*client)<0) {
            heap_sample(r);
            client_drop(client);
            /* a kept connection the server had closed fails at once: one more try on a new one */
            if(reused&&!r->stop&&(TickType_t)(xTaskGetTickCount()-sent_at)<pdMS_TO_TICKS(2000))continue;
            return r->stop?-4:-1;
        }
        heap_sample(r);
        esp_http_client_set_timeout_ms(*client,PJ_WX_READ_MS);
        int status=esp_http_client_get_status_code(*client),want=status==200,cut=0,idle=0;
        int64_t length=esp_http_client_get_content_length(*client),got=0,unwanted=0;
        if(radar&&want)wx_gif_begin(&radar_work->gif,radar_work->table,radar_page,WX_RADAR_W,WX_RADAR_H);
        for(;;) {
            if(r->stop){cut=1;break;}
            int n=esp_http_client_read(*client,(char *)r->chunk,sizeof(r->chunk));
            if(n==-ESP_ERR_HTTP_EAGAIN){if(++idle>=PJ_WX_READ_TRIES){cut=1;break;}continue;} /* nothing yet: the stop flag is looked at, then again */
            if(n<0){cut=1;break;}
            if(n==0)break;
            idle=0;got+=n;
            if(want)want=radar?wx_gif_feed(&radar_work->gif,r->chunk,(size_t)n):wx_fetch_feed(&r->fetch,r->chunk,(size_t)n);
            else unwanted+=n;
            heap_sample(r);
            if(got>PJ_WX_REPLY_MAX){cut=1;break;}
            /* Everything wanted has been read. The rest is read on and dropped to keep the connection, whether
             * the length was announced or not (a chunked reply ends by itself), unless it is known or turns out
             * to be longer than the drain limit. */
            if(!want&&(unwanted>PJ_WX_DRAIN_MAX||(length>0&&length-got>PJ_WX_DRAIN_MAX))){cut=1;break;}
        }
        int complete=!cut&&(length>0?got==length:esp_http_client_is_complete_data_received(*client));
        if(!complete||status!=200)client_drop(client); /* the connection cannot be used again */
        r->stack_free=(uint32_t)uxTaskGetStackHighWaterMark(NULL)*sizeof(StackType_t);
        if(status!=200)return status>0?status:-1;
        if(radar) {
            if(!wx_gif_ok(&radar_work->gif))return r->stop?-4:-3;
            wx_radar_despeckle(radar_page,WX_RADAR_W,WX_RADAR_H);
            return 200;
        }
        if(complete||wx_fetch_done(&r->fetch))return 200;
        return r->stop?-4:-2;
    }
    return -1;
}
static void fetch_task(void *arg) {
    pj_wx_round *r=arg;
    esp_http_client_handle_t client=NULL;
    const char *client_host=NULL;
    for(;;) {
        int status=fetch_one(r,&client,&client_host,(unsigned)r->current_index);
        r->reply_status=status;__sync_synchronize();r->reply_ready=1;
        /* the main loop commits the reply and names the next request; this task only waits */
        for(TickType_t t0=xTaskGetTickCount();r->reply_ready&&!r->stop;) {
            vTaskDelay(1);
            if((TickType_t)(xTaskGetTickCount()-t0)>=PJ_WX_HANDOVER_TICKS)r->stop=1; /* nobody is taking replies */
        }
        if(r->stop||r->next_index<0)break;
        /* an HTTP error or a refused picture does not end the round; a dead connection does */
        if(status==-1||status==-2)break;
        r->current_index=r->next_index;
    }
    client_drop(&client);
    heap_sample(r);
    r->alive=0;
    vTaskDelete(NULL);
}
static int request_due(unsigned index,uint32_t now_ms){return !cache->due_set[index]||(int32_t)(now_ms-cache->due_ms[index])>=0;}
static void put_off(unsigned index,uint32_t now_ms,uint32_t by_ms){cache->due_set[index]=1;cache->due_ms[index]=now_ms+by_ms;}
/* The radar request's memory, taken by the main loop before the task is told to fetch it; the
 * channel stops showing the page of levels first, because the task will write into it. */
static int radar_prepare(void) {
    const uint32_t caps=MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL;
    if(esp_get_free_heap_size()<PJ_WX_RADAR_HEAP_MIN+(radar_page?0u:WX_RADAR_BYTES)||heap_caps_get_largest_free_block(caps)<PJ_WX_RADAR_BLOCK_MIN)return 0;
    if(!radar_work&&!(radar_work=heap_caps_calloc(1,sizeof(pj_wx_radar),caps)))return 0; /* the large block first */
    if(!radar_page&&!(radar_page=heap_caps_calloc(1,WX_RADAR_BYTES,caps))){heap_caps_free(radar_work);radar_work=NULL;return 0;}
    wx_set_radar(&cache->state,NULL,0,0,0);
    return 1;
}
/* The next request to make: the first in table order that is due and wanted; -1 none. */
static int pick_next(uint32_t now_ms,int prepare) {
    for(unsigned i=0;i<wx_place_request_count(place_now);i++) {
        wx_req_info req;
        if(!wx_place_info(place_now,i,&req)||!request_due(i,now_ms)||!wx_kind_wanted(&cache->state,req.kind,req.slot))continue;
        if(prepare&&req.kind==WX_K_RADAR&&!radar_prepare()){put_off(i,now_ms,PJ_WX_RETRY_MS);cache->radar_failed++;continue;}
        return (int)i;
    }
    return -1;
}
/* The weather location record, read when the channel opens (two 256-byte reads); NULL for none, a
 * record that is not good, a failed read or no memory: the default pack. Never fails the channel. */
/* 1.6.5: the location pack for that place (wx_lpack.h), when Chromatic Manager wrote one: two 16-byte reads to choose
 * the copy, then that copy checked whole where it lies in flash: the 64 KiB page that holds both copies
 * (PJ_WX_PACK_PAGE) is memory-mapped (data, read only through the flash cache), so the pack's maps, table and names stay
 * in flash as the built-in tables do and only its index (wx_lpack, about 0.9 KB) is on the heap (review M1 of 1.6.5).
 * Should the mapping fail (no free MMU page), the copy is read into one heap block instead, as the first 1.6.5 build
 * did. The other copy when the first is not good. Kept with the place until the channel is left; nothing when there
 * is none, it is not good, names another place, cannot be read or there is no memory: the place's generic pages, as
 * in 1.6.4. Nothing on the console ever writes the pack's sectors, so the mapped bytes cannot change while in use. */
#define PJ_WX_PACK_PAGE 0x3F0000u
#define PJ_WX_PACK_PAGE_SIZE 0x10000u
_Static_assert(WX_LPACK_FLASH_A>=PJ_WX_PACK_PAGE&&WX_LPACK_FLASH_B+WX_LPACK_COPY<=PJ_WX_PACK_PAGE+PJ_WX_PACK_PAGE_SIZE,
               "both pack copies lie in the one mapped 64 KiB page");
static uint8_t *pack_bytes;                 /* the fallback's heap copy */
static wx_lpack *pack_now;
static spi_flash_mmap_handle_t pack_map;
static uint8_t pack_mapped;
static void pack_release(void) {
    if(pack_now){heap_caps_free(pack_now);pack_now=NULL;}
    if(pack_bytes){heap_caps_free(pack_bytes);pack_bytes=NULL;}
    if(pack_mapped){spi_flash_munmap(pack_map);pack_mapped=0;}
}
static void pack_read(wx_place *p) {
    const uint32_t caps=MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL;
    uint8_t head[2][16];
    const uint32_t at[2]={WX_LPACK_FLASH_A,WX_LPACK_FLASH_B};
    for(int c=0;c<2;c++)if(esp_flash_read(NULL,head[c],at[c],sizeof(head[c]))!=ESP_OK)memset(head[c],0xff,sizeof(head[c]));
    int first=wx_lpack_b_first(head[0],head[1]);
    if(!wx_lpack_length(head[0])&&!wx_lpack_length(head[1]))return;   /* no pack: nothing mapped, nothing taken */
    const void *page=NULL;
    if(spi_flash_mmap(PJ_WX_PACK_PAGE,PJ_WX_PACK_PAGE_SIZE,SPI_FLASH_MMAP_DATA,&page,&pack_map)==ESP_OK&&page)pack_mapped=1;
    for(int t=0;t<2;t++) {
        int c=t?!first:first;
        uint32_t len=wx_lpack_length(head[c]);
        if(!len)continue;
        if(!pack_now&&!(pack_now=heap_caps_calloc(1,sizeof(wx_lpack),caps)))break;
        const uint8_t *bytes;
        if(pack_mapped)bytes=(const uint8_t*)page+(at[c]-PJ_WX_PACK_PAGE);
        else {
            if(!(pack_bytes=heap_caps_calloc(1,len,caps)))break;
            if(esp_flash_read(NULL,pack_bytes,at[c],len)!=ESP_OK){heap_caps_free(pack_bytes);pack_bytes=NULL;continue;}
            bytes=pack_bytes;
        }
        if(wx_lpack_parse(bytes,len,pack_now)&&wx_place_use_pack(p,pack_now))return;
        if(pack_bytes){heap_caps_free(pack_bytes);pack_bytes=NULL;}
    }
    pack_release();
}
static wx_place *place_read(void) {
    const uint32_t caps=MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL;
    typedef struct {uint8_t raw[2][WX_PLACE_RECORD];wx_place spare;} work;
    wx_place *p=heap_caps_calloc(1,sizeof(wx_place),caps);  /* kept until the channel is left */
    work *w=p?heap_caps_calloc(1,sizeof(work),caps):NULL;  /* only for this call, taken after it */
    int good=0;
    if(w) {
        if(esp_flash_read(NULL,w->raw[0],WX_PLACE_FLASH_A,WX_PLACE_RECORD)!=ESP_OK)memset(w->raw[0],0xff,WX_PLACE_RECORD);
        if(esp_flash_read(NULL,w->raw[1],WX_PLACE_FLASH_B,WX_PLACE_RECORD)!=ESP_OK)memset(w->raw[1],0xff,WX_PLACE_RECORD);
        good=wx_place_pick(w->raw[0],w->raw[1],WX_PLACE_RECORD,p,&w->spare);
    }
    if(w)heap_caps_free(w);
    if(!good&&p){heap_caps_free(p);p=NULL;}
    if(p)pack_read(p);
    return p;
}
static void place_release(void) {
    if(cache)wx_set_place(&cache->state,NULL);
    if(place_now){heap_caps_free(place_now);place_now=NULL;}
    pack_release();
}
/* The round's end. 1 while its task lives or the idle task may still hold its stack; then the
 * round's block and the radar decoder are freed and 0 is returned. */
static int round_reap(void) {
    pj_wx_round *r=round_now;
    if(!r)return 0;
    if(r->alive)return 1;
    if(task_handle) {
        if(!gone_seen){gone_seen=1;gone_at=xTaskGetTickCount();}
        if((TickType_t)(xTaskGetTickCount()-gone_at)<pdMS_TO_TICKS(30))return 1;
        task_handle=NULL;gone_seen=0;
    }
    if(radar_work) { /* a radar reply nobody took: the page may be half written */
        if(wx_gif_touched(&radar_work->gif)&&cache)cache->radar_good=0;
        heap_caps_free(radar_work);radar_work=NULL;
        if(cache&&cache->radar_good&&is_open)wx_set_radar(&cache->state,radar_page,WX_RADAR_W,WX_RADAR_H,cache->radar_taken);
    }
    if(cache) {
        cache->heap_low=r->heap_low;cache->stack_free=r->stack_free;cache->connections=r->connections;
        cache->gap_pending=1;
        wx_set_updating(&cache->state,0);
    }
    heap_caps_free(r);round_now=NULL;
    return 0;
}

int pj_wx_open(void) {
    if(leaving||round_now)return -6;
    place_release(); /* (an open without a close: nothing of a round is using it, there is none) */
    if(!cache) {
        cache=heap_caps_calloc(1,sizeof(pj_wx_cache),MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL);
        if(!cache)return -2;
        wx_init(&cache->state);
    }
    place_now=place_read();
    uint32_t place_id=place_now?place_now->id:0u;
    if(cache->place_id!=place_id) { /* the values and ages held are another place's: start clean */
        wx_init(&cache->state);
        memset(cache->due_ms,0,sizeof(cache->due_ms));memset(cache->due_set,0,sizeof(cache->due_set));
        cache->place_id=place_id;
    }
    wx_set_place(&cache->state,place_now);
    /* the radar picture did not stay: its request is due again; a failed round's wait does not carry over */
    for(unsigned i=0;i<wx_place_request_count(place_now);i++) {
        wx_req_info req;
        if(wx_place_info(place_now,i,&req)&&req.kind==WX_K_RADAR)cache->due_set[i]=0;
    }
    cache->radar_good=0;cache->hold_set=0;cache->gap_pending=0;
    is_open=1;paused=0;
    return 0;
}
wx_state *pj_wx_state(void){return is_open&&cache?&cache->state:NULL;}
int pj_wx_busy(void){return round_reap();}
/* 1: no new request starts and the round in flight is stopped (the reply being read is dropped). */
void pj_wx_pause(int on) {
    paused=(uint8_t)(on!=0);
    if(paused&&round_now){round_now->stop=1;__sync_synchronize();}
}
static void leave(int how) {
    if(round_now){round_now->stop=1;__sync_synchronize();}
    is_open=0;
    if(how>leaving)leaving=(uint8_t)how;
}
void pj_wx_close(void){if(is_open)leave(1);}
void pj_wx_forget(void){if(is_open||cache||leaving)leave(2);}
int pj_wx_poll(void) {
    if(!leaving)return 0;
    if(round_reap())return 1;
    if(sntp_started){esp_netif_sntp_deinit();sntp_started=0;}
    if(radar_work){heap_caps_free(radar_work);radar_work=NULL;}
    if(radar_page){heap_caps_free(radar_page);radar_page=NULL;}
    place_release(); /* the round's task, the only other reader, is gone */
    if(cache) {
        wx_set_radar(&cache->state,NULL,0,0,0);
        wx_set_updating(&cache->state,0);
        cache->radar_good=0;
        if(leaving==2){heap_caps_free(cache);cache=NULL;}
    }
    leaving=0;
    return 0;
}

void pj_wx_service(uint32_t now_ms,int wifi_up) {
    if(!is_open||!cache)return;
    if(wifi_up&&!sntp_started) {
        esp_sntp_config_t config=ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
        config.wait_for_sync=false; /* nobody waits: pj_wx_epoch reads the clock */
        if(esp_netif_sntp_init(&config)==ESP_OK)sntp_started=1;
    }
    pj_wx_round *r=round_now;
    if(r&&r->alive) {
        if(!r->reply_ready)return;
        /* the task is waiting and not touching the round's block: this reply is ours */
        int status=r->reply_status,index=r->current_index;
        if(!pj_wx_epoch()&&wx_clock_ok(r->reply_date)) { /* SNTP has not answered: the server's own Date sets the clock */
            struct timeval tv={.tv_sec=(time_t)r->reply_date,.tv_usec=0};
            settimeofday(&tv,NULL);
        }
        int64_t epoch=pj_wx_epoch();
        if(status==200&&!epoch)status=-5; /* no believable clock (no SNTP answer, no Date header): nothing can be dated, so nothing is kept */
        cache->last_status=status;r->done++;
        wx_req_info req;
        if(index>=0&&wx_place_info(place_now,(unsigned)index,&req)&&status!=-4) {
            if(req.kind==WX_K_RADAR) {
                if(status==200&&radar_work&&wx_gif_ok(&radar_work->gif)&&epoch){cache->radar_good=1;cache->radar_taken=epoch;cache->radar_ok++;}
                else{if(radar_work&&wx_gif_touched(&radar_work->gif))cache->radar_good=0;cache->radar_failed++;} /* an untouched page keeps the last picture */
                if(radar_work){heap_caps_free(radar_work);radar_work=NULL;}
                if(cache->radar_good)wx_set_radar(&cache->state,radar_page,WX_RADAR_W,WX_RADAR_H,cache->radar_taken);
            } else wx_fetch_commit(&cache->state,&r->fetch,status,epoch);
            if(status==200){cache->fetch_ok++;put_off((unsigned)index,now_ms,req.period_s*1000u);}
            else{cache->fetch_failed++;cache->round_failed=1;put_off((unsigned)index,now_ms,PJ_WX_RETRY_MS);}
        }
        r->next_index=!r->stop&&!paused&&wifi_up&&status!=-1&&status!=-2&&status!=-4?pick_next(now_ms,1):-1;
        __sync_synchronize();
        r->reply_ready=0;
        return;
    }
    if(r&&round_reap())return; /* the round has ended; a moment later its stack is free too */
    if(cache->gap_pending) {
        cache->gap_pending=0;
        cache->hold_until_ms=now_ms+(cache->round_failed?PJ_WX_GAP_FAIL_MS:PJ_WX_GAP_OK_MS);cache->hold_set=1;
        return;
    }
    if(paused||!wifi_up)return;
    if(cache->hold_set&&(int32_t)(now_ms-cache->hold_until_ms)<0)return;
    if(pick_next(now_ms,0)<0)return; /* everything held is younger than its period */
    uint32_t free_now=(uint32_t)esp_get_free_heap_size();
    const uint32_t caps=MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL;
    if(free_now<PJ_WX_HEAP_MIN||heap_caps_get_largest_free_block(caps)<PJ_WX_BLOCK_MIN)return; /* looked at again on the next pass */
    r=heap_caps_calloc(1,sizeof(pj_wx_round),caps);
    if(!r){cache->hold_until_ms=now_ms+PJ_WX_GAP_OK_MS;cache->hold_set=1;return;}
    round_now=r;
    int index=pick_next(now_ms,1);
    if(index<0){heap_caps_free(r);round_now=NULL;return;}
    cache->heap_start=r->heap_low=free_now;cache->round_failed=0;
    r->current_index=index;r->next_index=-1;r->alive=1;
    __sync_synchronize();
    if(xTaskCreate(fetch_task,"wx-fetch",PJ_WX_STACK,r,1,&task_handle)!=pdPASS) {
        task_handle=NULL;r->alive=0;
        if(radar_work){heap_caps_free(radar_work);radar_work=NULL;}
        heap_caps_free(r);round_now=NULL;
        cache->hold_until_ms=now_ms+PJ_WX_GAP_FAIL_MS;cache->hold_set=1;
        return;
    }
    cache->rounds++;
    wx_set_updating(&cache->state,1);
}
void pj_wx_diag(char *out,size_t size) {
    if(!out||!size)return;
    if(!cache){snprintf(out,size,"weather open=0 cached=0 busy=%d clock=%d",round_now!=NULL,pj_wx_epoch()!=0);return;}
    /* place=0 the default pack, 1 the weather location record: whether, never which. pack=map: its location pack read
     * in place from flash (only its index on the heap); heap: held in a heap block (the mapping failed); none. With
     * heap-low (the least free heap seen in a round, the pack held) and radar-failed (the radar put off for memory) it
     * answers review M1 of 1.6.5 after one look at the console. */
    snprintf(out,size,"weather open=%d cached=1 busy=%d clock=%d place=%d pack=%s rounds=%u ok=%u failed=%u last=%d conn=%u radar=%d radar-ok=%u "
             "radar-failed=%u heap-start=%u heap-low=%u stack-free=%u",
             is_open,round_now!=NULL,pj_wx_epoch()!=0,cache->place_id!=0,pack_mapped?"map":pack_bytes?"heap":"none",
             (unsigned)cache->rounds,(unsigned)cache->fetch_ok,(unsigned)cache->fetch_failed,
             (int)cache->last_status,(unsigned)cache->connections,cache->radar_good,(unsigned)cache->radar_ok,(unsigned)cache->radar_failed,
             (unsigned)cache->heap_start,(unsigned)cache->heap_low,(unsigned)cache->stack_free);
}
