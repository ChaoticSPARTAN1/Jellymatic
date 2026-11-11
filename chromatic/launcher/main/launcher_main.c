/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Startup menu shown only on power-on. Everything that decides behaviour is in
 * shared/launcher_logic.h, shared/remote.h and shared/service.h; this file only moves bytes.
 *
 * Bus setup copies stock main.c exactly (same pins, QSPI command, 40 MHz, UART1 at
 * 115200) because the stock FPGA is what listens. It sends only the two messages
 * the launcher needs. It writes no flash, opens no NVS, and has no cartridge path.
 *
 * Any way out of here lands on stock: "Games" restarts (not a power-on reset, so the
 * bootloader picks stock); a hang stops the watchdog feed and the RTC watchdog does
 * the same; a crash resets the same way.
 *
 * Remote control (docs/Design-notes.md): one CHR1 handler, two transports.
 *  - USB: received bytes come from the UART0 driver's RX ring buffer (RX only: no TX
 *    buffer, and the console VFS is not switched to the driver, so printf, the boot
 *    markers and the hand-over behave exactly as before). Replies go into the transmit
 *    FIFO only while there is room, so the loop never blocks on the PC.
 *  - LAN: only after the owner presses A on the Jellyfin screen with a provisioned
 *    record: Wi-Fi joins (RAM storage, no NVS) and a task serves TCP 38771 to private
 *    addresses on the console's own subnet. The radio is off before stock runs.
 * Flash is only read: the provisioning record and, for Chromatic Manager, esp_flash_read.
 */
#include <stdio.h>
#include <string.h>
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "driver/uart.h"
#include "esp_event.h"
#include "esp_flash.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "bootloader_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "hal/efuse_hal.h"
#include "hal/uart_ll.h"
#include "hal/wdt_hal.h"
#include "lwip/sockets.h"
#include "esp_heap_caps.h"
#include "soc/uart_struct.h"
#include "esp32_media.h"
#include "jellyfin_esp32.h"
#include "launcher_diag.h"
#include "launcher_logic.h"
#include "wifi_order.h"
#include "wifi_scan.h" /* pj_join_result / pj_join_words: a failed join in plain words (counts only in diagnostics) */
#include "log_ring.h"
#include "provision.h"
#include "remote.h"
#include "usb_tx.h"
#include "service.h"
#include "service_screen.h"
#include "pocket_link_screen.h" /* 1.3.0: the Pocket link status screen (pocket_link.h pj_plink_view) */
#include "ui_font.h"
#if PJ_ENABLE_SETUP /* SETUP HOOK: on-console Wi-Fi / Jellyfin setup (shared/setup_ui.h); 0 compiles it all out */
#include "setup_esp32.h"
#define PJ_SETUP_IS_BUSY (pj_setup_esp32_busy()) /* a setup worker (scan, discovery, HTTP) is running: no radio change under it */
#else
#define PJ_SETUP_IS_BUSY 0
#endif
/* Printed on the console UART (the USB serial port) so the PC tool can prove which
 * program is running after a write. Plain text only: nothing about the unit. */
#define PJ_LAUNCHER_VERSION "1.5.1" /* must equal PROJECT_VER in CMakeLists.txt */
/* 1.3.5: the FPGA version line ("fpga version=18.41 debug=1") is said again every PJ_FPGA_LINE_MS and whenever the
 * reported version changes, so Chromatic Manager can read which image runs passively (no restart of this chip) after
 * a temporary FPGA load, and a console whose FPGA was reconfigured under a running launcher says so. */
#define PJ_FPGA_LINE_MS 5000u
#define PJ_LAUNCHER_MARKER "PJ-LAUNCHER-READY " PJ_LAUNCHER_VERSION
#define PJ_SLOT_OFFSET 0x260000u /* the launcher partition (partitions.csv) */

/* Stock main/board.h */
#define PIN_QSPI_CS GPIO_NUM_5
#define PIN_QSPI_CLK GPIO_NUM_18
#define PIN_QSPI_MOSI GPIO_NUM_23
#define PIN_QSPI_MISO GPIO_NUM_19
#define PIN_QSPI_WP GPIO_NUM_22
#define PIN_QSPI_HD GPIO_NUM_21
#define PIN_UART_FROM_FPGA GPIO_NUM_10
#define PIN_UART_TO_FPGA GPIO_NUM_9
#define BYTES_PER_XFER 1024u
#define NUM_XFERS (PJ_OSD_W*PJ_OSD_H*2u/BYTES_PER_XFER)

static DMA_ATTR uint16_t frame[PJ_OSD_W*PJ_OSD_H];
static spi_device_handle_t spi;
static pj_launcher launcher;
static pj_fpga_rx rx;
static char radio_line[40]="Wi-Fi: press A to check";
static char remote_line[40]="Remote Pilot: USB";
static volatile int radio_done,radio_started,radio_running;
static volatile unsigned join_heard,join_try; /* diagnostics: saved networks heard in the scan, which try joined */
/* Launcher 1.2.9: Wi-Fi switching and reconnecting without a restart (docs/Design-notes.md).
 *  - joined_ssid: the saved network the console is on, name only, never printed: Setup marks it "connected".
 *  - rejoin_wanted: run join_task again after a clean teardown (the saved list changed in Setup, "Use this
 *    network now", or a dropped link that quick retries could not bring back). rejoin_from_setup: Setup asked,
 *    so the join may start while Setup is open (its own scan is refused meanwhile: radio_running).
 *  - lan_alive / lan_quit: the CHR1 LAN task's life; it is asked to leave and waited for before any teardown.
 *  - link_lost: the joined network dropped (the ESP-IDF driver never reconnects by itself): wifi_connect() is
 *    retried with back-off while the driver stays up (the Jellyfin app may be running and is told); after four
 *    tries with more than one saved network and no app running, a full rejoin (scan, priority order) runs.
 *  - join_state / join_fail: what the radio is doing, in plain words for Setup's Switching screen.
 *  - menu_setup_wanted: the Jellyfin menu's "Wi-Fi & sign-in" row: Setup opens once the app has let go. */
static char joined_ssid[33];
static int rejoin_wanted,rejoin_from_setup,menu_setup_wanted;static uint32_t rejoin_retry_at; /* retry_at: after a failed settings read */
static volatile int lan_alive,lan_quit;
static int link_lost;static uint32_t link_lost_ms,link_retry_at;static unsigned link_tries;
enum {JOIN_IDLE,JOIN_JOINING,JOIN_CONNECTED,JOIN_FAILED}; /* the same order as setup_ui.h PJ_SU_JOIN_* */
static volatile int join_state,join_fail,connect_pending,disconnect_reason;
#if PJ_ENABLE_SETUP
#define PJ_SETUP_IS_OPEN (launcher.setup_open)
#else
#define PJ_SETUP_IS_OPEN 0
#endif
/* Pairing is provisioned from the PC. Nothing is stored yet, so this stays NULL and
 * the screen says so; connecting would use pj_esp_media_connect() with it. */
static const pj_esp_media_config *const pairing=NULL;
/* Diagnostics: printed as PJ-DIAG lines on the USB serial console (read-only for
 * Remote Pilot) and kept in a RAM ring for the CHR1 "log" action. Counts and fixed
 * names only; shared/launcher_diag.h has the rules each line is checked against. */
static pj_log diag_log;
static pj_diag_counts counts;
static pj_diag_watch watch;
/* Written by the radio tasks, reported once by the main loop (never after handover). */
static volatile int radio_ok,radio_err;
static volatile unsigned radio_networks;
static const char *volatile radio_step="none";
/* Remote control and Chromatic Manager's service sessions. */
static pj_svc svc;
static pj_remote rmt;
static pj_prov2 record;       /* RAM only: networks kept for joining, every other secret wiped */
static int record_valid,record_wifi,record_jellyfin;
/* The console's Jellyfin sign-in, copied from the record before it is wiped: RAM only, never
 * printed, and only handed to the Jellyfin app (jellyfin_esp32.c) while it runs. */
static pj_jf_creds jf_creds;
#if PJ_ENABLE_SETUP
/* SETUP HOOK: the on-console setup screens (Select on the Jellyfin screen). All of its state is heap,
 * allocated by pj_setup_esp32_open while it is open (setup_esp32.c; refused with a message when the
 * heap is short); here only this pointer and a flag. It hands a new sign-in to jf_creds, and its one
 * write is pj_provision_commit (shared/provision_write.c), run by pj_setup_esp32_service. */
static pj_setup *setup;static uint8_t setup_refused;
#endif
static int jf_failed;static uint32_t jf_last_stats,jf_retry_at; /* jf_failed: failed starts in a row */
static int jf_sound; /* the FPGA reported media audio: Jellyfin plays sound and holds the media bit */
/* Launcher 1.3.0 (docs/Design-notes.md "Launcher 1.3.0"). The Pocket link screen replaces the Jellyfin app
 * (never both in memory) and exists only on the link FPGA (18.41, pj_fpga_pocket_link): on stock and on the 18.40 sound
 * image nothing below ever runs and the launcher behaves as 1.2.9.
 *  - plink_wanted: the app's "Pocket link" row was chosen; the link opens once the app has let go of its memory.
 *  - plink_hold_until: the link could not start (memory): the Jellyfin screen says so for 4 s, then the app returns.
 *  - find_wanted: the app's "Can't reach the Jellyfin server" asked for Find my server again: Setup opens at it. */
static int plink_wanted,find_wanted,plink_refused;static uint32_t plink_hold_until,plink_drawn_ms,plink_stats_ms;
static unsigned plink_rx_line=PJ_PLD_PERIODIC; /* 1.3.6: the next "plink rx" line to offer after the 5 s line */
static pj_plink_status plink_shown;
static int link_fpga(void){return rx.version_seen&&pj_fpga_pocket_link(rx.version);}
static uint32_t jf_press_at[16]; /* per button: a second edge within 120 ms is contact bounce */
static uint32_t loop_ms,work_ms;
/* Remote Pilot (CHR1) cost in the main loop, for the Jellyfin diagnostics: the longest pass and the sum since
 * the last line (receive, actions, output pumps), and the live frames encoded; a CHV1 frame is built by
 * pj_rmt_video inside pj_rmt_rx and costs a pass a few ms on top of the 46 KB it then sends. */
static uint32_t chr1_us_max,chr1_us_sum,chr1_frames_mark;
static esp_reset_reason_t reset_reason;
/* LAN: the server task moves bytes between its socket and these; the main loop owns
 * the CHR1 handler. Flags are set by one side and cleared by the other. */
static StreamBufferHandle_t volatile lan_rx,lan_tx;
static volatile int wifi_state,lan_opened,lan_closed,lan_close_request,lan_connected,radio_stop;
static volatile uint32_t own_ip,own_mask;
static EventGroupHandle_t wifi_events;
#define WIFI_GOT_IP BIT0
#define WIFI_LOST BIT1

