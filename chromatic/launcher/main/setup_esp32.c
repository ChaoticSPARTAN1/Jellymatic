/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * ESP32 side of the on-console setup. The screens (shared/setup_ui.h) never touch hardware;
 * this file does, one job at a time, in a worker task of its own so the main loop keeps
 * feeding the watchdog. Results are handed to the screens only from the main task
 * (pj_setup_esp32_service), so the setup state is touched by one task.
 *
 * Memory: every buffer (the pj_setup itself, scan and discovery results, HTTP strings, UDP reply,
 * HTTP body, the commit's sector buffers) is heap-allocated while the setup is open or the job
 * runs, and freed, wiped, when it closes. Static DRAM of this file: a handful of pointers and ints.
 *
 * Jobs:
 *  - scan: an active Wi-Fi scan (names, signal, locked/open) through the driver the shell
 *    already started, or one started here with RAM storage and no NVS and stopped again after.
 *  - discover: "who is JellyfinServer?" to the broadcast address, UDP 7359, replies for 2.5 s.
 *  - http: one Quick Connect request (shared/quickconnect.h decides which), plain http on the
 *    LAN as the Jellyfin app itself; the Authorization header is built by quickconnect.h, the
 *    token only ever goes into that header.
 *  - commit: the single audited provisioning write (shared/provision_write.c), run inline on
 *    the main task; the "Do not switch off" screen is already up.
 * Nothing here logs a name, password, code, secret or token. */
#include <stdio.h>
#include <string.h>
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_random.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "setup_esp32.h"
#include "provision_write.h"
#define PJ_SETUP_FLASH_SLOT 0x260000u
#define PJ_SETUP_DISCOVER_MS 2500u
#define PJ_SETUP_HTTP_TIMEOUT_MS 8000
#define PJ_SETUP_WORKER_STACK 8192
enum {JOB_NONE,JOB_SCAN,JOB_DISCOVER,JOB_HTTP};
/* Everything a job needs, allocated with the setup. */
typedef struct {
    pj_scan_list scan;pj_jf_servers servers;
    char http_url[200+129],http_auth[320],http_method[8],http_req_body[160];
    uint8_t reply[1024];
    int http_status;size_t http_len;uint8_t *http_body; /* body: allocated per request */
} pj_setup_work;
static pj_setup_work *work;
static volatile int job,job_done,job_running,job_ok;
static volatile int radio_busy;static uint32_t last_redraw;

/* Both provisioning sectors (812 bytes each suffice to parse), on the heap for the call. */
static int read_record(pj_prov2 *r) {
    uint8_t *a=heap_caps_calloc(2,PJ_PROV2_SIZE,MALLOC_CAP_8BIT);
    if(!a)return 0;
    uint8_t *b=a+PJ_PROV2_SIZE;
    int ok=esp_flash_read(NULL,a,PJ_SETUP_FLASH_SLOT+PJ_PROVISION_OFFSET_A,PJ_PROV2_SIZE)==ESP_OK&&
           esp_flash_read(NULL,b,PJ_SETUP_FLASH_SLOT+PJ_PROVISION_OFFSET_B,PJ_PROV2_SIZE)==ESP_OK&&
           pj_provision_current(a,b,PJ_PROV2_SIZE,r);
    memset(a,0,2*PJ_PROV2_SIZE);heap_caps_free(a);
    return ok;
}
void pj_setup_esp32_random(uint8_t out[12]){esp_fill_random(out,12);}
int pj_setup_esp32_busy(void){return job_running;}

pj_setup *pj_setup_esp32_open(const pj_setup_account *account,int wifi_connected,const char *connected_ssid,const char *version) {
    if(job_running||work)return NULL;
    if(esp_get_free_heap_size()<PJ_SETUP_HEAP_MIN)return NULL;
    pj_setup *s=heap_caps_calloc(1,sizeof(pj_setup),MALLOC_CAP_8BIT);
    work=heap_caps_calloc(1,sizeof(pj_setup_work),MALLOC_CAP_8BIT);
    if(!s||!work){if(s)heap_caps_free(s);if(work){heap_caps_free(work);work=NULL;}return NULL;}
    pj_prov2 r;const char *names[PJ_PROV2_NETWORKS];unsigned count=0;int linked=0;
    if(read_record(&r)){for(unsigned i=0;i<r.count;i++)names[count++]=r.net[i].ssid;linked=r.has_jellyfin;}
    pj_setup_open(s,names,count,linked&&account!=NULL,account,wifi_connected,version);
    pj_setup_set_connected(s,wifi_connected?connected_ssid:NULL);
    memset(&r,0,sizeof(r));
    return s;
}
void pj_setup_esp32_close(pj_setup *s) {
    if(work) {
        if(work->http_body){memset(work->http_body,0,PJ_QC_BODY_MAX);heap_caps_free(work->http_body);}
        memset(work,0,sizeof(*work));heap_caps_free(work);work=NULL;
    }
    if(s){memset(s,0,sizeof(*s));heap_caps_free(s);}
    job=JOB_NONE;job_done=0;
}
/* ---- worker jobs ---- */
static void job_scan(void) {
    wifi_init_config_t config=WIFI_INIT_CONFIG_DEFAULT();
    config.nvs_enable=0;
    int started_here=0,ok=0;
    wifi_scan_config_t scan={.scan_type=WIFI_SCAN_TYPE_ACTIVE,.scan_time.active.min=60,.scan_time.active.max=150};
    pj_scan_reset(&work->scan);
    /* The shell's radio check or join owns the driver while it runs (and starts no new one while the
     * setup is open): the scan then says it did not work and can be tried again. */
    if(radio_busy)goto out;
    /* Use the driver as it really is, not as a flag remembers it: not initialised (never started, or
     * the shell's radio check already deinitialised it), initialised but stopped (a join that failed),
     * or running (joined). Whatever this job changes it puts back. */
    wifi_mode_t mode;
    esp_err_t e=esp_wifi_get_mode(&mode);
    if(e==ESP_ERR_WIFI_NOT_INIT) {
        /* Same start as the shell's radio check: RAM storage, no NVS, stopped and deinitialised below.
         * The netif layer, event loop and station interface stay for the shell (wifi_base there). */
        e=esp_netif_init();if(e!=ESP_OK&&e!=ESP_ERR_INVALID_STATE)goto out;
        e=esp_event_loop_create_default();if(e!=ESP_OK&&e!=ESP_ERR_INVALID_STATE)goto out;
        if(!esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"))esp_netif_create_default_wifi_sta();
        if(esp_wifi_init(&config)!=ESP_OK)goto out;
        started_here=2;
        if(esp_wifi_set_storage(WIFI_STORAGE_RAM)!=ESP_OK||esp_wifi_set_mode(WIFI_MODE_STA)!=ESP_OK||esp_wifi_start()!=ESP_OK)goto done;
    } else if(e!=ESP_OK)goto out;
    e=esp_wifi_scan_start(&scan,true);
    if(e==ESP_ERR_WIFI_NOT_STARTED&&!started_here) {
        if(esp_wifi_start()!=ESP_OK)goto out;
        started_here=1;
        e=esp_wifi_scan_start(&scan,true);
    }
    if(e==ESP_OK) {
        uint16_t n=0;
        if(esp_wifi_scan_get_ap_num(&n)==ESP_OK) {
            if(n>24)n=24;
            wifi_ap_record_t *records=heap_caps_calloc(n?n:1,sizeof(wifi_ap_record_t),MALLOC_CAP_8BIT);
            if(records&&esp_wifi_scan_get_ap_records(&n,records)==ESP_OK) {
                for(unsigned i=0;i<n;i++)pj_scan_add(&work->scan,records[i].ssid,(unsigned)strnlen((const char *)records[i].ssid,32),
                                                    records[i].rssi,records[i].authmode!=WIFI_AUTH_OPEN);
                ok=1;
            }
            if(records){memset(records,0,n*sizeof(wifi_ap_record_t));heap_caps_free(records);}
        }
        esp_wifi_clear_ap_list();
    }
done:
    if(started_here)esp_wifi_stop();
    if(started_here==2)esp_wifi_deinit();
out:
    job_ok=ok;
}
static void job_discover(void) {
    memset(&work->servers,0,sizeof(work->servers));
    int sock=socket(AF_INET,SOCK_DGRAM,IPPROTO_IP);
    if(sock<0){job_ok=0;return;}
    int yes=1;setsockopt(sock,SOL_SOCKET,SO_BROADCAST,&yes,sizeof(yes));
    struct timeval wait={0,300000};setsockopt(sock,SOL_SOCKET,SO_RCVTIMEO,&wait,sizeof(wait));
    struct sockaddr_in to={.sin_family=AF_INET,.sin_port=htons(PJ_DISCOVERY_PORT),.sin_addr.s_addr=htonl(INADDR_BROADCAST)};
    static const char ask[]=PJ_DISCOVERY_MESSAGE;
    sendto(sock,ask,sizeof(ask)-1,0,(struct sockaddr *)&to,sizeof(to));
    TickType_t until=xTaskGetTickCount()+pdMS_TO_TICKS(PJ_SETUP_DISCOVER_MS);
    while((int32_t)(until-xTaskGetTickCount())>0) {
        struct sockaddr_in from;socklen_t size=sizeof(from);
        int n=recvfrom(sock,work->reply,sizeof(work->reply)-1,0,(struct sockaddr *)&from,&size);
        if(n>0)pj_discovery_add(&work->servers,work->reply,(size_t)n);
        if((int32_t)(until-xTaskGetTickCount())>(int32_t)pdMS_TO_TICKS(1200)&&work->servers.heard==0)
            sendto(sock,ask,sizeof(ask)-1,0,(struct sockaddr *)&to,sizeof(to)); /* ask again until someone answers */
    }
    memset(work->reply,0,sizeof(work->reply));
    close(sock);job_ok=1;
}
static void job_http(void) {
    work->http_status=0;work->http_len=0;
    esp_http_client_config_t config={.url=work->http_url,.timeout_ms=PJ_SETUP_HTTP_TIMEOUT_MS,.buffer_size=1024,.buffer_size_tx=1024,
        .disable_auto_redirect=true,.method=!strcmp(work->http_method,"POST")?HTTP_METHOD_POST:HTTP_METHOD_GET};
    esp_http_client_handle_t client=esp_http_client_init(&config);
    if(!client)return;
    esp_http_client_set_header(client,"Authorization",work->http_auth);
    esp_http_client_set_header(client,"Accept","application/json");
    size_t body=strlen(work->http_req_body);
    if(body)esp_http_client_set_header(client,"Content-Type","application/json");
    if(esp_http_client_open(client,(int)body)==ESP_OK&&(!body||esp_http_client_write(client,work->http_req_body,(int)body)==(int)body)&&
       esp_http_client_fetch_headers(client)>=0) {
        int status=esp_http_client_get_status_code(client);size_t got=0;
        for(;;) {
            int n=esp_http_client_read(client,(char *)work->http_body+got,(int)(PJ_QC_BODY_MAX-got));
            if(n<=0)break;
            got+=(size_t)n;
            if(got>=PJ_QC_BODY_MAX){status=-2;break;} /* too big for the console: an unexpected reply */
        }
        work->http_len=got;work->http_status=status;
    }
    esp_http_client_close(client);esp_http_client_cleanup(client);
}
static void worker(void *unused) {
    (void)unused;
    switch(job){case JOB_SCAN:job_scan();break;case JOB_DISCOVER:job_discover();break;case JOB_HTTP:job_http();break;default:break;}
    job_done=1;job_running=0;
    vTaskDelete(NULL);
}
static int start_job(int which) {
    job=which;job_done=0;job_ok=0;job_running=1;
    if(xTaskCreate(worker,"setup",PJ_SETUP_WORKER_STACK,NULL,1,NULL)!=pdPASS){job_running=0;job=JOB_NONE;return 0;}
    return 1;
}
/* ---- main task ---- */
int pj_setup_esp32_service(pj_setup *s,uint32_t now,int radio_busy_now,int wifi_connected,const char *connected_ssid,int join_state,int join_fail) {
    int redraw=0;
    if(!s||!work)return 0;
    radio_busy=radio_busy_now;
    if(s->wifi_connected!=(uint8_t)(wifi_connected!=0)){s->wifi_connected=(uint8_t)(wifi_connected!=0);redraw=1;} /* a join finished while open */
    {int8_t before=s->connected;pj_setup_set_connected(s,wifi_connected?connected_ssid:NULL);if(s->connected!=before)redraw=1;}
    {uint8_t before=s->switch_state;pj_setup_join_state(s,join_state,join_fail);if(s->switch_state!=before)redraw=1;}
    if(job_done) { /* a worker finished: deliver on this task */
        job_done=0;int which=job;job=JOB_NONE;
        if(which==JOB_SCAN){s->scan=work->scan;memset(&work->scan,0,sizeof(work->scan));pj_setup_scan_done(s,job_ok);}
        else if(which==JOB_DISCOVER){s->servers=work->servers;memset(&work->servers,0,sizeof(work->servers));pj_setup_discover_done(s);}
        else if(which==JOB_HTTP) {
            pj_setup_http_done(s,work->http_status,work->http_body,work->http_len,now);
            if(work->http_body){memset(work->http_body,0,PJ_QC_BODY_MAX);heap_caps_free(work->http_body);work->http_body=NULL;}
            memset(work->http_auth,0,sizeof(work->http_auth));memset(work->http_req_body,0,sizeof(work->http_req_body));memset(work->http_url,0,sizeof(work->http_url));
        }
        redraw=1;
    }
    if(!job_running) {
        pj_setup_tick(s,now);
        int request=pj_setup_take_request(s);
        if(request==PJ_SU_REQ_SCAN){if(!start_job(JOB_SCAN))pj_setup_scan_done(s,0);redraw=1;}
        else if(request==PJ_SU_REQ_DISCOVER){if(!start_job(JOB_DISCOVER))pj_setup_discover_done(s);redraw=1;}
        else if(request==PJ_SU_REQ_HTTP) {
            work->http_body=heap_caps_calloc(1,PJ_QC_BODY_MAX,MALLOC_CAP_8BIT);
            snprintf(work->http_url,sizeof(work->http_url),"%s%s",s->qc.url,s->http.path);
            snprintf(work->http_method,sizeof(work->http_method),"%s",s->http.method?s->http.method:"GET");
            snprintf(work->http_req_body,sizeof(work->http_req_body),"%s",s->http.body);
            pj_qc_auth_header(work->http_auth,sizeof(work->http_auth),s->qc.device_id,s->version,s->http.with_token?s->qc.token:NULL);
            if(!work->http_body||!start_job(JOB_HTTP)) {
                if(work->http_body){heap_caps_free(work->http_body);work->http_body=NULL;}
                pj_setup_http_done(s,0,NULL,0,now);
            }
            redraw=1;
        } else if(request==PJ_SU_REQ_COMMIT) {
            int rc=pj_provision_commit(&s->edit); /* the launcher's one flash write */
            pj_setup_commit_done(s,rc);redraw=1;
        }
    }
    /* The busy, code and switching screens animate: a repaint four times a second. */
    if(pj_setup_animates(s)&&(uint32_t)(now-last_redraw)>=250u){last_redraw=now;redraw=1;}
    return redraw;
}
