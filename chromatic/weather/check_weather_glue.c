/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC model of the Local Weather channel's ESP32 side: chromatic/launcher/main/weather_esp32.c
 * itself, compiled against the stand-in headers of host_stubs/ and run here with
 *   - a counting heap (every allocation of the file, the task's stack, the HTTP client and a
 *     stand-in for one connection's TLS memory), which must return to its start in every scenario;
 *   - a scripted HTTP client fed from the real replies kept in the PC reference's kept replies (not published) and
 *     the radar fixtures, with connects, header waits and reads that take a scripted number of
 *     steps and can fail, stall or break off;
 *   - a scheduler that runs the fetch task one step at a time between main-loop passes (the task
 *     is a real thread, but only one of the two ever runs: every network call and every
 *     vTaskDelay hands over), so each run is the same every time;
 *   - the clock (unset at start, set by the reply's Date header or by a scripted SNTP answer).
 * A pass is 10 ms: pj_wx_service and pj_wx_poll once, then one step of the task.
 * What it cannot show: real TLS, real heap figures, real timing, the launcher's own side.
 *
 *   check_weather_glue <cache folder> <radar fixture folder>
 */
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_crt_bundle.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_netif_sntp.h"
#include "esp_system.h"
#include "spi_flash_mmap.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "../launcher/main/weather_esp32.h"

static int failures,checks;
#define CHECK(cond,...) do{checks++;if(!(cond)){failures++;printf("FAIL %s:%d: ",__FILE__,__LINE__);printf(__VA_ARGS__);printf("\n");}}while(0)
#define PASS(name) printf("%s %s\n",failures==before?"PASS":"FAIL",name)
static const char *cache_dir,*fixture_dir;

/* ---- the model ---- */
#define HEAP_TOTAL 85000u       /* free when the weather channel opens (the launcher's figure) */
#define CONNECTION_BYTES 30000u /* stand-in for what one HTTPS connection holds */
#define CLIENT_BYTES 3000u
#define TASK_EXTRA 400u
static uint32_t heap_used,heap_peak,heap_reserved,heap_ours; /* reserved: taken by "someone else"; ours: weather_esp32.c's own blocks */
static uint32_t ours_peak;
static int fail_alloc_at;       /* the n-th allocation from now fails (0: none) */
static uint32_t ticks,now_ms;
static int64_t clock_base;      /* UTC seconds at tick 0; 0 = not set */
static int wifi=1,sntp_running,sntp_starts,sntp_answer_after; /* SNTP sets the clock this many passes after it starts (0: never) */
static uint32_t sntp_started_at;
static int64_t TRUE_TIME=1795131996LL; /* 2026-11-19 23:46:36 UTC, when the kept replies were made (the place scenarios: theirs) */
static char date_header[40]="Thu, 19 Nov 2026 23:46:36 GMT";
static int model_errors;

static void heap_take(uint32_t n,int ours){heap_used+=n;if(ours){heap_ours+=n;if(heap_ours>ours_peak)ours_peak=heap_ours;}if(heap_used>heap_peak)heap_peak=heap_used;}
static void heap_give(uint32_t n,int ours){heap_used-=n;if(ours)heap_ours-=n;}
void *heap_caps_malloc(size_t size,uint32_t caps) {
    (void)caps;
    if(fail_alloc_at&&--fail_alloc_at==0)return NULL;
    if(heap_used+heap_reserved+size>HEAP_TOTAL)return NULL;
    uint32_t *block=malloc(size+8);
    block[0]=(uint32_t)size;block[1]=0x57584d4du;
    heap_take((uint32_t)size,1);
    return block+2;
}
void *heap_caps_calloc(size_t n,size_t size,uint32_t caps){void *p=heap_caps_malloc(n*size,caps);if(p)memset(p,0,n*size);return p;}
void heap_caps_free(void *p) {
    if(!p)return;
    uint32_t *block=(uint32_t *)p-2;
    if(block[1]!=0x57584d4du){model_errors++;printf("MODEL a block was freed twice or never allocated\n");return;}
    block[1]=0;heap_give(block[0],1);
    memset(p,0xdd,block[0]); /* anything still using it will show */
    free(block);
}
uint32_t esp_get_free_heap_size(void){return HEAP_TOTAL-heap_used-heap_reserved;}
size_t heap_caps_get_largest_free_block(uint32_t caps){(void)caps;return esp_get_free_heap_size();}
time_t host_time(time_t *out){time_t t=clock_base?(time_t)(clock_base+ticks/100u):(time_t)(ticks/100u);if(out)*out=t;return t;}
int host_settimeofday(const struct timeval *tv,const void *zone){(void)zone;clock_base=(int64_t)tv->tv_sec-ticks/100u;return 0;}
esp_err_t esp_crt_bundle_attach(void *conf){(void)conf;return ESP_OK;}
esp_err_t esp_netif_sntp_init(const esp_sntp_config_t *config) {
    if(sntp_running){model_errors++;printf("MODEL SNTP started twice\n");return ESP_FAIL;}
    if(config->wait_for_sync){model_errors++;printf("MODEL SNTP asked to wait\n");}
    sntp_running=1;sntp_starts++;sntp_started_at=ticks;
    return ESP_OK;
}
void esp_netif_sntp_deinit(void){if(!sntp_running){model_errors++;printf("MODEL SNTP stopped while not running\n");}sntp_running=0;}

/* -- the scheduler: the task is a thread, but it runs only inside task_step() -- */
struct host_task {pthread_t thread;void (*code)(void *);void *arg;uint32_t stack;int running,exited_at;};
static pthread_mutex_t baton=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t handed=PTHREAD_COND_INITIALIZER;
static int turn;                 /* 0 the main loop, 1 the task */
static struct host_task *task;   /* at most one */
static struct host_task *dead[8];static int dead_n; /* exited: the "idle task" frees their stacks two passes later */
static int fail_task_create,tasks_created;
static void task_yield(void) {
    pthread_mutex_lock(&baton);
    turn=0;pthread_cond_broadcast(&handed);
    while(turn!=1)pthread_cond_wait(&handed,&baton);
    pthread_mutex_unlock(&baton);
}
static void *task_entry(void *arg) {
    struct host_task *t=arg;
    pthread_mutex_lock(&baton);
    while(turn!=1)pthread_cond_wait(&handed,&baton);
    pthread_mutex_unlock(&baton);
    t->code(t->arg);
    model_errors++;printf("MODEL the task function returned\n");
    return NULL;
}
BaseType_t xTaskCreate(void (*code)(void *),const char *name,uint32_t stack,void *arg,UBaseType_t priority,TaskHandle_t *handle) {
    (void)name;
    if(priority!=1){model_errors++;printf("MODEL task priority %u\n",priority);}
    if(task){model_errors++;printf("MODEL a second task while one runs\n");return 0;}
    if(fail_task_create||heap_used+heap_reserved+stack+TASK_EXTRA>HEAP_TOTAL)return 0;
    task=calloc(1,sizeof(*task));
    task->code=code;task->arg=arg;task->stack=stack;task->running=1;
    heap_take(stack+TASK_EXTRA,0);
    pthread_create(&task->thread,NULL,task_entry,task);
    tasks_created++;
    if(handle)*handle=task;
    return pdPASS;
}
void vTaskDelay(TickType_t t){for(TickType_t i=0;i<(t?t:1);i++)task_yield();}
void vTaskDelete(TaskHandle_t h) {
    if(h){model_errors++;printf("MODEL vTaskDelete of another task\n");return;}
    struct host_task *t=task;
    pthread_mutex_lock(&baton);
    t->running=0;t->exited_at=(int)ticks;
    task=NULL;dead[dead_n++]=t;
    turn=0;pthread_cond_broadcast(&handed);
    pthread_mutex_unlock(&baton);
    pthread_exit(NULL);
}
TickType_t xTaskGetTickCount(void){return ticks;}
UBaseType_t uxTaskGetStackHighWaterMark(TaskHandle_t h){(void)h;return 4321;}
static void task_step(void) {
    if(!task)return;
    pthread_mutex_lock(&baton);
    turn=1;pthread_cond_broadcast(&handed);
    while(turn!=0)pthread_cond_wait(&handed,&baton);
    pthread_mutex_unlock(&baton);
}
static void idle_task(void) {
    for(int i=0;i<dead_n;) {
        if((int)ticks-dead[i]->exited_at>=2) {
            pthread_join(dead[i]->thread,NULL);
            heap_give(dead[i]->stack+TASK_EXTRA,0);
            free(dead[i]);dead[i]=dead[--dead_n];
        } else i++;
    }
}

/* -- the HTTP client -- */
enum {RULE_NONE,RULE_CONNECT_FAIL,RULE_STATUS,RULE_LOST,RULE_STALL};
static struct {const char *match;int kind,value,times;} rules[4];
static int connect_steps=8,header_steps=3,chunked_all,send_date=1;
static const char *radar_fixture="radar-pattern.gif";
static struct {char path[240];char host[40];int fresh,status;} log_req[200];
static int log_n,handshakes,connections_open,most_connections;
struct esp_http_client {
    char host[64],url[400],accept[40];
    http_event_handle_cb handler;void *user;
    int connected,status,lost_at,stall,timeout_ms;
    uint8_t *body;size_t size,pos;int64_t length;
    int in_reply;
};
static uint8_t *load(const char *path,size_t *size) {
    FILE *f=fopen(path,"rb");
    if(!f)return NULL;
    fseek(f,0,SEEK_END);
    long n=ftell(f);
    fseek(f,0,SEEK_SET);
    uint8_t *data=malloc((size_t)n+1);
    *size=fread(data,1,(size_t)n,f);
    fclose(f);
    return data;
}
static void split_url(const char *url,char *host,size_t hcap,const char **path) {
    const char *p=strstr(url,"://");
    p=p?p+3:url;
    const char *slash=strchr(p,'/');
    size_t n=slash?(size_t)(slash-p):strlen(p);
    if(n>=hcap)n=hcap-1;
    memcpy(host,p,n);host[n]=0;
    *path=slash?slash:"/";
}
static void conn_close(struct esp_http_client *c) {
    if(c->connected){c->connected=0;heap_give(CONNECTION_BYTES,0);connections_open--;}
    free(c->body);c->body=NULL;c->size=c->pos=0;c->in_reply=0;
}
esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *config) {
    if(heap_used+heap_reserved+CLIENT_BYTES>HEAP_TOTAL)return NULL;
    struct esp_http_client *c=calloc(1,sizeof(*c));
    const char *path;
    heap_take(CLIENT_BYTES,0);
    snprintf(c->url,sizeof(c->url),"%s",config->url);
    split_url(c->url,c->host,sizeof(c->host),&path);
    c->handler=config->event_handler;c->user=config->user_data;c->timeout_ms=config->timeout_ms;
    if(!config->crt_bundle_attach||strncmp(config->url,"https://",8)||!config->user_agent||!config->disable_auto_redirect) {
        model_errors++;printf("MODEL the client was set up without HTTPS, the certificate bundle, a User-Agent or with redirects on\n");
    }
    return c;
}
esp_err_t esp_http_client_set_url(esp_http_client_handle_t c,const char *url) {
    char host[64];
    const char *path;
    split_url(url,host,sizeof(host),&path);
    if(strcmp(host,c->host))conn_close(c); /* another server: the real client closes too */
    snprintf(c->url,sizeof(c->url),"%s",url);snprintf(c->host,sizeof(c->host),"%s",host);
    return ESP_OK;
}
esp_err_t esp_http_client_set_header(esp_http_client_handle_t c,const char *key,const char *value){if(!strcmp(key,"Accept"))snprintf(c->accept,sizeof(c->accept),"%s",value);return ESP_OK;}
esp_err_t esp_http_client_set_timeout_ms(esp_http_client_handle_t c,int ms){c->timeout_ms=ms;return ESP_OK;}
static int rule_for(const char *path,int kind,int *value) {
    for(int i=0;i<4;i++)if(rules[i].kind==kind&&rules[i].match&&strstr(path,rules[i].match)&&rules[i].times) {
        if(rules[i].times>0)rules[i].times--;
        if(value)*value=rules[i].value;
        return 1;
    }
    return 0;
}
esp_err_t esp_http_client_open(esp_http_client_handle_t c,int write_len) {
    char host[64],file[600],slug[300];
    const char *path;
    int value=0;
    (void)write_len;
    split_url(c->url,host,sizeof(host),&path);
    if(c->connected&&c->in_reply&&c->pos<c->size){model_errors++;printf("MODEL a connection was used again with %zu bytes of the last reply unread (%s)\n",c->size-c->pos,path);}
    if(c->timeout_ms>4000){model_errors++;printf("MODEL connect timeout %d ms\n",c->timeout_ms);}
    int fresh=!c->connected;
    if(fresh) {
        for(int i=0;i<connect_steps;i++)task_yield(); /* name look-up, TCP, TLS: cannot be interrupted */
        if(!wifi||rule_for(path,RULE_CONNECT_FAIL,NULL))return ESP_FAIL;
        c->connected=1;handshakes++;connections_open++;heap_take(CONNECTION_BYTES,0);
        if(connections_open>most_connections)most_connections=connections_open;
    }
    free(c->body);c->body=NULL;c->size=c->pos=0;c->lost_at=-1;c->stall=0;c->status=200;
    if(!strcmp(host,WX_RADAR_HOST)) {
        snprintf(file,sizeof(file),"%s/%s",fixture_dir,radar_fixture);
        if(strcmp(c->accept,"image/gif")){model_errors++;printf("MODEL radar asked with Accept %s\n",c->accept);}
    } else {
        size_t n=0;
        int gap=0;
        snprintf(file,sizeof(file),"%s%s",host,path);
        for(const char *p=file;*p&&n+2<sizeof(slug);p++) {
            int keep=(*p>='A'&&*p<='Z')||(*p>='a'&&*p<='z')||(*p>='0'&&*p<='9')||*p=='.'||*p=='-';
            if(keep){if(gap&&n)slug[n++]='_';gap=0;slug[n++]=*p;}else gap=1;
        }
        slug[n]=0;
        snprintf(file,sizeof(file),"%s/%s.json",cache_dir,slug);
        if(strcmp(host,WX_HOST)){model_errors++;printf("MODEL unknown host %s\n",host);}
    }
    c->body=load(file,&c->size);
    if(!c->body){c->status=404;c->body=(uint8_t *)strdup("{\"title\":\"Not Found\"}");c->size=strlen((char *)c->body);}
    if(rule_for(path,RULE_STATUS,&value)){c->status=value;free(c->body);c->body=(uint8_t *)strdup("{\"status\":500}");c->size=strlen((char *)c->body);}
    if(rule_for(path,RULE_LOST,&value))c->lost_at=value;
    if(rule_for(path,RULE_STALL,&value))c->stall=1;
    c->length=chunked_all?-1:(int64_t)c->size;c->in_reply=1;
    if(log_n<200){snprintf(log_req[log_n].path,sizeof(log_req[log_n].path),"%.239s",path);snprintf(log_req[log_n].host,sizeof(log_req[log_n].host),"%.39s",host);log_req[log_n].fresh=fresh;log_req[log_n].status=c->status;log_n++;}
    return ESP_OK;
}
int64_t esp_http_client_fetch_headers(esp_http_client_handle_t c) {
    for(int i=0;i<header_steps;i++)task_yield();
    if(!c->connected||!wifi)return -1;
    if(c->timeout_ms>4000){model_errors++;printf("MODEL header timeout %d ms\n",c->timeout_ms);}
    if(send_date&&c->handler) {
        char key[]="date",value[40]; /* servers send any case */
        snprintf(value,sizeof(value),"%s",date_header);
        esp_http_client_event_t ev={.event_id=HTTP_EVENT_ON_HEADER,.client=c,.user_data=c->user,.header_key=key,.header_value=value};
        c->handler(&ev);
    }
    return c->length>0?c->length:0;
}
int esp_http_client_read(esp_http_client_handle_t c,char *buffer,int len) {
    task_yield();
    if(c->timeout_ms>1500){model_errors++;printf("MODEL body read timeout %d ms\n",c->timeout_ms);}
    if(!c->connected||!wifi)return -1;
    if(c->stall)return -ESP_ERR_HTTP_EAGAIN;
    if(c->lost_at>=0&&c->pos>=(size_t)c->lost_at){conn_close(c);return -1;}
    size_t n=c->size-c->pos;
    if(n>(size_t)len)n=(size_t)len;
    if(n>700&&(c->pos/700)%3==1)n=700; /* pieces of uneven size, as a network gives */
    memcpy(buffer,c->body+c->pos,n);c->pos+=n;
    return (int)n;
}
int esp_http_client_get_status_code(esp_http_client_handle_t c){return c->status;}
int64_t esp_http_client_get_content_length(esp_http_client_handle_t c){return c->length;}
bool esp_http_client_is_complete_data_received(esp_http_client_handle_t c){return c->pos>=c->size;}
esp_err_t esp_http_client_close(esp_http_client_handle_t c){conn_close(c);return ESP_OK;}
esp_err_t esp_http_client_cleanup(esp_http_client_handle_t c){conn_close(c);heap_give(CLIENT_BYTES,0);free(c);return ESP_OK;}

/* -- the flash: only the weather location's two sectors (1.6.3) and the location pack's two copies (1.6.5) are ever
 *    read -- */
static uint8_t flash_place[2][4096],flash_pack[2][WX_LPACK_COPY];
static int flash_reads,flash_fail,pack_reads;
esp_err_t esp_flash_read(esp_flash_t *chip,void *buffer,uint32_t address,uint32_t length) {
    flash_reads++;
    if(chip!=NULL){model_errors++;printf("MODEL a flash read named a chip\n");}
    int sector=address>=WX_PLACE_FLASH_A&&address+length<=WX_PLACE_FLASH_A+4096u?0:address>=WX_PLACE_FLASH_B&&address+length<=WX_PLACE_FLASH_B+4096u?1:-1;
    int copy=address>=WX_LPACK_FLASH_A&&address+length<=WX_LPACK_FLASH_A+WX_LPACK_COPY?0:
             address>=WX_LPACK_FLASH_B&&address+length<=WX_LPACK_FLASH_B+WX_LPACK_COPY?1:-1;
    if(sector<0&&copy<0){model_errors++;printf("MODEL flash read outside the weather location and pack areas (0x%lx, %lu)\n",(unsigned long)address,(unsigned long)length);return ESP_FAIL;}
    if(flash_fail)return ESP_FAIL;
    if(copy>=0){pack_reads++;memcpy(buffer,flash_pack[copy]+(address-(copy?WX_LPACK_FLASH_B:WX_LPACK_FLASH_A)),length);return ESP_OK;}
    memcpy(buffer,flash_place[sector]+(address-(sector?WX_PLACE_FLASH_B:WX_PLACE_FLASH_A)),length);
    return ESP_OK;
}
/* 1.6.5 (review M1): the 64 KiB page holding both pack copies, mapped read-only; a fresh picture of it per mapping,
 * filled from the model's flash, so the code reads the pack in place (never a heap copy) */
static uint8_t *flash_map_page;
static int maps_open,map_fail,maps_made;
esp_err_t spi_flash_mmap(size_t src_addr,size_t size,spi_flash_mmap_memory_t memory,const void **out_ptr,
                         spi_flash_mmap_handle_t *out_handle) {
    if(src_addr!=0x3F0000u||size!=0x10000u||memory!=SPI_FLASH_MMAP_DATA||!out_ptr||!out_handle||maps_open) {
        model_errors++;printf("MODEL an unexpected mapping (0x%lx, %lu, %d open)\n",(unsigned long)src_addr,(unsigned long)size,maps_open);
        return ESP_FAIL;
    }
    if(map_fail)return ESP_FAIL;   /* ESP_ERR_NO_MEM on the console */
    if(!flash_map_page&&!(flash_map_page=malloc(0x10000u)))return ESP_FAIL;
    memset(flash_map_page,0xff,0x10000u);
    memcpy(flash_map_page+(WX_LPACK_FLASH_A-0x3F0000u),flash_pack[0],WX_LPACK_COPY);
    memcpy(flash_map_page+(WX_LPACK_FLASH_B-0x3F0000u),flash_pack[1],WX_LPACK_COPY);
    *out_ptr=flash_map_page;*out_handle=0x5150u;maps_open++;maps_made++;
    return ESP_OK;
}
void spi_flash_munmap(spi_flash_mmap_handle_t handle) {
    if(handle!=0x5150u||maps_open!=1){model_errors++;printf("MODEL an unmap that matches no mapping\n");return;}
    maps_open--;
    memset(flash_map_page,0xa5,0x10000u);   /* a read after the unmap would see nonsense, not the pack */
}
/* the default pack's own forecast request (its home grid) */
static const char *default_forecast(void) {
    for(unsigned i=0;i<WX_DEFAULT_PACK->requests;i++)if(WX_DEFAULT_PACK->req[i].kind==WX_K_FORECAST)return WX_DEFAULT_PACK->req[i].path;
    return "";
}

/* -- one main-loop pass: 10 ms -- */
static int updating_seen,passes_updating;
static void pass(void) {
    ticks++;now_ms+=10;
    if(sntp_running&&sntp_answer_after&&!clock_base&&(int)(ticks-sntp_started_at)>=sntp_answer_after)clock_base=TRUE_TIME-ticks/100u;
    idle_task();
    pj_wx_service(now_ms,wifi);
    pj_wx_poll();
    wx_state *s=pj_wx_state();
    if(s&&s->updating){updating_seen=1;passes_updating++;}
    if(s)wx_tick(s,now_ms,pj_wx_epoch());
    task_step();
}
static void run(int n){for(int i=0;i<n;i++)pass();}
/* Passes until nothing is in flight (and the note is off); -1 if it never settles. */
static int settle(int limit) {
    for(int i=0;i<limit;i++) {
        pass();
        wx_state *s=pj_wx_state();
        if(!pj_wx_busy()&&!pj_wx_poll()&&(!s||!s->updating)&&!task)return i+1;
    }
    return -1;
}
static void reset_model(void) {
    memset(rules,0,sizeof(rules));
    connect_steps=8;header_steps=3;chunked_all=0;send_date=1;radar_fixture="radar-pattern.gif";
    heap_reserved=0;fail_alloc_at=0;fail_task_create=0;wifi=1;sntp_answer_after=0;map_fail=0;
    log_n=0;handshakes=0;most_connections=0;updating_seen=0;passes_updating=0;tasks_created=0;
}
/* Back to "never opened": forget, wait, and the heap must be where it began. */
static void end_scenario(const char *name) {
    pj_wx_forget();
    int n=settle(3000);
    for(int i=0;i<5;i++)pass();
    CHECK(n>=0,"%s: never settled after forget",name);
    CHECK(heap_used==0&&heap_ours==0,"%s: %u bytes still allocated after forget (%u of them the file's own)",name,(unsigned)heap_used,(unsigned)heap_ours);
    CHECK(maps_open==0,"%s: the pack's flash page is still mapped after forget",name);
    CHECK(!sntp_running,"%s: SNTP still running after forget",name);
    CHECK(connections_open==0&&!task&&dead_n==0,"%s: a connection or task is left",name);
    CHECK(pj_wx_state()==NULL&&!pj_wx_busy(),"%s: state or busy after forget",name);
    CHECK(model_errors==0,"%s: %d model errors",name,model_errors);
    model_errors=0;clock_base=0;
    reset_model();
}
static int index_of(const char *path) {
    for(unsigned i=0;i<wx_request_count();i++)if(!strcmp(wx_request_at(i)->path,path))return (int)i;
    return -1;
}
static void show_log(const char *why) {
    char line[300];
    printf("LOG %s:",why);
    for(int i=0;i<log_n;i++)printf(" %d%s(%d)",index_of(log_req[i].path),log_req[i].fresh?"*":"",log_req[i].status);
    pj_wx_diag(line,sizeof(line));
    printf("\nLOG %s\n",line);
}
static int count_kind(int kind){int n=0;for(int i=0;i<log_n;i++){int k=index_of(log_req[i].path);n+=k>=0&&wx_request_at((unsigned)k)->kind==kind;}return n;}

static uint32_t cached_bytes;

static void test_first_round(void) {
    int before=failures;
    reset_model();
    CHECK(pj_wx_state()==NULL&&!pj_wx_busy()&&!pj_wx_poll(),"before the first open");
    pj_wx_service(0,1);pj_wx_close();pj_wx_pause(1);pj_wx_pause(0); /* harmless while closed */
    CHECK(heap_used==0,"calls while closed allocated");
    CHECK(pj_wx_open()==0,"open");
    cached_bytes=heap_ours;
    wx_state *s=pj_wx_state();
    CHECK(s!=NULL&&s->obs[0].temp_f==WX_NA&&!s->updating,"a new state is not cleared");
    CHECK(pj_wx_epoch()==0,"the clock is set before anything");
    wx_enter(s,now_ms);
    pass();pass();
    CHECK(task!=NULL&&s->updating,"no round started at once (no wait for SNTP)");
    CHECK(sntp_running,"SNTP not started");
    /* values arrive while the pages cycle */
    int at_obs=0,at_fc=0,n=0;
    for(;n<6000;n++){pass();if(!at_obs&&s->obs[0].temp_f==55)at_obs=n;if(!at_fc&&s->fc.n_local)at_fc=n;if(!pj_wx_busy()&&!s->updating)break;}
    CHECK(n<6000,"the first round never ended");
    CHECK(at_obs>0&&at_fc>at_obs&&at_fc<n,"the home figures did not arrive early in the round (obs %d, forecast %d, end %d)",at_obs,at_fc,n);
    CHECK(pj_wx_epoch()>=TRUE_TIME&&pj_wx_epoch()<TRUE_TIME+120,"the clock was not set from the Date header (%lld)",(long long)pj_wx_epoch());
    /* order and connections */
    int last=-1,ordered=1,second_host=0;
    for(int i=0;i<log_n;i++){int k=index_of(log_req[i].path);if(k<=last)ordered=0;last=k;if(i&&strcmp(log_req[i].host,log_req[i-1].host))second_host++;}
    if(!ordered||log_n!=24)show_log("first round");
    CHECK(ordered&&log_n==24,"requests out of table order, or %d of them (want 23 JSON + radar)",log_n);
    CHECK(handshakes==2&&second_host==1&&most_connections==1,"%d handshakes, %d host changes, %d connections at once (want 2, 1, 1)",handshakes,second_host,most_connections);
    CHECK(!strcmp(log_req[log_n-1].host,WX_RADAR_HOST)&&!strcmp(log_req[0].path,WX_DEFAULT_PACK->req[0].path),"first and last request");
    CHECK(s->obs[0].temp_f==55&&s->obs[0].pressure==3041&&!strcmp(s->obs[0].sky,"MOSTLY CLOUDY")&&s->trend==WX_TREND_FALLING,"home figures after the round");
    CHECK(s->fc.n_local==3&&s->fc.n_ext==6&&s->city[0].temp==68&&s->obs[8].temp_f==57,"forecast, cities and the row that needed its recent reports");
    CHECK(s->radar!=NULL&&s->radar_show&&s->pages[s->n_pages-1][0]==WX_P_RADAR,"the radar picture is not shown");
    CHECK(heap_ours==cached_bytes+WX_RADAR_BYTES,"after the round the file holds %u bytes (want cache %u + radar page %u)",(unsigned)heap_ours,(unsigned)cached_bytes,(unsigned)WX_RADAR_BYTES);
    CHECK(heap_used==heap_ours,"task, client or connection memory left after the round (%u)",(unsigned)(heap_used-heap_ours));
    CHECK(updating_seen&&!s->updating,"UPDATING was not shown during the round or is still on");
    printf("INFO first round: %d requests, %d handshakes, %d passes of 10 ms in the model; home observation after %d, forecast after %d\n",log_n,handshakes,n,at_obs,at_fc);
    printf("INFO heap (the file's own blocks): cache %u; open with a radar picture %u; peak in a round %u; whole model peak %u of %u\n",
           (unsigned)cached_bytes,(unsigned)heap_ours,(unsigned)ours_peak,(unsigned)heap_peak,HEAP_TOTAL);
    /* idle: nothing is due for a while */
    log_n=0;handshakes=0;tasks_created=0;
    run(3000); /* 30 s */
    CHECK(log_n==0&&tasks_created==0,"requests while everything is fresh (%d)",log_n);
    char line[300];
    pj_wx_diag(line,sizeof(line));
    CHECK(strstr(line,"rounds=1 ")&&strstr(line,"ok=24 ")&&strstr(line,"failed=0 ")&&strstr(line,"conn=2 ")&&strstr(line,"radar=1 "),"diagnostics: %s",line);
    printf("INFO %s\n",line);

    /* the cache */
    pj_wx_close();
    CHECK(pj_wx_state()==NULL,"state after close");
    CHECK(settle(100)>=0,"close never settled");
    CHECK(heap_used==cached_bytes&&heap_ours==cached_bytes,"after close %u bytes are held (want the cache, %u)",(unsigned)heap_used,(unsigned)cached_bytes);
    CHECK(!sntp_running,"SNTP runs while closed");
    run(500);
    CHECK(pj_wx_open()==0,"open again");
    s=pj_wx_state();
    CHECK(s->obs[0].temp_f==55&&s->fc.n_local==3&&!s->radar,"the kept state lost its values, or kept a radar picture");
    log_n=0;handshakes=0;
    CHECK(settle(2000)>=0,"second open never settled");run(300);
    if(!(log_n==1&&s->radar))show_log("second open");
    CHECK(log_n==1&&handshakes==1&&count_kind(WX_K_RADAR)==1&&s->radar,"opened again within the periods: %d requests, %d handshakes (want the radar only)",log_n,handshakes);
    /* 11 minutes on: the ten-minute requests, and only they */
    now_ms+=660000u;ticks+=66000u;
    log_n=0;handshakes=0;
    CHECK(settle(3000)>=0,"refresh never settled");run(300);
    if(log_n!=3)show_log("11 minutes");
    CHECK(log_n==3&&handshakes==2&&!strcmp(log_req[0].path,WX_DEFAULT_PACK->req[0].path)&&strstr(log_req[1].path,"/alerts/")&&count_kind(WX_K_RADAR)==1,
          "after 11 minutes: %d requests, %d handshakes (want home observation, alerts, radar)",log_n,handshakes);
    /* an hour on: everything */
    now_ms+=3600000u;ticks+=360000u;
    log_n=0;handshakes=0;
    CHECK(settle(8000)>=0,"hourly refresh never settled");run(300);
    CHECK(log_n>=23&&handshakes==2,"after an hour: %d requests, %d handshakes",log_n,handshakes);
    /* closed, 11 minutes pass, opened: the cache's ages were kept across the close */
    pj_wx_close();settle(100);
    now_ms+=660000u;ticks+=66000u;
    CHECK(pj_wx_open()==0,"third open");
    log_n=0;handshakes=0;
    settle(3000);run(300);
    CHECK(log_n==3&&handshakes==2,"closed for 11 minutes: %d requests on opening (want 3)",log_n);
    end_scenario("first round and cache");
    PASS("glue: first round (order, 2 handshakes, clock from Date, UPDATING), cache across close and open");
}

static void test_stops(void) {
    int before=failures,worst_close=0,worst_pause=0,sixes=0;
    /* close at every moment of a round: connect, headers, body, hand-over, radar */
    for(int at=1;at<=420;at+=1+(at>120)*2) {
        reset_model();
        CHECK(pj_wx_open()==0,"open (close sweep %d)",at);
        run(at);
        pj_wx_close();
        CHECK(pj_wx_state()==NULL,"state after close");
        int n=0,was_busy=pj_wx_busy();
        for(;n<400;n++) {
            if(pj_wx_open()==-6)sixes++;else{CHECK(!pj_wx_busy(),"open succeeded while busy");pj_wx_close();}
            pass();
            if(!pj_wx_busy()&&!pj_wx_poll())break;
        }
        CHECK(n<400,"close at pass %d never finished",at);
        if(was_busy&&n>worst_close)worst_close=n;
        CHECK(heap_used==cached_bytes&&connections_open==0&&!task&&dead_n==0,"close at pass %d: %u bytes held when busy dropped (want %u), %d connections",at,(unsigned)heap_used,(unsigned)cached_bytes,connections_open);
        end_scenario("close sweep");
    }
    CHECK(sixes>0,"open never returned -6 while a close was letting go");
    /* forget at every moment */
    for(int at=1;at<=420;at+=7) {
        reset_model();
        pj_wx_open();run(at);
        end_scenario("forget sweep");
    }
    /* pause at every moment: the round stops, and goes on to the end when resumed */
    for(int at=2;at<=400;at+=23) {
        reset_model();
        pj_wx_open();
        wx_state *s=pj_wx_state();
        run(at);
        pj_wx_pause(1);
        int n=0;
        for(;n<400;n++){pass();if(!pj_wx_busy())break;}
        CHECK(n<400,"pause at pass %d: still busy",at);
        if(n>worst_pause)worst_pause=n;
        CHECK(heap_used==heap_ours&&connections_open==0,"pause at pass %d left a connection or the task",at);
        int requests=log_n;
        run(1000);
        CHECK(log_n==requests&&!s->updating,"requests while paused");
        pj_wx_pause(0);
        CHECK(settle(8000)>=0,"resume never settled");
        now_ms+=130000u;ticks+=13000u; /* what the stop cut short is due at once; nothing here had failed */
        settle(8000);
        CHECK(s->obs[0].temp_f==55&&s->fc.n_local==3&&s->radar,"after pause at pass %d and resume the pages are not filled",at);
        end_scenario("pause sweep");
    }
    /* a stop inside a long connection set-up cannot be quick: busy says so until it is really over */
    reset_model();
    connect_steps=700; /* 7 s of name look-up */
    pj_wx_open();run(100);
    pj_wx_close();
    int n=0;
    for(;n<2000;n++){pass();if(!pj_wx_busy()&&!pj_wx_poll())break;}
    CHECK(n>550&&n<700,"a stop during a 7 s connect took %d passes",n);
    CHECK(heap_used==cached_bytes,"memory after the slow stop");
    end_scenario("slow connect");
    printf("INFO stop: busy dropped at most %d passes (10 ms each) after a close and %d after a pause, connect taking 8 steps; a 7 s connect holds it for its length\n",worst_close,worst_pause);
    PASS("glue: close, forget and pause at every moment of a round; -6 while leaving; busy exact");
}

static void test_trouble(void) {
    int before=failures;
    wx_state *s;
    /* the heap gate */
    reset_model();
    pj_wx_open();s=pj_wx_state();
    heap_reserved=HEAP_TOTAL-cached_bytes-53000u; /* 53,000 free: under the gate */
    run(800);
    CHECK(log_n==0&&!task&&!s->updating&&heap_ours==cached_bytes,"a round started under the heap gate");
    heap_reserved=HEAP_TOTAL-cached_bytes-56000u; /* enough for the JSON requests, not for the radar beside a connection */
    CHECK(settle(8000)>=0,"gate: never settled");
    CHECK(s->obs[0].temp_f==55&&s->fc.n_local==3&&!s->radar&&count_kind(WX_K_RADAR)==0&&s->pages[s->n_pages-1][0]==WX_P_OUTLOOK,
          "tight heap: want the pages filled, no radar request, the rain outlook");
    CHECK(heap_ours==cached_bytes,"tight heap: radar memory was taken");
    end_scenario("heap gate");
    /* no Wi-Fi */
    reset_model();wifi=0;
    pj_wx_open();s=pj_wx_state();
    run(600);
    CHECK(log_n==0&&!task&&!sntp_running&&!s->updating,"something ran without Wi-Fi");
    wifi=1;
    CHECK(settle(8000)>=0&&s->obs[0].temp_f==55,"Wi-Fi back: not filled");
    wifi=0;run(50);wifi=1; /* a drop between rounds is harmless */
    end_scenario("no Wi-Fi");
    /* Wi-Fi lost in the middle of a round */
    reset_model();
    pj_wx_open();s=pj_wx_state();
    run(60);wifi=0;
    CHECK(settle(2000)>=0,"Wi-Fi lost mid-round: never settled");
    CHECK(heap_used==heap_ours&&connections_open==0,"Wi-Fi lost mid-round: something left");
    end_scenario("Wi-Fi lost");
    /* no Content-Length anywhere (chunked replies): still two handshakes */
    reset_model();chunked_all=1;
    pj_wx_open();s=pj_wx_state();
    CHECK(settle(8000)>=0,"chunked: never settled");
    CHECK(handshakes==2&&log_n==24&&s->obs[0].temp_f==55&&s->city[8].temp==70&&s->radar,"replies without Content-Length: %d handshakes, %d requests",handshakes,log_n);
    end_scenario("no Content-Length");
    /* HTTP 500 and 404: that request fails, the round goes on, it is asked again two minutes later */
    reset_model();
    rules[0].match=default_forecast();rules[0].kind=RULE_STATUS;rules[0].value=500;rules[0].times=1;
    rules[1].match="/alerts/";rules[1].kind=RULE_STATUS;rules[1].value=404;rules[1].times=1;
    pj_wx_open();s=pj_wx_state();
    CHECK(settle(8000)>=0,"HTTP errors: never settled");
    CHECK(s->obs[0].temp_f==55&&!s->fc.n_local&&s->city[0].temp==68&&s->radar&&log_n==24,"HTTP 500/404: the rest of the round did not go on (%d requests)",log_n);
    char line[300];
    pj_wx_diag(line,sizeof(line));
    CHECK(strstr(line,"failed=2 "),"diagnostics after HTTP errors: %s",line);
    log_n=0;
    run(6000);
    CHECK(log_n==0,"a failed request was asked again within a minute");
    now_ms+=70000u;ticks+=7000u;
    settle(3000);
    CHECK(log_n==2&&s->fc.n_local==3,"the failed requests were not asked again after two minutes (%d requests)",log_n);
    end_scenario("HTTP errors");
    /* the connection breaks in the middle of the largest reply */
    reset_model();
    rules[0].match="limit=40";rules[0].kind=RULE_LOST;rules[0].value=30000;rules[0].times=1;
    pj_wx_open();s=pj_wx_state();
    CHECK(settle(3000)>=0,"lost connection: never settled");
    CHECK(s->obs[0].temp_f==55&&s->fc.n_local==3&&s->trend==WX_TREND_NONE&&log_n==4,"lost connection: what came before must stay, the round must end (%d requests)",log_n);
    CHECK(heap_used==heap_ours&&heap_ours==cached_bytes,"lost connection: memory");
    run(5800); /* under a minute: no new round */
    CHECK(log_n==4,"a new round started within a minute of a broken one");
    now_ms+=130000u;ticks+=13000u;
    CHECK(settle(8000)>=0&&s->trend==WX_TREND_FALLING&&s->radar,"after the wait the rest was not fetched");
    end_scenario("lost connection");
    /* nothing connects */
    reset_model();
    rules[0].match="/";rules[0].kind=RULE_CONNECT_FAIL;rules[0].times=-1;
    pj_wx_open();s=pj_wx_state();
    CHECK(settle(3000)>=0,"no connection: never settled");
    CHECK(s->obs[0].temp_f==WX_NA&&!s->updating&&heap_ours==cached_bytes&&heap_used==heap_ours,"no connection: state or memory");
    CHECK(pj_wx_epoch()==0,"clock without any reply");
    end_scenario("no connection");
    /* a reply that stops arriving */
    reset_model();
    rules[0].match="/alerts/";rules[0].kind=RULE_STALL;rules[0].times=1;
    pj_wx_open();s=pj_wx_state();
    CHECK(settle(3000)>=0,"stalled reply: never settled");
    CHECK(s->fc.n_local==3&&log_n==3&&heap_used==heap_ours,"stalled reply: the round must end there (%d requests)",log_n);
    end_scenario("stalled reply");
    /* the map server answers with its error text, then with a picture of the wrong size: no radar, nothing else hurt */
    static const char *const refused[2]={"refuse-html.gif","refuse-oversized.gif"};
    for(int k=0;k<2;k++) {
        reset_model();radar_fixture=refused[k];
        pj_wx_open();s=pj_wx_state();
        CHECK(settle(8000)>=0,"refused radar: never settled");
        CHECK(!s->radar&&s->pages[s->n_pages-1][0]==WX_P_OUTLOOK&&s->obs[0].temp_f==55,"%s: want the rain outlook and the rest filled",refused[k]);
        CHECK(heap_ours==cached_bytes+WX_RADAR_BYTES,"%s: the decoder was not freed (%u)",refused[k],(unsigned)heap_ours);
        end_scenario("refused radar");
    }
    /* a radar picture that breaks off, after a good one: the good one is gone (decoded in place), the page says so */
    reset_model();
    pj_wx_open();s=pj_wx_state();
    settle(8000);
    CHECK(s->radar!=NULL,"no first radar picture");
    radar_fixture="stop-truncated.gif";
    now_ms+=660000u;ticks+=66000u;
    settle(3000);
    CHECK(!s->radar&&s->pages[s->n_pages-1][0]==WX_P_OUTLOOK,"a broken radar picture is shown");
    radar_fixture="refuse-html.gif"; /* and a refusal keeps a good picture */
    now_ms+=130000u;ticks+=13000u;settle(3000);
    radar_fixture="radar-pattern.gif";
    now_ms+=130000u;ticks+=13000u;settle(3000);
    CHECK(s->radar!=NULL,"radar did not come back");
    const uint8_t *page=s->radar;
    uint32_t taken=s->radar_at;
    radar_fixture="refuse-html.gif";
    now_ms+=660000u;ticks+=66000u;settle(3000);
    CHECK(s->radar==page&&s->radar_at==taken,"a refused radar reply did not keep the last picture");
    end_scenario("broken radar");
    /* allocations failing: the cache, the round's block, the task */
    reset_model();
    fail_alloc_at=1;
    CHECK(pj_wx_open()==-2&&pj_wx_state()==NULL&&heap_used==0,"open without memory");
    CHECK(pj_wx_open()==0,"open after that");
    s=pj_wx_state();
    fail_alloc_at=1;
    run(300);
    CHECK(log_n==0&&heap_ours==cached_bytes,"a round ran without its block");
    fail_task_create=1;
    run(900);
    CHECK(log_n==0&&heap_ours==cached_bytes&&!s->updating,"a round ran without its task");
    fail_task_create=0;
    now_ms+=70000u;ticks+=7000u;
    CHECK(settle(8000)>=0&&s->obs[0].temp_f==55,"did not recover after failed allocations");
    end_scenario("failed allocations");
    /* SNTP answers before the first reply: its time is used; no Date header at all: SNTP alone sets the clock */
    reset_model();
    sntp_answer_after=3;send_date=0;connect_steps=20;
    pj_wx_open();s=pj_wx_state();
    CHECK(settle(8000)>=0&&pj_wx_epoch()>=TRUE_TIME&&s->obs[0].temp_f==55,"SNTP clock: not filled");
    end_scenario("SNTP clock");
    /* neither SNTP nor a Date header: no believable clock, so nothing is shown as current */
    reset_model();
    send_date=0;
    pj_wx_open();s=pj_wx_state();
    CHECK(settle(8000)>=0,"no clock: never settled");
    CHECK(pj_wx_epoch()==0&&s->obs[0].temp_f==WX_NA&&!s->radar,"values were committed without a clock");
    end_scenario("no clock");
    /* forget while closed (only the cache held), twice, and close twice */
    reset_model();
    pj_wx_open();settle(8000);
    pj_wx_close();pj_wx_close();settle(100);
    CHECK(heap_used==cached_bytes,"closed: cache");
    pj_wx_forget();pj_wx_forget();
    end_scenario("forget while closed");
    PASS("glue: heap gate, no Wi-Fi, no Content-Length, HTTP 500/404, lost and stalled connections, radar refused, failed allocations, clock");
}

/* ---- 1.6.3: the weather location record ---- */
static const char *place_dir;     /* replies for the record's place, named as the HTTP model names them */
static uint8_t place_record[WX_PLACE_RECORD],place_record2[WX_PLACE_RECORD];
static int64_t place_time;
static char place_date[40];
static void put_place(int sector,const uint8_t *record){memset(flash_place[sector],0xff,4096);if(record)memcpy(flash_place[sector],record,WX_PLACE_RECORD);}
static int load_record(const char *name,uint8_t *out) {
    char path[600];size_t n=0;
    snprintf(path,sizeof(path),"%s/%s",place_dir,name);
    uint8_t *data=load(path,&n);
    if(!data||n!=WX_PLACE_RECORD){free(data);return 0;}
    memcpy(out,data,WX_PLACE_RECORD);free(data);
    return 1;
}
static void place_clock(int on){TRUE_TIME=on?place_time:1795131996LL;snprintf(date_header,sizeof(date_header),"%s",on?place_date:"Thu, 19 Nov 2026 23:46:36 GMT");}
static int in_place_list(const wx_place *p,const char *path) {
    char url[304];
    for(unsigned i=0;i<wx_place_request_count(p);i++){wx_req_info info;if(wx_place_request(p,i,&info,url,sizeof(url))&&!strcmp(strstr(url,".gov")+4,path))return (int)i;}
    return -1;
}
static void test_places(void) {
    int before=failures;
    const char *home_dir=cache_dir;
    char line[300];
    CHECK(load_record("denver.rec",place_record)&&load_record("second.rec",place_record2),"the place records are missing");
    /* a good record in both sectors: that place's list, pages and clock */
    reset_model();place_clock(1);cache_dir=place_dir;
    put_place(0,place_record);put_place(1,place_record);
    flash_reads=0;pack_reads=0;
    CHECK(pj_wx_open()==0,"open with a place");
    wx_state *s=pj_wx_state();
    uint32_t cache_only=heap_ours-(uint32_t)sizeof(wx_place);
    CHECK(flash_reads==4&&pack_reads==2&&s&&s->place&&!strcmp(s->place->name,"DENVER, CO")&&!s->place->radar,"the record (and the pack headers) were not read at open (%d reads)",flash_reads);
    CHECK(heap_ours==cached_bytes+sizeof(wx_place),"open with a place holds %u bytes (want the cache %u and the place %u)",(unsigned)heap_ours,(unsigned)cached_bytes,(unsigned)sizeof(wx_place));
    wx_enter(s,now_ms);
    CHECK(settle(8000)>=0,"place round never settled");
    int ordered=1,last=-1,radar=0,missing=0;
    for(int i=0;i<log_n;i++){int k=in_place_list(s->place,log_req[i].path);if(k<=last)ordered=0;last=k;radar+=!strcmp(log_req[i].host,WX_RADAR_HOST);missing+=log_req[i].status!=200;}
    if(!ordered||missing)show_log("place round");
    CHECK(ordered&&log_n>=10&&!radar&&!missing&&handshakes==1,"place round: %d requests, ordered %d, radar %d, missing %d, %d handshakes",log_n,ordered,radar,missing,handshakes);
    CHECK(log_n>0&&!strcmp(log_req[0].path,"/stations/KBJC/observations/latest")&&!strcmp(log_req[1].path,"/gridpoints/BOU/63,62/forecast"),"place: first requests");
    CHECK(s->obs[0].temp_f!=WX_NA&&s->fc.n_local==3&&s->fc.n_ext>=5,"place: home figures (temp %d, %u periods, %u days)",s->obs[0].temp_f,s->fc.n_local,s->fc.n_ext);
    int regional=0;for(unsigned i=0;i<s->n_pages;i++)regional+=s->pages[i][0]==WX_P_STATE||s->pages[i][0]==WX_P_TRI||s->pages[i][0]==WX_P_RADAR;
    CHECK(!regional&&s->pages[s->n_pages-1][0]==WX_P_OUTLOOK,"place outside the built-in pack's state: regional or radar pages shown");
    pj_wx_diag(line,sizeof(line));
    CHECK(strstr(line," place=1 ")&&!strstr(line,"DENVER")&&!strstr(line,"39."),"diagnostics: %s",line);
    /* close: the place block goes, the cache stays; open again: read again, values kept, nothing asked */
    pj_wx_close();settle(200);
    CHECK(heap_used==cache_only&&heap_ours==cache_only,"after close %u bytes held (want the cache %u)",(unsigned)heap_used,(unsigned)cache_only);
    flash_reads=0;log_n=0;
    CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&flash_reads==4&&s->obs[0].temp_f!=WX_NA,"open again: place or values lost");
    settle(2000);run(300);
    CHECK(log_n==0,"open again within the periods asked %d requests",log_n);
    /* another place between opens: the held values were Denver's, so they go */
    pj_wx_close();settle(200);
    put_place(0,place_record2);put_place(1,place_record2);
    CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&s->place->pack&&s->place->radar,"the second place (inside a pack's radar view)");
    CHECK(s->obs[0].temp_f==WX_NA&&!s->fc.n_local,"another place's values were kept");
    /* 1.6.5: the same place with a location pack made for it: read whole, used (its whole table), its bytes held while
     * the channel is open and freed at close; a pack for another place, a damaged one, a torn newer copy */
    {
        char path[600];size_t n=0;
        snprintf(path,sizeof(path),"%s/second.pjlp",place_dir);
        uint8_t *lp=load(path,&n);
        CHECK(lp&&n<=WX_LPACK_COPY,"the second place's pack is missing");
        if(lp&&n<=WX_LPACK_COPY) {
            pj_wx_close();settle(200);
            memset(flash_pack,0xff,sizeof(flash_pack));memcpy(flash_pack[0],lp,n);
            flash_reads=0;pack_reads=0;maps_made=0;
            CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&s->place->builtin&&s->place->pack&&s->place->radar
                  &&wx_place_request_count(s->place)==s->place->pack->requests&&pack_reads==2&&maps_made==1&&maps_open==1,
                  "the pack was not used in place (%d pack reads, %d mappings)",pack_reads,maps_made);
            /* review M1 of 1.6.5: read in place from flash: only its index (wx_lpack) on the heap, not the pack's bytes */
            CHECK(heap_ours==cached_bytes+sizeof(wx_place)+sizeof(wx_lpack),"open with a pack holds %u bytes (want %u: the pack stays in flash)",
                  (unsigned)heap_ours,(unsigned)(cached_bytes+sizeof(wx_place)+sizeof(wx_lpack)));
            const uint8_t *in_page=(const uint8_t*)s->place->pack->name;
            CHECK(in_page>=flash_map_page&&in_page<flash_map_page+0x10000u,"the pack's names do not point into the mapped flash page");
            char diag[400];pj_wx_diag(diag,sizeof(diag));
            CHECK(strstr(diag," pack=map ")&&strstr(diag," heap-low="),"the diagnostics line does not say the pack is read in place: %s",diag);
            int maps=0;for(unsigned i=0;i<s->n_pages;i++)maps+=s->pages[i][0]==WX_P_STATE||s->pages[i][0]==WX_P_TRI;
            CHECK(maps==2,"the pack's two Regional Forecast pages are not shown");
            settle(3000);run(600);   /* rounds with the pack in use: its paths are read from the mapped page */
            CHECK(log_n>0&&maps_open==1,"no requests made from the mapped pack (%d)",log_n);
            pj_wx_close();settle(200);
            CHECK(heap_ours==cache_only&&maps_open==0,"after close with a pack %u bytes held (want %u), %d mappings open",(unsigned)heap_ours,
                  (unsigned)cache_only,maps_open);
            /* no free MMU page: the copy is read into one heap block instead (the first 1.6.5 build's way), freed at close */
            map_fail=1;pack_reads=0;
            CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&s->place->builtin&&pack_reads==3&&maps_open==0
                  &&heap_ours==cached_bytes+sizeof(wx_place)+sizeof(wx_lpack)+n,"the mapping refused: the heap copy was not used (%d reads, %u bytes)",
                  pack_reads,(unsigned)heap_ours);
            pj_wx_diag(diag,sizeof(diag));
            CHECK(strstr(diag," pack=heap ")!=NULL,"the diagnostics line does not say the pack is on the heap: %s",diag);
            pj_wx_close();settle(200);
            CHECK(heap_ours==cache_only,"after close with a heap pack %u bytes held (want %u)",(unsigned)heap_ours,(unsigned)cache_only);
            map_fail=0;
            /* a pack made for another place (here: on Denver's record) is read but not kept */
            put_place(0,place_record);put_place(1,place_record);
            CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&!s->place->builtin&&!s->place->pack&&heap_ours==cached_bytes+sizeof(wx_place),
                  "a pack for another place was used or kept (%u bytes)",(unsigned)heap_ours);
            pj_wx_close();settle(200);
            /* damaged A, good B (older): B; then both damaged: nothing kept */
            put_place(0,place_record2);put_place(1,place_record2);
            uint8_t *bad=malloc(n);memcpy(bad,lp,n);bad[n/2]^=0x40u;
            memcpy(flash_pack[1],lp,n);memcpy(flash_pack[0],bad,n);
            CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&s->place->builtin,"damaged A: the good B was not used");
            pj_wx_close();settle(200);
            memcpy(flash_pack[1],bad,n);
            CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&!s->place->builtin&&heap_ours==cached_bytes+sizeof(wx_place),
                  "two damaged copies: something kept (%u bytes)",(unsigned)heap_ours);
            pj_wx_close();settle(200);
            /* no memory for the pack's index: the place without it, nothing mapped */
            memcpy(flash_pack[0],lp,n);
            fail_alloc_at=3;   /* the cache is held: the place, the room to read it, then the pack's index */
            CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&!s->place->builtin&&heap_ours==cached_bytes+sizeof(wx_place)&&maps_open==0,
                  "no memory for the pack: %u bytes held, %d mappings",(unsigned)heap_ours,maps_open);
            fail_alloc_at=0;
            pj_wx_close();settle(200);
            /* no MMU page and no memory for the heap copy: the place without its pack */
            map_fail=1;fail_alloc_at=4;   /* the place, the room to read it, the index, then the copy */
            CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&!s->place->builtin&&heap_ours==cached_bytes+sizeof(wx_place)&&maps_open==0,
                  "no page and no memory for the pack: %u bytes held",(unsigned)heap_ours);
            fail_alloc_at=0;map_fail=0;
            pj_wx_close();settle(200);
            free(bad);
            memset(flash_pack,0xff,sizeof(flash_pack));
        }
        free(lp);
    }
    end_scenario("a weather location");
    /* not good records: the default pack, exactly its compiled-in list */
    static const struct {const char *name;int a,b;} bad[]={{"blank",0,0},{"zeros",1,1},{"a byte changed",2,2},{"flash read fails",3,3},
                                                           {"no memory for the place",4,4},{"no memory to read it",5,5}};
    for(unsigned c=0;c<sizeof(bad)/sizeof(bad[0]);c++) {
        reset_model();place_clock(0);cache_dir=home_dir;
        uint8_t broken[WX_PLACE_RECORD];
        memcpy(broken,place_record,sizeof(broken));broken[40]^=0x01u;
        put_place(0,NULL);put_place(1,NULL);
        if(c==1){memset(flash_place[0],0,4096);memset(flash_place[1],0,4096);}
        if(c==2){put_place(0,broken);put_place(1,broken);}
        if(c>=3){put_place(0,place_record);put_place(1,place_record);}
        flash_fail=c==3;
        if(c==4)fail_alloc_at=2;   /* the cache, then the place */
        if(c==5)fail_alloc_at=3;   /* the cache, the place, then the room to read it */
        CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&!s->place,"%s: not the default pack",bad[c].name);
        CHECK(heap_ours==cached_bytes,"%s: %u bytes held at open (want the cache %u)",bad[c].name,(unsigned)heap_ours,(unsigned)cached_bytes);
        flash_fail=0;
        wx_enter(s,now_ms);
        CHECK(settle(8000)>=0&&log_n==24&&!strcmp(log_req[0].path,WX_DEFAULT_PACK->req[0].path)&&!strcmp(log_req[log_n-1].host,WX_RADAR_HOST)
              &&s->obs[0].temp_f==55,"%s: the default pack's round (%d requests)",bad[c].name,log_n);
        pj_wx_diag(line,sizeof(line));
        CHECK(strstr(line," place=0 ")!=NULL,"%s: diagnostics %s",bad[c].name,line);
        end_scenario(bad[c].name);
    }
    /* A torn (half new, half erased), B the good older copy: B */
    reset_model();place_clock(1);cache_dir=place_dir;
    {uint8_t torn[WX_PLACE_RECORD];memcpy(torn,place_record2,sizeof(torn));memset(torn+128,0xff,WX_PLACE_RECORD-128);
     put_place(0,torn);put_place(1,place_record);}
    CHECK(pj_wx_open()==0&&(s=pj_wx_state())&&s->place&&!strcmp(s->place->name,"DENVER, CO"),"torn A: B not used");
    end_scenario("torn A");
    put_place(0,NULL);put_place(1,NULL);place_clock(0);cache_dir=home_dir;
    PASS("glue: weather location record (read at open, its list and pages, freed at close; blank, corrupt, unreadable, no memory, torn: the default pack)");
}

int main(int argc,char **argv) {
    if(argc!=3&&argc!=6){fprintf(stderr,"usage: check_weather_glue <cache folder> <radar fixture folder> [<place folder> <epoch> <HTTP date>]\n");return 2;}
    cache_dir=argv[1];fixture_dir=argv[2];
    memset(flash_place,0xff,sizeof(flash_place)); /* no weather location: as every console before 1.6.3 */
    test_first_round();
    test_stops();
    test_trouble();
    if(argc==6) {
        place_dir=argv[3];place_time=strtoll(argv[4],NULL,10);snprintf(place_date,sizeof(place_date),"%s",argv[5]);
        test_places();
    }
    printf("%s %d checks, %d failed\n",failures?"FAILED":"ALL PASS",checks,failures);
    return failures?1:0;
}