static uint32_t now_ms(void) {return (uint32_t)(esp_timer_get_time()/1000);}
/* Chromatic Manager's only flash access: a read. */
static int flash_read(void *unused,uint32_t offset,void *out,uint32_t size) {
    (void)unused;
    return esp_flash_read(NULL,out,offset,size)==ESP_OK?0:1;
}
static uint32_t random32(void *unused) {(void)unused;return esp_random();}
static void facts(void *unused,pj_rmt_facts *f) {
    static const char *const states[]={"off","joining","connected","failed"};
    (void)unused;
    f->heap_free=esp_get_free_heap_size();f->heap_min=esp_get_minimum_free_heap_size();
    f->loop_ms=loop_ms;f->work_ms=work_ms;f->frames=counts.frames;
    f->stack_free=(uint32_t)uxTaskGetStackHighWaterMark(NULL); /* bytes, main task (the only caller) */
    f->wifi=states[wifi_state&3];f->reset=PJ_DIAG_NAME(pj_diag_reset_names,reset_reason);
    f->fpga_version=rx.version;f->fpga_seen=rx.version_seen; /* 1.3.5: CHR1 status says which FPGA image runs */
}
/* 1.3.5: the FPGA version line, as at boot (Chromatic Manager's pattern: "fpga version=18.41 debug=1" / "none"). */
static void fpga_version_line(char *body,size_t size) {
    if(rx.version_seen)snprintf(body,size,"fpga version=%u.%u debug=%u",(unsigned)(rx.version&63u),(unsigned)((rx.version>>6)&63u),(unsigned)((rx.version>>12)&1u));
    else snprintf(body,size,"fpga version=none");
}
/* USB (1.3.2): owed CHR1 bytes go into usb_ring here, on the main task (the CHR1 handler keeps one owner), and
 * usb_drain moves them into the 128-byte UART0 transmit FIFO every 5 ms from the esp_timer task, whatever the
 * main loop is doing. Before 1.3.2 the FIFO was filled once per pass: with Jellyfin open (45-60 ms passes) USB
 * carried about 2.5 KB/s, a 46 KB screen took about 18 s and Remote Pilot dropped the session after 9 s
 * (docs/Design-notes.md, "USB pace with Jellyfin open"; model: scripts/model_chromatic_usb_pace.py).
 * The drain advances the ring only after the bytes are in the FIFO, so an empty ring means every CHR1 byte is
 * ahead of anything printf writes next. Still no UART0 TX driver and no VFS change. */
static pj_utx usb_ring;
static esp_timer_handle_t usb_drain_timer;
static uint32_t uart0_room(void){return uart_ll_get_txfifo_len(&UART0);}
static void uart0_write(const uint8_t *data,uint32_t size){uart_ll_write_txfifo(&UART0,data,size);}
static void usb_drain(void *unused){(void)unused;pj_utx_drain(&usb_ring,uart0_room,uart0_write);}
/* 1 while CHR1 owns USB or its bytes are still going out: printf stays off the UART (the log ring keeps the line). */
static int usb_quiet(void){return pj_rmt_usb_quiet(&rmt)||!pj_utx_idle(&usb_ring);}
/* With `whole` (no session) a short text line still owed is finished, so a printed diagnostics line never lands
 * inside it; that wait is only ever for a line (never a body) and at most 30 ms. Otherwise it stops when the ring
 * is full. */
static void usb_pump(int whole) {
    uint32_t start=now_ms();
    for(;;) {
        uint8_t bytes[256];size_t space=pj_utx_space(&usb_ring),n=0;
        if(space)n=pj_rmt_out(&rmt,&rmt.usb,bytes,space<sizeof(bytes)?space:sizeof(bytes));
        if(n){pj_utx_put(&usb_ring,bytes,n);continue;}
        if(whole&&(pj_rmt_busy_output(&rmt.usb)||!pj_utx_idle(&usb_ring))&&!rmt.usb.body_len&&now_ms()-start<30u)continue;
        return;
    }
}
/* Before the last lines and the restart to stock: what is in the ring goes out first (1 KB is 90 ms at 115200),
 * then the drain stops, so nothing of ours is written after the hand-over marker. */
static void usb_finish(void) {
    for(uint32_t t0=now_ms();!pj_utx_idle(&usb_ring)&&now_ms()-t0<200u;)vTaskDelay(1);
    if(usb_drain_timer)esp_timer_stop(usb_drain_timer);
}
/* USB: what the PC sent, from the driver's 4 KiB RX buffer (filled by its interrupt, so a
 * slow pass loses nothing); at most 1 KiB per pass. */
static void usb_receive(uint32_t now) {
    uint8_t bytes[128];int n;
    for(unsigned budget=8;budget&&(n=uart_read_bytes(UART_NUM_0,bytes,sizeof(bytes),0))>0;budget--)
        for(int i=0;i<n;i++)pj_rmt_rx(&rmt,&rmt.usb,bytes[i],now);
}
/* LAN: owed CHR1 bytes into the server task's stream buffer while there is room. */
static void lan_pump(void) {
    if(!lan_tx)return;
    uint8_t out[512];size_t space,n;
    while(lan_connected&&(space=xStreamBufferSpacesAvailable(lan_tx))>0&&
          (n=pj_rmt_out(&rmt,&rmt.lan,out,space<sizeof(out)?space:sizeof(out)))>0)xStreamBufferSend(lan_tx,out,n,0);
    if(rmt.lan.want_close&&lan_connected)lan_close_request=1;
}
/* Both pumps, also run from inside the Jellyfin app's picture decodes (pj_jf_set_pump): a 40-50 ms decode
 * would otherwise let the 1 KB USB ring (90 ms of line) and the LAN buffer run dry. */
static void io_pump(void){usb_pump(0);lan_pump();}
static void diag(const char *body) {
    char line[PJ_DIAG_MAX];
    uint32_t t=now_ms();
    if(!pj_diag_line(line,sizeof(line),t,body))counts.unsafe++;
    counts.lines++;
    /* While USB carries CHR1 the line goes to the ring alone (the log action serves it). */
    if(!usb_quiet())printf("%s\n",line);
    /* The ring keeps the text after the time; pj_log_add stamps the time itself. */
    const char *rest=strchr(line+sizeof(PJ_DIAG_PREFIX " t=")-1,' ');
    pj_log_add(&diag_log,t,"diag",rest?rest+1:line);
}
/* Reports a newly begun close, a phase change and the DONE reason, once each. */
static void diag_changes(void) {
    char body[PJ_DIAG_MAX];
    if(launcher.close_reason!=watch.close_reason) {
        watch.close_reason=launcher.close_reason;
        pj_diag_close(body,sizeof(body),&launcher);diag(body);
    }
    if(launcher.phase!=watch.phase) {
        pj_diag_phase(body,sizeof(body),watch.phase,&launcher);diag(body);
        watch.phase=(uint8_t)launcher.phase;
    }
    if(launcher.phase==PJ_L_DONE&&!watch.done_reported) {
        watch.done_reported=1;
        pj_diag_done(body,sizeof(body),&launcher);diag(body);
    }
}
static void feed_watchdog(void) {
    wdt_hal_context_t rtc={.inst=WDT_RWDT,.rwdt_dev=&RTCCNTL};
    wdt_hal_write_protect_disable(&rtc);wdt_hal_feed(&rtc);wdt_hal_write_protect_enable(&rtc);
}
static void send_fpga(unsigned address,uint16_t value) {
    uint8_t message[8];
    /* The stock FPGA receiver only accepts V2 (0x8F) messages; never follow rx.v1. */
    size_t size=pj_fpga_encode(message,0,address,value);
    uart_write_bytes(UART_NUM_1,message,size);
    if(address==PJ_TX_POKE)counts.pokes++;else counts.polls++;
}
/* The media register (0x1D) of the stock+media FPGA, from ONE place: sent only when the wanted
 * bits change. On the stock FPGA nothing is ever wanted, so nothing is sent. */
static uint16_t media_sent=0;
static void media_bits(uint16_t want) {
    if(want==media_sent)return;
    if(pj_jl_rate_changing())return; /* 1.3.6: not while the link's UART rate changes; said on the next pass */
    media_sent=want;send_fpga(PJ_TX_MEDIA,want);
}
/* 1.3.1: the Pocket link ends (keyed leave at both rates, back to 115,200), then the media bits are said again on the
 * next pass, whatever the FPGA heard while the rates were changing, so the sound bit cannot be lost. */
static void plink_end(void) {
    pj_jl_end();
    if(link_fpga())media_sent=0xffffu; /* never a real value: media_bits sends the wanted bits next pass */
}
static void send_frame(void) {
    /* One transaction, reused: spi_device_transmit returns when the transfer is done, so the 45 static ones of
     * earlier launchers (about 1.8 KB of DRAM) were never in flight together. */
    spi_transaction_t trans;
    for(unsigned i=0;i<NUM_XFERS;i++) {
        memset(&trans,0,sizeof(trans));
        trans.tx_buffer=(const uint8_t *)frame+i*BYTES_PER_XFER;
        trans.length=BYTES_PER_XFER*8;
        trans.flags=SPI_TRANS_MODE_QIO;
        trans.cmd=0x400|1023;
        trans.addr=i*BYTES_PER_XFER;
        ESP_ERROR_CHECK(spi_device_transmit(spi,&trans));
    }
    counts.frames++;
}
/* The provisioning record (v1 or v2, newest of the two sectors): read, never written here. Heap for the two
 * sectors only during the call. */
static int read_record(pj_prov2 *r) {
    uint8_t *a=heap_caps_calloc(2,PJ_PROV2_SIZE,MALLOC_CAP_8BIT);
    if(!a)return 0;
    int ok=esp_flash_read(NULL,a,PJ_SLOT_OFFSET+PJ_PROVISION_OFFSET_A,PJ_PROV2_SIZE)==ESP_OK&&
           esp_flash_read(NULL,a+PJ_PROV2_SIZE,PJ_SLOT_OFFSET+PJ_PROVISION_OFFSET_B,PJ_PROV2_SIZE)==ESP_OK&&
           pj_provision_current(a,a+PJ_PROV2_SIZE,PJ_PROV2_SIZE,r);
    memset(a,0,2*PJ_PROV2_SIZE);heap_caps_free(a);
    return ok;
}
/* A line for the Jellyfin screen with a network name cut to fit (the screen draws it at x=12; ".." when cut),
 * the way Setup fits names. The name stays on the screen only. */
static void name_line(char *out,size_t size,const char *prefix,const char *name) {
    snprintf(out,size,"%s%s",prefix,name);
    size_t n=strlen(out);
    while(n>strlen(prefix)+1&&pj_osd_measure(out,pj_font_advance)>144){out[--n]=0;if(n>=2){out[n-1]='.';out[n-2]='.';}}
}
/* The saved networks back into `record` for a fresh join (join_task wipes them after joining); every other
 * secret is wiped at once. 1 when a record was read, 0 when none is valid, -1 when flash (or the 1.6 KB of heap
 * for the read) was unavailable: then nothing is known and nothing is changed. */
static int reload_networks(void) {
    pj_prov2 r;uint8_t *probe=heap_caps_calloc(2,PJ_PROV2_SIZE,MALLOC_CAP_8BIT);
    if(!probe)return -1;
    int readable=esp_flash_read(NULL,probe,PJ_SLOT_OFFSET+PJ_PROVISION_OFFSET_A,PJ_PROV2_SIZE)==ESP_OK;
    memset(probe,0,2*PJ_PROV2_SIZE);heap_caps_free(probe);
    if(!readable)return -1;
    if(!read_record(&r)){memset(&r,0,sizeof(r));return 0;}
    memset(r.chr1_key,0,sizeof(r.chr1_key));memset(r.gateway_token,0,sizeof(r.gateway_token));memset(r.gateway_cert_sha256,0,sizeof(r.gateway_cert_sha256));
    memset(r.access_token,0,sizeof(r.access_token));memset(r.url,0,sizeof(r.url));memset(r.user_id,0,sizeof(r.user_id));memset(r.device_id,0,sizeof(r.device_id));
    record=r;memset(&r,0,sizeof(r));return 1;
}
/* 1.3.6: trouble on UART1 (the FPGA's line) is counted instead of passing unseen; the Pocket link prints the counts
 * ("plink rx uart", docs/Design-notes.md "Launcher 1.3.6 link diagnostics"). The driver reports through
 * its event queue: the hardware FIFO overflowed (the driver then empties the FIFO: up to 128 bytes gone), the 4096-byte
 * receive ring was full, a parity error, a break. The queue holds PJ_UART1_EVENTS entries and the driver drops what
 * does not fit, so finding it full is counted too (evq: some events may be missing). Framing errors raise no
 * interrupt in this driver: their raw status bit is read and cleared here, so `frame` counts loop passes that saw
 * at least one. The receive interrupt now fires at PJ_UART1_RX_FULL bytes instead of the driver's 120 of 128: the
 * interrupt may be late by 80 bytes of line time (6.9 ms at 115,200, 1.5 ms at the fast rate) before the FIFO
 * overflows, not by 8 bytes (0.7 ms, 0.15 ms). A saturated line then makes about 240 data events a second at
 * 115,200 and about 1,090 at the fast rate, so the 16 entries cover a loop pass of about 67 ms at 115,200 and of only
 * about 15 ms at the fast rate. `evq` therefore means something at 115,200 only; at the fast rate it rises in
 * ordinary use and ovf/full may be under-counted (their events are dropped when the queue is full). The queue is
 * allocated at boot and stays: 16 entries of 12 bytes and the queue itself, about 0.3 KB of heap in every mode
 * (48 entries cost about 0.7 KB of a 13-17 KB playback margin: too much for what they told). */
#define PJ_UART1_EVENTS 16
#define PJ_UART1_RX_FULL 48
static QueueHandle_t uart1_events;static pj_pld_uart uart1_counts;
static void uart1_watch(void) {
    uart_event_t event;
    if(!uart1_events)return;
    if(uxQueueSpacesAvailable(uart1_events)==0)uart1_counts.evq++;
    while(xQueueReceive(uart1_events,&event,0)==pdTRUE)switch(event.type) {
    case UART_FIFO_OVF:uart1_counts.ovf++;break;
    case UART_BUFFER_FULL:uart1_counts.full++;break;
    case UART_PARITY_ERR:uart1_counts.parity++;break;
    case UART_FRAME_ERR:uart1_counts.frame++;break;
    case UART_BREAK:uart1_counts.brk++;break;
    default:break; /* UART_DATA: the bytes are read by the loop */
    }
    if(uart_ll_get_intraw_mask(&UART1)&UART_INTR_FRAM_ERR){uart_ll_clr_intsts_mask(&UART1,UART_INTR_FRAM_ERR);uart1_counts.frame++;}
}
/* UART1 (the FPGA's line) as it is: every byte waiting goes to the launcher's own parser (buttons, version) and, while
 * the Pocket link runs, to the link's parser beside it (1.3.0). 1.4.0: a function, read at the start of the loop pass as
 * before and again at its end while the link runs (pj_jl_pump). */
static void uart1_read(uint32_t now) {
    uint8_t bytes[128];
    int count;
    while((count=uart_read_bytes(UART_NUM_1,bytes,sizeof(bytes),0))>0) {
        counts.uart_bytes+=(uint32_t)count;
        for(int i=0;i<count;i++) {
            uint16_t value;
            if(pj_fpga_rx_byte(&rx,bytes[i],&value)) {
                counts.messages++;counts.last_buttons=value;
                pj_rmt_physical(&rmt,value,now);
                pj_launch_buttons(&launcher,value,now);
                diag_changes();
            }
        }
        if(pj_jl_active())pj_jl_rx(bytes,count,now); /* 1.3.0: the link bridge's status packets, beside the parser above */
    }
}
static void buses_init(void) {
    const uart_config_t uart={.baud_rate=115200,.data_bits=UART_DATA_8_BITS,.parity=UART_PARITY_DISABLE,
        .stop_bits=UART_STOP_BITS_1,.flow_ctrl=UART_HW_FLOWCTRL_DISABLE,.source_clk=UART_SCLK_DEFAULT};
    /* With the overlay open the FPGA streams about 11.5 KB/s; the loop drains it all. */
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_1,4096,1024,PJ_UART1_EVENTS,&uart1_events,0)); /* 1.3.6: with its event queue */
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_1,&uart));
    ESP_ERROR_CHECK(uart_set_pin(UART_NUM_1,PIN_UART_TO_FPGA,PIN_UART_FROM_FPGA,-1,-1));
    ESP_ERROR_CHECK(uart_set_rx_full_threshold(UART_NUM_1,PJ_UART1_RX_FULL));
    /* Console UART: RX ring buffer only. No TX buffer and no VFS switch, so printf keeps
     * writing the FIFO directly and the boot markers are unchanged. */
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0,4096,0,0,NULL,0));
    const spi_bus_config_t bus={.miso_io_num=PIN_QSPI_MISO,.mosi_io_num=PIN_QSPI_MOSI,.sclk_io_num=PIN_QSPI_CLK,
        .quadwp_io_num=PIN_QSPI_WP,.quadhd_io_num=PIN_QSPI_HD,.max_transfer_sz=BYTES_PER_XFER*8+46,
        .flags=SPICOMMON_BUSFLAG_QUAD|SPICOMMON_BUSFLAG_MASTER};
    const spi_device_interface_config_t device={.clock_speed_hz=40*1000*1000,.mode=0,.spics_io_num=PIN_QSPI_CS,
        .queue_size=4,.flags=SPI_DEVICE_HALFDUPLEX,.cs_ena_pretrans=3,.cs_ena_posttrans=3,
        .command_bits=11,.address_bits=32,.dummy_bits=3};
    ESP_ERROR_CHECK(spi_bus_initialize(VSPI_HOST,&bus,SPI_DMA_CH_AUTO));
    ESP_ERROR_CHECK(spi_bus_add_device(VSPI_HOST,&device,&spi));
}
/* Checks that the radio receives: counts nearby networks, joins none, stores nothing.
 * Used when no provisioning record exists. Runs only when the user asks (A), because a
 * game is running under the overlay and shares the battery. The scan is passive (it
 * never transmits) and transmit power is capped low, and the radio is switched off
 * again on every path. */
/* The netif layer, the default event loop and the station interface are made once per boot and kept: the
 * radio check, the join and the on-console setup's scan may each be the first to need them, so "already
 * made" is not a failure and the station interface is never made twice. */
static esp_err_t wifi_base(void) {
    esp_err_t e=esp_netif_init();if(e!=ESP_OK&&e!=ESP_ERR_INVALID_STATE)return e;
    e=esp_event_loop_create_default();if(e!=ESP_OK&&e!=ESP_ERR_INVALID_STATE)return e;
    if(!esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"))esp_netif_create_default_wifi_sta(); /* asserts on failure: a panic restarts into stock */
    return ESP_OK;
}
static void radio_task(void *unused) {
    (void)unused;
    wifi_init_config_t config=WIFI_INIT_CONFIG_DEFAULT();
    config.nvs_enable=0;
    uint16_t found=0;
    int ok=0;
    wifi_scan_config_t scan={.scan_type=WIFI_SCAN_TYPE_PASSIVE,.scan_time.passive=120};
    esp_err_t err=ESP_OK;
    /* Notes the first failing step for the diagnostics line; same calls, same order. */
#define RADIO_STEP(name,call) (radio_step=(name),(err=(call))==ESP_OK)
    if(RADIO_STEP("netif",wifi_base())) {
        if(RADIO_STEP("init",esp_wifi_init(&config))) {
            if(RADIO_STEP("storage",esp_wifi_set_storage(WIFI_STORAGE_RAM))&&
               RADIO_STEP("mode",esp_wifi_set_mode(WIFI_MODE_STA))&&RADIO_STEP("start",esp_wifi_start())) {
                radio_started=1;
                esp_wifi_set_max_tx_power(8); /* 2 dBm; the passive scan sends nothing anyway */
                ok=RADIO_STEP("scan",esp_wifi_scan_start(&scan,true))&&RADIO_STEP("count",esp_wifi_scan_get_ap_num(&found));
                if(ok)radio_step="none";
                esp_wifi_clear_ap_list();
                esp_wifi_stop();
            }
            esp_wifi_deinit();
            radio_started=0; /* deinitialised: a network saved in the setup later joins on close */
        }
    }
    radio_ok=ok;radio_err=(int)err;radio_networks=found;
    if(!ok)snprintf(radio_line,sizeof(radio_line),"Wi-Fi: check failed");
    else if(!found)snprintf(radio_line,sizeof(radio_line),"Wi-Fi: no networks heard");
    else snprintf(radio_line,sizeof(radio_line),"Wi-Fi ok: %u networks",(unsigned)found);
    radio_running=0;radio_done=1;launcher.redraw=1;
    vTaskDelete(NULL);
}
static void on_wifi(void *unused,esp_event_base_t base,int32_t id,void *data) {
    (void)unused;
    if(base==WIFI_EVENT&&id==WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *d=data;
        disconnect_reason=d?d->reason:0;connect_pending=0; /* the reason stays a number: pj_join_result turns it into words */
        xEventGroupSetBits(wifi_events,WIFI_LOST);
    }
    if(base==IP_EVENT&&id==IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *got=data;
        own_ip=got->ip_info.ip.addr;own_mask=got->ip_info.netmask.addr;connect_pending=0;xEventGroupSetBits(wifi_events,WIFI_GOT_IP);
    }
}
/* The one place the driver is asked to join (the first join, every retry after a drop): the network is the
 * driver's own current config. */
static esp_err_t wifi_connect(void){connect_pending=1;return esp_wifi_connect();}
/* Serves CHR1 on TCP 38771: one client at a time, private addresses on our own subnet. */
static void lan_task(void *unused) {
    (void)unused;
    int listener=socket(AF_INET,SOCK_STREAM,IPPROTO_IP),client=-1;
    struct sockaddr_in at={.sin_family=AF_INET,.sin_port=htons(PJ_CHR1_PORT),.sin_addr.s_addr=htonl(INADDR_ANY)};
    if(listener<0||bind(listener,(struct sockaddr *)&at,sizeof(at))||listen(listener,1)){if(listener>=0)close(listener);lan_alive=0;vTaskDelete(NULL);}
    uint8_t bytes[1024];
    /* Runs until the hand-over or a teardown asks it to leave (lan_quit). A dropped link only closes the client:
     * the listener (bound to any address) outlives the reconnect, so Remote Pilot can come back. */
    while(!radio_stop&&!lan_quit) {
        if(client>=0&&wifi_state!=2){close(client);client=-1;lan_connected=0;lan_closed=1;}
        fd_set set;FD_ZERO(&set);FD_SET(listener,&set);int top=listener;
        if(client>=0){FD_SET(client,&set);if(client>top)top=client;}
        struct timeval wait={0,20000};
        if(select(top+1,&set,NULL,NULL,&wait)>0) {
            if(FD_ISSET(listener,&set)) {
                struct sockaddr_in peer;socklen_t size=sizeof(peer);
                int fresh=accept(listener,(struct sockaddr *)&peer,&size);
                uint8_t ip[4];memcpy(ip,&peer.sin_addr.s_addr,4);
                int local=fresh>=0&&pj_prov_private_ipv4(ip)&&(peer.sin_addr.s_addr&own_mask)==(own_ip&own_mask);
                if(fresh>=0&&(client>=0||lan_closed||!local))close(fresh); /* one client, own subnet only */
                else if(fresh>=0){client=fresh;xStreamBufferReset(lan_rx);xStreamBufferReset(lan_tx);lan_connected=1;lan_opened=1;}
            }
            if(client>=0&&FD_ISSET(client,&set)) {
                size_t space=xStreamBufferSpacesAvailable(lan_rx);
                int got=space?recv(client,bytes,space<sizeof(bytes)?space:sizeof(bytes),0):1;
                if(got<=0){close(client);client=-1;lan_connected=0;lan_closed=1;}
                else if(space)xStreamBufferSend(lan_rx,bytes,(size_t)got,0);
            }
        }
        if(client>=0) {
            size_t n;
            while((n=xStreamBufferReceive(lan_tx,bytes,sizeof(bytes),0))>0)if(send(client,bytes,n,0)<0)break;
            if(lan_close_request&&!xStreamBufferBytesAvailable(lan_tx)) {
                close(client);client=-1;lan_connected=0;lan_close_request=0;lan_closed=1;
            }
        }
    }
    if(client>=0){close(client);lan_connected=0;lan_closed=1;}
    close(listener);
    lan_alive=0;
    vTaskDelete(NULL);
}
/* Joins the provisioned network (only after the owner pressed A on the Jellyfin screen)
 * and starts the CHR1 server. RAM storage: nothing about the network is ever written. */
static void join_task(void *unused) {
    (void)unused;
    wifi_init_config_t config=WIFI_INIT_CONFIG_DEFAULT();
    config.nvs_enable=0;
    esp_err_t err=ESP_OK;int ok=0,timed_out=0;
    pj_join_tally tally;pj_join_tally_begin(&tally); /* the last network's tries: their words if no network joins */
    join_state=JOIN_JOINING;join_fail=0;joined_ssid[0]=0;
    if(!wifi_events)wifi_events=xEventGroupCreate(); /* one per boot: lan_task and the handler use it across rejoins */
    /* A rejoin: the driver from the last join goes first (disconnect, stop, deinit), so the join below is the same
     * from a cold start and from a switch. The netif, event loop and station interface stay (wifi_base). */
    if(radio_started){esp_wifi_disconnect();esp_wifi_stop();esp_wifi_deinit();radio_started=0;}
    xEventGroupClearBits(wifi_events,WIFI_GOT_IP|WIFI_LOST);connect_pending=0;
    if(RADIO_STEP("netif",wifi_base())) {
        esp_event_handler_register(WIFI_EVENT,WIFI_EVENT_STA_DISCONNECTED,on_wifi,NULL); /* same handler and argument: registered once */
        esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,on_wifi,NULL);
        if(RADIO_STEP("init",esp_wifi_init(&config))&&RADIO_STEP("storage",esp_wifi_set_storage(WIFI_STORAGE_RAM))&&
           RADIO_STEP("mode",esp_wifi_set_mode(WIFI_MODE_STA))&&RADIO_STEP("start",esp_wifi_start())) {
            radio_started=1;
            /* One quick scan first (wifi_order.h): the saved networks heard now are tried first, in saved order (the list
             * is the owner's priority list), then the rest in saved order (hidden ones never answer a scan). No flash
             * write: worked out again every join. */
            uint8_t order[PJ_PROV2_NETWORKS];
            for(unsigned i=0;i<PJ_PROV2_NETWORKS;i++)order[i]=(uint8_t)i;
            {
                wifi_scan_config_t scan={.scan_type=WIFI_SCAN_TYPE_ACTIVE,.scan_time.active.min=40,.scan_time.active.max=120};
                uint16_t found=0;
                if(record.count>1&&!radio_stop&&esp_wifi_scan_start(&scan,true)==ESP_OK&&esp_wifi_scan_get_ap_num(&found)==ESP_OK&&found) {
                    if(found>PJ_WIFI_HEARD_MAX)found=PJ_WIFI_HEARD_MAX;
                    wifi_ap_record_t *aps=heap_caps_calloc(found,sizeof(wifi_ap_record_t),MALLOC_CAP_8BIT);
                    pj_wifi_heard *heard=heap_caps_calloc(found,sizeof(pj_wifi_heard),MALLOC_CAP_8BIT);
                    if(aps&&heard&&esp_wifi_scan_get_ap_records(&found,aps)==ESP_OK) {
                        const char *names[PJ_PROV2_NETWORKS];
                        for(unsigned i=0;i<record.count;i++)names[i]=record.net[i].ssid;
                        for(unsigned i=0;i<found&&i<PJ_WIFI_HEARD_MAX;i++) {
                            heard[i].len=(uint8_t)strnlen((const char *)aps[i].ssid,32);
                            memcpy(heard[i].ssid,aps[i].ssid,heard[i].len);heard[i].rssi=aps[i].rssi;
                        }
                        join_heard=pj_wifi_join_order(names,record.count,heard,found,order);
                    }
                    if(aps){memset(aps,0,found*sizeof(wifi_ap_record_t));heap_caps_free(aps);}
                    if(heard){memset(heard,0,found*sizeof(pj_wifi_heard));heap_caps_free(heard);}
                }
                esp_wifi_clear_ap_list();
            }
            /* The provisioned networks in that order, 15 s each, until one gives an address. 1.3.4: a network whose
             * handshake or authentication failed is tried again (up to PJ_JOIN_ATTEMPTS, 2 s then 5 s apart,
             * wifi_scan.h): right after a power-on the access point may still hold the console's last association, and
             * one such failure was shown as "The password is wrong" although the password was right. */
            for(unsigned t=0;t<record.count&&!ok&&!radio_stop;t++) {
                unsigned i=order[t];join_try=t+1;
                pj_join_tally_begin(&tally);
                for(;;) {
                    wifi_config_t join;memset(&join,0,sizeof(join));
                    memcpy(join.sta.ssid,record.net[i].ssid,strlen(record.net[i].ssid));
                    memcpy(join.sta.password,record.net[i].psk,strlen(record.net[i].psk));
                    join.sta.threshold.authmode=record.net[i].psk[0]?WIFI_AUTH_WPA2_PSK:WIFI_AUTH_OPEN;
                    xEventGroupClearBits(wifi_events,WIFI_GOT_IP|WIFI_LOST);disconnect_reason=0;
                    ok=RADIO_STEP("config",esp_wifi_set_config(WIFI_IF_STA,&join))&&RADIO_STEP("connect",wifi_connect());
                    memset(&join,0,sizeof(join));
                    int asked=ok;
                    EventBits_t bits=ok?xEventGroupWaitBits(wifi_events,WIFI_GOT_IP|WIFI_LOST,pdFALSE,pdFALSE,pdMS_TO_TICKS(15000)):0;
                    timed_out=ok&&!(bits&(WIFI_GOT_IP|WIFI_LOST));
                    ok=ok&&(bits&WIFI_GOT_IP)&&!(bits&WIFI_LOST);
                    if(ok)break;
                    esp_wifi_disconnect();
                    pj_join_tally_add(&tally,asked?disconnect_reason:0,0,timed_out);
                    if(!asked||radio_stop||!pj_join_retry(&tally))break;
                    snprintf(radio_line,sizeof(radio_line),"%s",PJ_JOIN_RETRY_LINE);launcher.redraw=1;
                    vTaskDelay(pdMS_TO_TICKS(PJ_JOIN_RETRY_MS(tally.attempts)));
                    if(radio_stop)break;
                }
                if(ok)memcpy(joined_ssid,record.net[i].ssid,sizeof(joined_ssid)); /* name only, for Setup's list; never printed */
            }
            /* The driver keeps its own copy of the joined network; ours goes. */
            memset(record.net,0,sizeof(record.net));
            if(ok){radio_step="none";xEventGroupClearBits(wifi_events,WIFI_LOST);}
            else if(err==ESP_OK)radio_step="no-address";
        }
    }
#undef RADIO_STEP
    radio_ok=ok;radio_err=(int)err;radio_networks=0;
    if(ok) {
        uint8_t ip[4];memcpy(ip,(const void *)&own_ip,4);
        name_line(radio_line,sizeof(radio_line),"Wi-Fi: ",joined_ssid); /* the screen only: never a diagnostics line */
        snprintf(remote_line,sizeof(remote_line),"Remote Pilot: %u.%u.%u.%u",ip[0],ip[1],ip[2],ip[3]);
        if(!lan_rx)lan_rx=xStreamBufferCreate(1024,1);
        if(!lan_tx)lan_tx=xStreamBufferCreate(4096,1);
        wifi_state=2;link_lost=0;join_state=JOIN_CONNECTED;
        lan_quit=0;lan_alive=1;
        if(!lan_rx||!lan_tx||xTaskCreate(lan_task,"chr1-lan",4096,NULL,1,NULL)!=pdPASS){lan_alive=0;wifi_state=3;}
    } else {
        /* In plain words for Setup's Switching screen and the Jellyfin screen (counts only in the diagnostics). */
        join_fail=tally.attempts?pj_join_verdict(&tally):pj_join_result(disconnect_reason,0,timed_out,0); /* 1.3.4: wrong password only after every try failed authentication */
        wifi_state=3;join_state=JOIN_FAILED;snprintf(radio_line,sizeof(radio_line),"Wi-Fi: could not join");
        if(radio_started)esp_wifi_stop();
    }
    radio_running=0;radio_done=1;launcher.redraw=1;
    vTaskDelete(NULL);
}
/* Reached only if the bootloader fell forward from a stock image that failed its
 * check: restarting would come straight back here. Say so and wait for the PC tool. */
static void stock_failed(void) {
    pj_osd_fill(frame,0x101010u);
    pj_osd_text(frame,8,20,"Stock firmware did not",0xff6666u,pj_font_alpha,pj_font_advance);
    pj_osd_text(frame,8,34,"pass its check.",0xff6666u,pj_font_alpha,pj_font_advance);
    pj_osd_text(frame,8,62,"Connect USB and run",0xd0d0d0u,pj_font_alpha,pj_font_advance);
    pj_osd_text(frame,8,76,"Chromatic Manager:",0xd0d0d0u,pj_font_alpha,pj_font_advance);
    pj_osd_text(frame,8,90,"Restore stock.",0xd0d0d0u,pj_font_alpha,pj_font_advance);
    send_frame();
    pj_rmt_shutdown(&rmt);usb_finish();
    {
        char body[PJ_DIAG_MAX];
        pj_diag_handover(body,sizeof(body),&launcher,1,now_ms());diag(body);
    }
    printf("PJ-LAUNCHER-STOCK-INVALID\n");fflush(stdout);
    for(;;){feed_watchdog();vTaskDelay(pdMS_TO_TICKS(500));}
}
void app_main(void) {
    printf("\n" PJ_LAUNCHER_MARKER "\n");fflush(stdout);
    /* The bootloader starts this app on a power-on reset. Any other reason means it
     * fell forward because stock failed validation. */
    reset_reason=esp_reset_reason();
    int stock_suspect=reset_reason!=ESP_RST_POWERON;
    char body[PJ_DIAG_MAX];
    pj_diag_boot(body,sizeof(body),PJ_LAUNCHER_VERSION,(unsigned)reset_reason,stock_suspect,esp_get_free_heap_size());diag(body);
    buses_init();
    {
        /* MAC and flash ID: only for the salted svc-id digest; never printed. */
        uint8_t mac[6]={0};uint32_t flash_id=0;
        if(esp_efuse_mac_get_default(mac)!=ESP_OK)memset(mac,0,sizeof(mac));
        if(esp_flash_read_id(NULL,&flash_id)!=ESP_OK)flash_id=0;
        pj_svc_init(&svc,mac,flash_id,(unsigned)efuse_hal_get_major_chip_version(),flash_read,NULL);
        memset(mac,0,sizeof(mac));
        /* The provisioning record, v1 or v2, newest of the two sectors (written only by
         * Chromatic Manager): read, never written. */
        record_valid=read_record(&record); /* heap for the two sector reads during the call only (1.2.9: was 1.6 KB static) */
        record_wifi=record_valid&&record.count>0;
        record_jellyfin=record_wifi&&record.has_jellyfin;
        if(record_jellyfin) {
            snprintf(jf_creds.url,sizeof(jf_creds.url),"%s",record.url);snprintf(jf_creds.user,sizeof(jf_creds.user),"%s",record.user_id);
            snprintf(jf_creds.token,sizeof(jf_creds.token),"%s",record.access_token);snprintf(jf_creds.device,sizeof(jf_creds.device),"%s",record.device_id);
            snprintf(jf_creds.version,sizeof(jf_creds.version),"%s",PJ_LAUNCHER_VERSION);
        }
        if(record_wifi)strcpy(radio_line,"Wi-Fi: connecting...");
    }
    uint32_t now=now_ms();
    pj_launch_init(&launcher,now);
    /* Which FPGA is this? Stock answers the version request once even with the overlay closed. The
     * stock+media build (debug bit, minor 40) then gets its media register before the first poke:
     * overlay allowed without a cartridge, the Menu button to us while our screen is up. Stock: nothing. */
    send_fpga(PJ_TX_VERSION_REQUEST,0);
    for(uint32_t t0=now_ms();!rx.version_seen&&(uint32_t)(now_ms()-t0)<250u;) {
        uint8_t b[64];uint16_t v;int n=uart_read_bytes(UART_NUM_1,b,sizeof(b),pdMS_TO_TICKS(10));
        for(int i=0;i<n;i++)if(pj_fpga_rx_byte(&rx,b[i],&v)){counts.messages++;counts.last_buttons=v;}
        uart1_watch();
        feed_watchdog();
    }
    /* Chromatic Manager's status check reads this line (version register: debug bit 12, minor 11..6, major 5..0). */
    fpga_version_line(body,sizeof(body));diag(body);
    uint16_t fpga_said=rx.version;uint8_t fpga_said_seen=rx.version_seen;uint32_t fpga_said_ms=now_ms();
    now=now_ms();
    /* True random numbers for nonces and pairing keys while the radio is off (esp_random
     * alone is only pseudo-random then). Switched off before Wi-Fi starts, which then
     * supplies the entropy itself. Uses SAR ADC and I2S0, which this launcher does not. */
    bootloader_random_enable();
    /* The stock-invalid screen is for Chromatic Manager's esptool restore: no remote there. */
    pj_rmt_init(&rmt,PJ_LAUNCHER_VERSION,record_valid&&record.has_gateway?record.chr1_key:NULL,&launcher,&svc,frame,
                &diag_log,random32,facts,NULL);
    pj_jf_set_pump(io_pump);
    {   /* 1.3.2: the USB drain, every 5 ms from the esp_timer task */
        const esp_timer_create_args_t drain={.callback=usb_drain,.name="usb-drain"};
        if(esp_timer_create(&drain,&usb_drain_timer)!=ESP_OK||esp_timer_start_periodic(usb_drain_timer,PJ_UTX_DRAIN_MS*1000u)!=ESP_OK)
            usb_drain_timer=NULL; /* never expected; then the main loop drains below, as before 1.3.2 */
    }
    /* Only the Wi-Fi networks stay in RAM; the key now lives in the handler alone. */
    memset(record.chr1_key,0,sizeof(record.chr1_key));memset(record.gateway_token,0,sizeof(record.gateway_token));
    memset(record.gateway_cert_sha256,0,sizeof(record.gateway_cert_sha256));
    memset(record.access_token,0,sizeof(record.access_token));memset(record.url,0,sizeof(record.url));
    memset(record.user_id,0,sizeof(record.user_id));memset(record.device_id,0,sizeof(record.device_id));
    watch.phase=(uint8_t)launcher.phase;
    uint32_t last_stats=now,pass_start=now;
    int radio_reported=0,svc_begun=0,svc_ended=0,menu_marked=0;
    for(;;) {
        now=now_ms();loop_ms=now-pass_start;pass_start=now;
        int64_t chr1_t0=esp_timer_get_time();
        /* While a video plays, Remote Pilot's live frames are held to one per PJ_RMT_VIDEO_SLOW_MS (remote.h):
         * each raw frame is 46 KB over USB at 115200 baud or over the Wi-Fi the stream needs. Buttons still work. */
        rmt.video_slow=(uint8_t)(pj_jf_playing()||pj_jl_streaming()); /* 1.4.0: also while a title plays over the Pocket link */
        if(!stock_suspect)usb_receive(now);
        if(lan_rx) { /* also after Wi-Fi was lost, so the LAN link always closes */
            uint8_t bytes[256];size_t n;
            if(lan_opened){lan_opened=0;pj_rmt_lan_open(&rmt,now);}
            while((n=xStreamBufferReceive(lan_rx,bytes,sizeof(bytes),0))>0)for(size_t i=0;i<n;i++)pj_rmt_rx(&rmt,&rmt.lan,bytes[i],now);
            if(lan_closed||(!lan_connected&&(rmt.lan.state!=PJ_LS_IDLE||pj_rmt_busy_output(&rmt.lan)))) {
                lan_closed=0;pj_rmt_lan_closed(&rmt);
            }
        }
        pj_svc_poll(&svc,now);
        pj_rmt_poll(&rmt,now);
        uint32_t chr1_us=(uint32_t)(esp_timer_get_time()-chr1_t0);
        uart1_watch(); /* 1.3.6: UART1 overflow, full ring, break and framing errors, counted */
        /* Drain everything waiting, so decisions use the newest reading, not a backlog. */
        uart1_read(now);
        /* Presses for the Jellyfin app arrive through the launcher (physical buttons and Remote
         * Pilot alike); a bouncing contact must not count twice (Back went up two levels). */
        if(launcher.app_pressed) {
            uint16_t pressed=launcher.app_pressed,keep=0;launcher.app_pressed=0;
            for(unsigned b=0;b<16;b++)if(pressed&(1u<<b)) {
                if((uint32_t)(now-jf_press_at[b])>=120u)keep|=(uint16_t)(1u<<b);
                jf_press_at[b]=now;
            }
            if(keep&&pj_jf_active()&&launcher.phase==PJ_L_MEDIA)pj_jf_press(keep);
            else if(keep&&pj_jl_active()&&launcher.phase==PJ_L_MEDIA&&(keep&(PJ_BTN_B|PJ_BTN_MENU))) {
                /* B (or Menu) on the Pocket link screen: the link mode ends, the cable is the game's again, Jellyfin returns */
                plink_end();launcher.app_owns_media=0;launcher.app_pressed=0;launcher.redraw=1;
                snprintf(body,sizeof(body),"plink end heap=%lu",(unsigned long)esp_get_free_heap_size());diag(body);
            }
        }
        int jf_closing=pj_jf_poll(); /* frees the last session's memory once its tasks are gone */
        if(launcher.phase!=PJ_L_MEDIA) {
            menu_setup_wanted=0;plink_wanted=0;find_wanted=0;
            if(pj_jl_active()){plink_end();launcher.app_owns_media=0;diag("plink end left-media");} /* never left running off its screen */
        }
#if PJ_ENABLE_SETUP
        /* SETUP HOOK begin: the on-console setup while it is open (Select on the Jellyfin screen, app not running). */
        _Static_assert((int)JOIN_FAILED==(int)PJ_SU_JOIN_FAILED&&(int)JOIN_CONNECTED==(int)PJ_SU_JOIN_CONNECTED&&(int)JOIN_JOINING==(int)PJ_SU_JOIN_JOINING,"join states");
        if(launcher.phase!=PJ_L_MEDIA)setup_refused=0;
        /* The Jellyfin menu's "Wi-Fi & sign-in" row: the app has closed and let go of its memory; now Setup opens. */
        if(menu_setup_wanted&&launcher.phase==PJ_L_MEDIA&&!pj_jf_active()&&!jf_closing&&!launcher.setup_open) {
            menu_setup_wanted=0;launcher.setup_open=1;launcher.setup_pressed=0;launcher.redraw=1;
        }
        if(launcher.setup_open&&launcher.phase==PJ_L_MEDIA) {
            if(!setup) {
                pj_setup_account account;memset(&account,0,sizeof(account));
                if(record_jellyfin){snprintf(account.url,sizeof(account.url),"%s",jf_creds.url);snprintf(account.user_id,sizeof(account.user_id),"%s",jf_creds.user);
                    snprintf(account.device_id,sizeof(account.device_id),"%s",jf_creds.device);snprintf(account.token,sizeof(account.token),"%s",jf_creds.token);}
                setup=pj_setup_esp32_open(record_jellyfin?&account:NULL,wifi_state==2,joined_ssid,PJ_LAUNCHER_VERSION);
                memset(&account,0,sizeof(account));
                if(!setup){launcher.setup_open=0;launcher.setup_pressed=0;setup_refused=1;find_wanted=0;launcher.redraw=1;} /* not enough heap: the screen says so */
                else {
                    setup_refused=0;
                    if(find_wanted){find_wanted=0;pj_setup_find_begin(setup,1);} /* 1.3.0: straight to Find my server again */
                }
            }
            if(setup) {
                if(launcher.setup_pressed){uint16_t p=launcher.setup_pressed;launcher.setup_pressed=0;pj_setup_press(setup,p,now,pj_setup_esp32_random);launcher.redraw=1;}
                if(pj_setup_esp32_service(setup,now,radio_running,wifi_state==2,joined_ssid,join_state,join_fail))launcher.redraw=1;
                /* "Use this network now" was committed: the join runs again now, while Setup shows each step. */
                if(pj_setup_take_switch(setup)){rejoin_wanted=1;rejoin_from_setup=1;record_wifi=1;join_state=JOIN_JOINING;} /* JOINING now: the screen must not read the last join's result */
                if(setup->screen==PJ_SU_CLOSED&&!pj_setup_esp32_busy()) {
                    int switched=rejoin_from_setup; /* this Setup session already asked for a join (Use this network now / a save tried) */
                    launcher.setup_open=0;launcher.redraw=1;rejoin_from_setup=0;
                    if(setup->jellyfin_changed) {
                        pj_setup_account fresh;
                        if(pj_setup_take_fresh(setup,&fresh)) {
                            snprintf(jf_creds.url,sizeof(jf_creds.url),"%s",fresh.url);snprintf(jf_creds.user,sizeof(jf_creds.user),"%s",fresh.user_id);
                            snprintf(jf_creds.token,sizeof(jf_creds.token),"%s",fresh.token);snprintf(jf_creds.device,sizeof(jf_creds.device),"%s",fresh.device_id);
                            snprintf(jf_creds.version,sizeof(jf_creds.version),"%s",PJ_LAUNCHER_VERSION);record_jellyfin=record_wifi||setup->net_count>0;
                        } else{memset(&jf_creds,0,sizeof(jf_creds));record_jellyfin=0;}
                        memset(&fresh,0,sizeof(fresh));jf_failed=0;
                    }
                    if(setup->wifi_changed) {
                        /* The saved list changed. Not connected (never joined, a failed join, a drop): join the new list
                         * now, no restart. Connected: stay, unless the network we are on was forgotten. */
                        record_wifi=setup->net_count>0;
                        int still_saved=0;for(unsigned i=0;i<setup->net_count;i++)if(!strcmp(setup->ssid[i],joined_ssid))still_saved=1;
                        if(switched){} /* that join is running or done with this list already: no second one */
                        else if(record_wifi&&(wifi_state!=2||!still_saved)){rejoin_wanted=1;strcpy(radio_line,"Wi-Fi: rejoining...");}
                        else if(!record_wifi&&radio_started){rejoin_wanted=1;} /* every network forgotten: the radio goes off */
                    }
                    pj_setup_esp32_close(setup);setup=NULL; /* wiped and freed; nothing about the record is logged */
                }
            }
        }
        /* SETUP HOOK end */
#endif
        /* ---- Wi-Fi after the first join: a dropped link, and rejoining without a restart ---- */
        if(wifi_events&&!radio_running) {
            EventBits_t bits=xEventGroupGetBits(wifi_events);
            if(wifi_state==2&&(bits&WIFI_LOST)) { /* the joined network dropped us: the driver does not come back by itself */
                xEventGroupClearBits(wifi_events,WIFI_GOT_IP|WIFI_LOST);
                wifi_state=1;link_lost=1;link_lost_ms=now;link_tries=0;link_retry_at=now;join_state=JOIN_JOINING;
                strcpy(radio_line,"Wi-Fi dropped: rejoining...");launcher.redraw=1;pj_jf_link(0);
                snprintf(body,sizeof(body),"wifi lost reason=%d",disconnect_reason);diag(body); /* a number, never a name */
            } else if(link_lost) {
                if(bits&WIFI_GOT_IP) { /* back on the same network */
                    uint8_t ip[4];memcpy(ip,(const void *)&own_ip,4);
                    xEventGroupClearBits(wifi_events,WIFI_LOST);link_lost=0;wifi_state=2;join_state=JOIN_CONNECTED;
                    name_line(radio_line,sizeof(radio_line),"Wi-Fi: ",joined_ssid);snprintf(remote_line,sizeof(remote_line),"Remote Pilot: %u.%u.%u.%u",ip[0],ip[1],ip[2],ip[3]);
                    launcher.redraw=1;pj_jf_link(1);
                    snprintf(body,sizeof(body),"wifi back tries=%u after-ms=%lu",link_tries,(unsigned long)(now-link_lost_ms));diag(body);
                } else if(connect_pending&&(uint32_t)(now-link_retry_at)>=20000u)connect_pending=0; /* no event at all: do not wait forever */
                else if(!connect_pending&&(int32_t)(now-link_retry_at)>=0) {
                    if(link_tries>=4&&record.count>1&&!pj_jf_active()&&!pj_jl_active()&&!jf_closing&&!PJ_SETUP_IS_OPEN) {
                        /* Four quick tries failed and another network is saved: a full join (scan, priority order). */
                        link_lost=0;rejoin_wanted=1;
                    } else {
                        link_tries++;xEventGroupClearBits(wifi_events,WIFI_LOST);
                        link_retry_at=now+(link_tries<=4?1000u<<(link_tries-1):10000u); /* at once, then 1, 2, 4, 8 s, then every 10 s */
                        if(wifi_connect()!=ESP_OK)connect_pending=0;
                    }
                }
            }
        }
        /* A fresh join after a clean teardown (Setup changed the list / Use this network now / a drop that quick retries
         * could not fix): only with the Jellyfin app gone (its sockets and tasks are the network's users), the LAN
         * task gone and its CHR1 session closed, then the radio task takes the driver down and up again. */
        if(rejoin_wanted&&!radio_running&&!pj_jf_active()&&!pj_jl_active()&&!jf_closing&&!PJ_SETUP_IS_BUSY&&(int32_t)(now-rejoin_retry_at)>=0) {
            if(lan_alive)lan_quit=1; /* asked to leave; waited for below */
            else {
                if(lan_rx||lan_tx){pj_rmt_lan_closed(&rmt);lan_opened=lan_closed=lan_close_request=0;lan_connected=0;}
                if(lan_rx){vStreamBufferDelete(lan_rx);lan_rx=NULL;}
                if(lan_tx){vStreamBufferDelete(lan_tx);lan_tx=NULL;}
                lan_quit=0;link_lost=0;
                int loaded=record_wifi?reload_networks():0;
                if(loaded<0) { /* flash or heap unavailable for the read: nothing is known, nothing is wiped; again in 2 s */
                    rejoin_retry_at=now+2000u;strcpy(radio_line,"Wi-Fi: settings busy, retrying");launcher.redraw=1;
                    diag("wifi rejoin read-failed");
                } else if(loaded>0&&record.count>0) {
                    rejoin_wanted=0;radio_done=0;wifi_state=1;join_state=JOIN_JOINING;strcpy(radio_line,"Wi-Fi: rejoining...");launcher.redraw=1;
                    snprintf(body,sizeof(body),"wifi rejoin networks=%u",(unsigned)record.count);diag(body);
                } else { /* nothing saved any more: the driver goes off and stays off; the sign-in stays (it is still on flash) */
                    rejoin_wanted=0;record_wifi=0;
                    if(radio_started){esp_wifi_disconnect();esp_wifi_stop();esp_wifi_deinit();radio_started=0;}
                    wifi_state=0;join_state=JOIN_IDLE;radio_done=0;joined_ssid[0]=0;strcpy(radio_line,"Wi-Fi: press A to check");launcher.redraw=1;
                }
            }
        }
        /* 1.3.9: while the Pocket link runs, a silent FPGA is given PJ_LAUNCH_LINK_SILENT_MS on the Media screen (the link's
         * own recovery from a silent bridge takes longer than 1.5 s; launcher_logic.h) */
        launcher.media_link=(uint8_t)(pj_jl_active()!=0);
        pj_launch_action action=pj_launch_step(&launcher,now);
        /* Holds the restart only while a session needs the launcher after an opening
         * failure, and leads every service end back to stock. */
        pj_rmt_link(&rmt,&action,now);
        if(svc.state!=PJ_SVC_OFF&&!svc_begun){svc_begun=1;pj_svc_diag_begin(body,sizeof(body),&svc);diag(body);}
        if(svc.state==PJ_SVC_ENDED&&!svc_ended) {
            svc_ended=1;
            if(svc.screen==PJ_SVC_SCREEN_SHOWN&&svc.end_reason!=PJ_SVC_END_OK&&svc.end_reason!=PJ_SVC_END_FAIL) {
                pj_launch_draw_handover(frame,pj_font_alpha,pj_font_advance);send_frame();
            }
            pj_svc_diag_end(body,sizeof(body),&svc,now);diag(body);
        }
        diag_changes();
        /* Once, when the menu first shows: Chromatic Manager's boot check after an install
         * accepts this, since the menu no longer goes on to Games by itself. */
        if(launcher.phase==PJ_L_MENU&&!menu_marked&&!stock_suspect){menu_marked=1;if(!usb_quiet())printf("PJ-LAUNCHER-MENU\n");}
        if(action.prime_frames) {
            /* The FPGA ignores the menu line until it has seen two frames. */
            pj_launch_draw_menu(frame,&launcher,now,pj_font_alpha,pj_font_advance);
            send_frame();send_frame();
        }
        /* Media FPGA (Version message: debug bit, minor 40): overlay allowed without a cartridge from
         * the start, the Menu button to us (and the game held paused, its sound off) while our screen
         * is up, sound only behind jf_sound; all cleared at the hand-over below. Stock FPGA: never sent. */
        {
            int media_fpga=rx.version_seen&&pj_fpga_media_audio(rx.version);
            int ours=(launcher.phase==PJ_L_MENU||launcher.phase==PJ_L_MEDIA||launcher.phase==PJ_L_OTHER)&&launcher.menu_open;
            launcher.menu_to_us=(uint8_t)(media_fpga&&ours);
            media_bits(media_fpga?(uint16_t)(PJ_MEDIA_OVERLAY|(ours?PJ_MEDIA_MENU:0)|(jf_sound?PJ_MEDIA_AUDIO:0)):0);
        }
        if(action.poke)send_fpga(PJ_TX_POKE,action.poke_value);
        /* 1.3.6: not in the one or two passes between the link's request for the fast UART and the
         * MCU's own change of rate: the bridge may be at the new rate already and this would reach it at the old one.
         * The next request follows 100 ms later; the gateway ends that state within 200 ms whatever happens. */
        if(action.request_buttons&&!pj_jl_rate_changing())send_fpga(PJ_TX_BUTTONS_REQUEST,0);
        if(action.restart_to_stock) {
            /* Sessions end and the radio goes off before stock runs. The Pocket link first (1.3.0): its keyed leave hands
             * the cable back to the game core and the UART is back at 115,200 before anything else is sent. */
            if(pj_jl_active())plink_end();
            pj_rmt_shutdown(&rmt);usb_finish(); /* 1.3.2: the ring's last bytes out, then the drain stops */
            media_bits(0);jf_sound=0;launcher.menu_to_us=0; /* media bits off before stock (its SysCtrl clears them too) */
            uart_wait_tx_done(UART_NUM_1,pdMS_TO_TICKS(50));
            radio_stop=1;
            if(radio_started){if(radio_running)esp_wifi_scan_stop();esp_wifi_disconnect();esp_wifi_stop();}
            if(stock_suspect)stock_failed();
            /* The overlay is closed now; if the menu button is pressed before stock draws,
             * this is what shows, not a stale menu. */
            pj_launch_draw_handover(frame,pj_font_alpha,pj_font_advance);send_frame();
            /* Last diagnostics: nothing of ours is printed after the marker, which
             * Chromatic Manager's boot watch relies on. */
            pj_diag_stats(body,sizeof(body),&counts,&launcher,esp_get_free_heap_size(),esp_get_minimum_free_heap_size());
            diag(body);
            pj_diag_handover(body,sizeof(body),&launcher,0,now_ms());diag(body);
            printf("PJ-LAUNCHER-TO-STOCK\n");fflush(stdout);
            esp_restart();
        }
        /* Saved Wi-Fi joins as soon as Jellyfin opens; without it A checks the radio as before. */
        /* Not while the on-console setup is open: its scan may be using the driver; the join starts when it closes. */
        if(pj_radio_start_wanted(&launcher,record_wifi,radio_running,radio_done,PJ_SETUP_IS_OPEN,rejoin_from_setup,rejoin_wanted,PJ_SETUP_IS_BUSY)) {
            radio_running=1;launcher.redraw=1;
            if(record_wifi){strcpy(radio_line,"Wi-Fi: joining...");wifi_state=1;}
            else strcpy(radio_line,"Wi-Fi: listening...");
            bootloader_random_disable(); /* must be off before the radio starts */
            /* Same priority as this loop, so Wi-Fi start-up cannot starve the watchdog feed. */
            if(xTaskCreate(record_wifi?join_task:radio_task,"radio",6144,NULL,1,NULL)!=pdPASS) {
                strcpy(radio_line,"Wi-Fi: check failed");radio_running=0;radio_done=1;wifi_state=record_wifi?3:0;
            }
        }
        if(launcher.phase!=PJ_L_MEDIA)jf_failed=0; /* opening Media again gets fresh tries */
        /* Not under an open setup: it holds its own memory and the buttons until it is closed. */
        if(launcher.phase==PJ_L_MEDIA&&record_jellyfin&&wifi_state==2&&!pj_jf_active()&&!jf_closing&&!PJ_SETUP_IS_OPEN&&!menu_setup_wanted&&!rejoin_wanted&&
           !plink_wanted&&!pj_jl_active()&&(int32_t)(now-plink_hold_until)>=0&&
           jf_failed<3&&(!jf_failed||(int32_t)(now-jf_retry_at)>=0)) {
            plink_refused=0;
            pj_jf_set_pocket_link(link_fpga()); /* 1.3.0: the menu offers Pocket link only on the link FPGA */
            /* Sound only on the stock+media FPGA, told apart by its version message (debug bit, minor 40);
             * on stock the stream and behaviour are exactly as before. The media bit also mutes the
             * hidden cartridge while Jellyfin is open; stock ignores the address. */
            jf_sound=rx.version_seen&&pj_fpga_media_audio(rx.version);
            int started=pj_jf_begin(&jf_creds,frame,now,jf_sound);
            if(started!=-6) { /* -6: the last session is still letting go, try next pass */
                snprintf(body,sizeof(body),"jellyfin begin=%d heap=%lu sound=%d",started,(unsigned long)esp_get_free_heap_size(),jf_sound);diag(body);
                if(started==0){jf_failed=0;launcher.app_owns_media=1;launcher.app_pressed=0;jf_last_stats=now;} /* media_bits sends the sound bit next pass */
                else { /* up to 3 tries, 2 s apart (memory freed by a closing task may come back) */
                    jf_failed++;jf_retry_at=now+2000u;
                    strcpy(radio_line,started==-5||started==-2?"Jellyfin: not enough memory":"Jellyfin could not start");launcher.redraw=1;
                }
            }
        }
        if(pj_jf_active()&&pj_jf_wants_exit()) {
            int to_setup=PJ_ENABLE_SETUP&&pj_jf_exit_to_setup(); /* the menu's "Wi-Fi & sign-in" row: Setup once the app has let go */
            int to_find=PJ_ENABLE_SETUP&&pj_jf_exit_to_find();   /* 1.3.0: "Can't reach": Find my server again */
            int to_plink=pj_jf_exit_to_plink()&&link_fpga();      /* 1.3.0: the menu's "Pocket link" row (link FPGA only) */
            pj_jf_end();
            jf_sound=0; /* media_bits drops the sound bit next pass; the overlay and Menu bits stay ours */
            snprintf(body,sizeof(body),"jellyfin end heap=%lu setup=%d",(unsigned long)esp_get_free_heap_size(),to_setup);diag(body);
            launcher.app_owns_media=0;launcher.app_pressed=0;launcher.redraw=1;
            if(to_find){snprintf(body,sizeof(body),"jellyfin find-server");diag(body);}
            if(to_setup||to_find){menu_setup_wanted=1;find_wanted=to_find;} /* stays on the Jellyfin screen ("Opening Setup..."); the hook opens Setup once the app has let go */
            else if(to_plink)plink_wanted=1; /* "Opening Pocket link..."; it starts once the app's memory is back */
            else pj_launch_enter(&launcher,PJ_L_MENU,now);
        }
        /* 1.3.0: the Pocket link starts once the app has let go (pj_jf_poll freed it), on the link FPGA only. */
        if(plink_wanted&&launcher.phase==PJ_L_MEDIA&&!pj_jf_active()&&!jf_closing&&!PJ_SETUP_IS_OPEN) {
            int started=link_fpga()&&record_jellyfin&&wifi_state==2?pj_jl_begin(&jf_creds,now):-1;
            if(started!=-6) { /* -6: still letting go, next pass */
                plink_wanted=0;
                snprintf(body,sizeof(body),"plink begin=%d heap=%lu block=%lu pieces=%u+%u+%u+%u",started,(unsigned long)esp_get_free_heap_size(),
                         (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL),
                         (unsigned)sizeof(pj_plink),(unsigned)PJ_PLINK_BUF,(unsigned)PJ_PLINK_RESPONSE,(unsigned)sizeof(pj_plink_scratch));diag(body); /* 1.3.7: the gateway's four heap pieces */
                if(started==0){launcher.app_owns_media=1;launcher.app_pressed=0;plink_stats_ms=now;plink_drawn_ms=now;launcher.redraw=1;
                    uart1_watch();memset(&uart1_counts,0,sizeof(uart1_counts));} /* 1.3.6: the UART counts are of this link session */
                else{plink_refused=started==-5||started==-2?1:2;plink_hold_until=now+4000u;launcher.redraw=1;} /* said on the screen, then Jellyfin */
            }
        }
        if(pj_jl_active()) {
            pj_jl_tick(now);
            pj_plink_status v;pj_jl_view(&v,now);
            int blinks=v.what==PJ_PLV_WAITING||v.what==PJ_PLV_SYNCING;
            if(v.what!=plink_shown.what||v.done!=plink_shown.done||v.total!=plink_shown.total||(blinks&&(uint32_t)(now-plink_drawn_ms)>=500u))launcher.redraw=1;
            if((uint32_t)(now-plink_stats_ms)>=5000u) {
                uint32_t st,lv,fa,fs,sm;pj_jl_counts(&st,&lv,&fa,&fs,&sm);plink_stats_ms=now;
                snprintf(body,sizeof(body),"plink state=%lu view=%u level=%lu faults=%lu fast=%lu stream=%lu heap=%lu heap-min=%lu block=%lu",
                         (unsigned long)st,(unsigned)v.what,(unsigned long)lv,(unsigned long)fa,(unsigned long)fs,(unsigned long)sm,
                         (unsigned long)esp_get_free_heap_size(),(unsigned long)esp_get_minimum_free_heap_size(),
                         (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT|MALLOC_CAP_INTERNAL));
                diag(body);
                if(pj_jl_net(body,sizeof(body)))diag(body); /* 1.3.8: where the heap goes (TIME_WAIT, connections, lowest) */
                uart1_watch(); /* 1.4.0: what the stream got, and UART1's trouble since the last such line */
                if(pj_jl_speed(body,sizeof(body),now,&uart1_counts,0)){diag(body);if(pj_jl_speed(body,sizeof(body),now,&uart1_counts,1))diag(body);}
                plink_rx_line=0;
            }
            /* 1.3.6: what the link heard from the Pocket. After the 5 s line the "plink rx ..." lines follow, each
             * only when it has news and one a loop pass (a full line is about 13-17 ms of the console's 115,200
             * baud). Otherwise, at once and at most one a second: a completed capture of the Pocket's first bytes,
             * a refused frame. All of that is for a link that does NOT work yet (waiting, unplugged, stopped). Once
             * it is connected, syncing or playing, pj_plink_say_periodic/_event (pocket_link.h) say one short line
             * every 30 s, no capture or ring bytes, and a refused frame at most once in 10 s: the console must not
             * hold the loop up while the cable streams. */
            else if(plink_rx_line<PJ_PLD_PERIODIC) {
                while(plink_rx_line<PJ_PLD_PERIODIC)if(pj_jl_diag_periodic(body,sizeof(body),now,plink_rx_line++,&uart1_counts)){diag(body);break;}
            }
            else if(pj_jl_diag_event(body,sizeof(body),now))diag(body);
        }
        {   /* 1.3.2: which audio/subtitle track the stream asked for, once per play and again when it changes
             * ("jellyfin tracks audio=2 lang=eng of=2 subs=-1": counts and codes only, never a title) */
            static char tracks_said[96];char tracks[96];
            if(!(pj_jf_active()&&pj_jf_tracks(tracks,sizeof(tracks))))tracks_said[0]=0;
            else if(strcmp(tracks,tracks_said)){snprintf(tracks_said,sizeof(tracks_said),"%s",tracks);diag(tracks);}
        }
        if(pj_jf_active()&&(uint32_t)(now-jf_last_stats)>=5000u) {
            pj_jf_status_t st;pj_jf_status(&st);jf_last_stats=now;
            snprintf(body,sizeof(body),"jellyfin screen=%u playing=%u shown=%lu dropped=%lu rate=%lu pos=%lu art=%lu heap=%lu heap-min=%lu block=%lu stack=%lu/%lu/%lu",
                     st.screen,st.playing,(unsigned long)st.shown,(unsigned long)st.dropped,(unsigned long)st.bitrate,
                     (unsigned long)st.position,(unsigned long)st.art,(unsigned long)esp_get_free_heap_size(),
                     (unsigned long)esp_get_minimum_free_heap_size(),(unsigned long)st.largest,
                     (unsigned long)st.http_stack,(unsigned long)st.stream_stack,(unsigned long)st.sound_stack);
            diag(body);
            /* Sound: 0 none, 1 playing and the clock, 2 on but the frame clock rules, 3 sink dead. starved: quality
             * step-downs because the sound starved (the link cannot carry the stream); catchup: pictures skipped to
             * keep up with the sound; cap: the bitrate that starved (never earned back this session). */
            if(st.sound||jf_sound) {
                snprintf(body,sizeof(body),"jellyfin sound=%u played=%lu underruns=%lu fpga=%d starved=%lu catchup=%lu cap=%lu",st.sound,
                         (unsigned long)st.sound_played,(unsigned long)st.sound_underruns,jf_sound,(unsigned long)st.sound_starved,
                         (unsigned long)st.sound_catchup,(unsigned long)st.sound_rate_cap);
                diag(body);
                /* The sound path itself (since the last line): ring fill min/max in bytes of a 2048 B ring, the longest gap
                 * between ring writes, the sound task's longest wait on an empty ring, its longest I2S write, DMA dry events
                 * with the ring empty (feed starved) / with sound queued (task late), finished DMA buffers of 128 frames
                 * (x128/5 s = the real I2S rate, 16000 expected), frames held in the DMA at this moment. */
                if(st.sound)
                    snprintf(body,sizeof(body),"jellyfin ring=%lu/%lu gap=%lu wait=%lu i2s=%lu dry=%lu/%lu eofs=%lu indma=%lu",
                             (unsigned long)st.sound_ring_min,(unsigned long)st.sound_ring_max,(unsigned long)st.sound_gap_ms,
                             (unsigned long)st.sound_wait_ms,(unsigned long)st.sound_i2s_ms,(unsigned long)st.sound_dry_ring,
                             (unsigned long)st.sound_dry_task,(unsigned long)st.sound_eofs,(unsigned long)st.sound_in_dma),diag(body);
            }
            /* Second line: what limits playback (network vs decoder); see docs/Design-notes.md. chr1: Remote
             * Pilot's cost in this loop since the last line, longest pass / total in ms, and live frames encoded. */
            uint32_t chr1_frames=rmt.video_seq-chr1_frames_mark;chr1_frames_mark=rmt.video_seq;
            snprintf(body,sizeof(body),"jellyfin play net=%lu dec=%lu/%lu/%lu fps=%lu cycle=%lu rebased=%lu slowed=%lu chr1=%lu/%lu/%lu",
                     (unsigned long)st.net_bytes,(unsigned long)st.decodes,(unsigned long)st.decode_ms_avg,
                     (unsigned long)st.decode_ms_max,(unsigned long)st.fps_cap,(unsigned long)st.cycle_ms,
                     (unsigned long)st.rebased,(unsigned long)st.slowed,(unsigned long)((chr1_us_max+999u)/1000u),
                     (unsigned long)((chr1_us_sum+999u)/1000u),(unsigned long)chr1_frames);
            chr1_us_max=chr1_us_sum=0;
            diag(body);
        }
        if(radio_done&&!radio_reported) {
            radio_reported=1;
            pj_diag_wifi(body,sizeof(body),radio_ok,radio_networks,radio_step,radio_err,esp_err_to_name(radio_err));
            diag(body);
            if(join_try){snprintf(body,sizeof(body),"wifi join heard=%u try=%u fail=%d",join_heard,join_try,join_state==JOIN_FAILED?join_fail:0);diag(body);} /* counts only, never names */
            if(join_state==JOIN_FAILED&&join_fail>PJ_JOIN_OK&&join_fail<=PJ_JOIN_FAILED)snprintf(radio_line,sizeof(radio_line),"Wi-Fi: %.32s",pj_join_words[join_fail]);
        }
        if(!radio_done)radio_reported=0; /* a rejoin reports again */
        if((uint32_t)(now-last_stats)>=PJ_DIAG_STATS_MS) {
            last_stats=now;
            pj_diag_stats(body,sizeof(body),&counts,&launcher,esp_get_free_heap_size(),esp_get_minimum_free_heap_size());
            diag(body);
        }
        /* 1.3.5: the FPGA version line again, every 5 s and at once when the reported version changed (the overlay
         * streams the Version message while it is open, so a chip reconfigured under this launcher shows up here). */
        if((rx.version_seen&&(!fpga_said_seen||rx.version!=fpga_said))||(uint32_t)(now-fpga_said_ms)>=PJ_FPGA_LINE_MS) {
            fpga_said=rx.version;fpga_said_seen=rx.version_seen;fpga_said_ms=now;
            fpga_version_line(body,sizeof(body));diag(body);
        }
        /* 1.3.5: the Pocket link's stage, state and reason, one line per change (jellyfin_esp32.c pj_jl_trace). */
        if(pj_jl_trace(body,sizeof(body)))diag(body);
        if(stock_suspect&&launcher.phase==PJ_L_MENU)stock_failed(); /* overlay is open: shown */
        static int was_pairing;
        if(was_pairing&&!rmt.pairing)launcher.redraw=1; /* the menu comes back after pairing */
        was_pairing=rmt.pairing;
        static int rmt_had_screen;
        {int owns=pj_rmt_owns_screen(&rmt);if(rmt_had_screen&&!owns)pj_jf_invalidate();rmt_had_screen=owns;}
        if(pj_rmt_owns_screen(&rmt)) {
            launcher.redraw=0; /* the menu stays hidden under the pairing or service screen */
            if(rmt.pairing) {
                static uint32_t pair_second=~0u,pair_for=~0u;
                uint32_t left=(PJ_RMT_PAIR_MS-(now-rmt.pair_ms))/1000u;
                if(pair_for!=rmt.pair_ms){pair_for=rmt.pair_ms;pair_second=~0u;}
                if(left!=pair_second&&pj_svc_screen_of(&launcher)==PJ_SVC_SCREEN_SHOWN) {
                    pair_second=left;pj_pair_draw(frame,rmt.pair_code,left,pj_font_alpha,pj_font_advance);send_frame();
                }
            } else if(pj_svc_wants_frame(&svc)) {
                svc.redraw=0;
                pj_svc_draw(frame,&svc,pj_font_alpha,pj_font_advance);send_frame();
                /* After this frame nothing is drawn again: it is what stays on screen while
                 * the ESP32 sits in its ROM bootloader for the PC's write. */
                if(svc.state==PJ_SVC_FROZEN)pj_svc_frame_sent(&svc);
            }
        } else if(pj_jl_active()&&launcher.phase==PJ_L_MEDIA&&!launcher.close_hint) { /* 1.3.0: the Pocket link screen */
            if(launcher.redraw) {
                launcher.redraw=0;pj_jl_view(&plink_shown,now);plink_drawn_ms=now;
                pj_plink_draw(frame,&plink_shown,now,pj_font_alpha,pj_font_advance);send_frame();
            }
        } else if(pj_jf_active()&&launcher.phase==PJ_L_MEDIA&&!launcher.close_hint) {
            launcher.redraw=0;
            if(pj_jf_tick(frame,now))send_frame();
        } else if(launcher.redraw) {
            launcher.redraw=0;
            if(launcher.close_hint)pj_launch_draw_closing(frame,pj_font_alpha,pj_font_advance);
#if PJ_ENABLE_SETUP
            else if(launcher.phase==PJ_L_MEDIA&&launcher.setup_open&&setup) /* SETUP HOOK: the setup screens */
                pj_setup_draw(frame,setup,rmt.usb.state==PJ_LS_READY||rmt.lan.state==PJ_LS_READY,now,pj_font_alpha,pj_font_advance);
            else if(launcher.phase==PJ_L_MEDIA) {
                const char *const lines[3]={radio_line,wifi_state==2?remote_line:
                    pairing?"Paired with PC":"Remote Pilot: USB",
                    /* SETUP HOOK: the console can do this itself now (Select) */
                    setup_refused?"Not enough memory for setup now":
                    menu_setup_wanted?"Opening Setup...":
                    plink_wanted?"Opening Pocket link...":
                    plink_refused==1?"Pocket link: not enough memory":plink_refused?"Pocket link could not start":
                    !record_wifi?"Select: set up Wi-Fi here":!record_jellyfin?"Select: sign in to Jellyfin":
                    link_lost||rejoin_wanted||wifi_state==1?"Back in a moment":
                    wifi_state==3?"Select: check Wi-Fi settings":"Opening Jellyfin..."};
#else
            else if(launcher.phase==PJ_L_MEDIA) {
                const char *const lines[3]={radio_line,wifi_state==2?remote_line:
                    pairing?"Paired with PC":"Remote Pilot: USB",
                    plink_wanted?"Opening Pocket link...":
                    plink_refused==1?"Pocket link: not enough memory":plink_refused?"Pocket link could not start":
                    !record_wifi?"Set up Wi-Fi in Chromatic Manager":!record_jellyfin?"Link Jellyfin in Chromatic Manager":
                    link_lost||rejoin_wanted||wifi_state==1?"Back in a moment":
                    wifi_state==3?"Jellyfin needs Wi-Fi":"Opening Jellyfin..."};
#endif
                pj_launch_draw_media(frame,lines,pj_font_alpha,pj_font_advance);
            } else if(launcher.phase==PJ_L_OTHER)pj_launch_draw_other(frame,&launcher,pj_font_alpha,pj_font_advance);
            else if(launcher.credits_open)pj_launch_draw_credits(frame,pj_font_alpha,pj_font_advance);
            else pj_launch_draw_menu(frame,&launcher,now,pj_font_alpha,pj_font_advance);
            send_frame();
        }
        /* Outside a USB session only short text lines are owed: send them whole. */
        chr1_t0=esp_timer_get_time();
        usb_pump(rmt.usb.state==PJ_LS_IDLE);
        if(!usb_drain_timer)usb_drain(NULL); /* no timer (never expected): one FIFO fill per pass, as before 1.3.2 */
        lan_pump();
        chr1_us+=(uint32_t)(esp_timer_get_time()-chr1_t0);
        if(chr1_us>chr1_us_max)chr1_us_max=chr1_us;
        chr1_us_sum+=chr1_us;
        /* 1.4.0: while the Pocket link runs, the end of the pass reads UART1 again and lets the gateway send what the
         * bridge's newest statuses allow (jellyfin_esp32.c pj_jl_pump): a pass made long by its other work still feeds
         * the cable twice. Not in the pass that asked for the fast rate (pj_jl_pump says nothing then). */
        if(pj_jl_active()){uint32_t end=now_ms();uart1_watch();uart1_read(end);pj_jl_pump(end);}
        work_ms=now_ms()-now;
        feed_watchdog();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
