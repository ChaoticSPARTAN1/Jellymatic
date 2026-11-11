/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * The launcher's single audited flash write (see provision_write.h). Read it whole: it is
 * meant to stay small enough to audit in one sitting.
 *
 * Order, and why a power cut at any point is safe (host test: scripts/test_chromatic_setup.py
 * cuts the model at every erase/write byte):
 *  1. read A and B; the newest valid record is "current" (provision.h, same rule as boot).
 *  2. build the edited record with sequence current+1; refuse (no write) if it would not parse.
 *  3. erase the OTHER sector, write the new record there, read it back and check that it is
 *     now the record boot would choose. Until this step finishes, the old sector is untouched
 *     and still wins (a torn new sector fails its CRC and counts as empty).
 *  4. erase the old sector (its stale secret goes). A cut here leaves a torn old sector,
 *     which counts as empty, and the verified new one wins.
 * Only PJ_PROV_FLASH_A and PJ_PROV_FLASH_B, 4096 bytes each, are ever erased or written: the
 * device wrappers below refuse any other address or size before touching esp_flash. */
#include <stdlib.h>
#include "provision_write.h"
#include "provision_build.h"
#define PJ_PW_SECTOR PJ_PROVISION_SECTOR
static int pj_pw_allowed(uint32_t offset,uint32_t size) {
    return (offset==PJ_PROV_FLASH_A||offset==PJ_PROV_FLASH_B)&&size==PJ_PW_SECTOR;
}
static int pj_pw_erased(const uint8_t *s){for(unsigned i=0;i<PJ_PW_SECTOR;i++)if(s[i]!=0xffu)return 0;return 1;}
static int pj_pw_same_record(const pj_prov2 *a,const pj_prov2 *b) {
    /* Equal apart from the sequence and version (a v1 record re-saved as v2 still counts as a change). */
    pj_prov2 x=*a,y=*b;x.sequence=y.sequence=0;x.version=y.version=0;
    int same=!memcmp(&x,&y,sizeof(x));memset(&x,0,sizeof(x));memset(&y,0,sizeof(y));return same;
}
int pj_provision_commit_with(const pj_prov_flash *flash,const pj_prov_edit *edit) {
    /* Work buffers on the heap only for the length of the commit: no static DRAM when idle. */
    uint8_t *a=calloc(1,PJ_PW_SECTOR),*b=calloc(1,PJ_PW_SECTOR),*image=calloc(1,PJ_PW_SECTOR);
    pj_prov2 current,next,check;int rc=PJ_PROV_WRITE_OK;
    memset(&current,0,sizeof(current));memset(&next,0,sizeof(next));memset(&check,0,sizeof(check));
    if(!a||!b||!image){rc=PJ_PROV_WRITE_NO_MEMORY;goto out;}
    if(!flash||!edit||flash->read(flash->ctx,PJ_PROV_FLASH_A,a,PJ_PW_SECTOR)||flash->read(flash->ctx,PJ_PROV_FLASH_B,b,PJ_PW_SECTOR))
        {rc=PJ_PROV_WRITE_READ_FAILED;goto out;}
    int have=pj_provision_current(a,b,PJ_PW_SECTOR,&current);
    /* Which sector holds the current record: the one whose own parse equals it (tie: A). */
    uint32_t old_at=PJ_PROV_FLASH_B,new_at=PJ_PROV_FLASH_A;
    if(have) {
        pj_prov2 ra;int in_a=pj_provision_any(a,PJ_PW_SECTOR,&ra)==1&&ra.sequence==current.sequence;
        memset(&ra,0,sizeof(ra));
        if(in_a){old_at=PJ_PROV_FLASH_A;new_at=PJ_PROV_FLASH_B;}
    }
    next=have?current:(pj_prov2){0};
    next.version=PJ_PROV2_VERSION;next.sequence=have?(current.sequence+1u?current.sequence+1u:1u):1u;
    if(edit->net_op==PJ_PROV_NET_SAVE||edit->net_op==PJ_PROV_NET_FORGET||edit->net_op==PJ_PROV_NET_FIRST) {
        unsigned at=next.count; /* the saved network of that name, else one past the end */
        for(unsigned i=0;i<next.count;i++)if(!strcmp(next.net[i].ssid,edit->net.ssid)){at=i;break;}
        if(edit->net_op==PJ_PROV_NET_SAVE) {
            if(at==next.count){if(next.count>=PJ_PROV2_NETWORKS){rc=PJ_PROV_WRITE_INVALID;goto out;}next.count++;}
            next.net[at]=edit->net;
        } else if(edit->net_op==PJ_PROV_NET_FIRST) {
            /* To the top of the list (the join priority); the others keep their order. Unknown or already
             * first: nothing changes (NO_CHANGE below). The password is the record's own, never the edit's. */
            if(at<next.count&&at>0) {
                pj_prov_network first=next.net[at];
                for(unsigned i=at;i>0;i--)next.net[i]=next.net[i-1];
                next.net[0]=first;memset(&first,0,sizeof(first));
            }
        } else if(at<next.count) {
            for(unsigned i=at;i+1<next.count;i++)next.net[i]=next.net[i+1];
            memset(&next.net[--next.count],0,sizeof(next.net[0]));
        }
    } else if(edit->net_op!=PJ_PROV_NET_KEEP){rc=PJ_PROV_WRITE_INVALID;goto out;}
    if(edit->set_address) { /* the address only, for the same sign-in (provision_write.h) */
        if(edit->set_jellyfin||edit->clear_jellyfin||!next.has_jellyfin||
           !memchr(edit->url,0,sizeof(edit->url))||!memchr(edit->user_id,0,sizeof(edit->user_id))||
           strcmp(next.user_id,edit->user_id)||!pj_prov_url(edit->url)){rc=PJ_PROV_WRITE_INVALID;goto out;}
        memset(next.url,0,sizeof(next.url));memcpy(next.url,edit->url,strlen(edit->url)); /* terminated (checked); zeros after, as parsed */
    }
    if(edit->clear_jellyfin) {
        next.has_jellyfin=0;memset(next.url,0,sizeof(next.url));memset(next.user_id,0,sizeof(next.user_id));
        memset(next.access_token,0,sizeof(next.access_token));memset(next.device_id,0,sizeof(next.device_id));
    }
    if(edit->set_jellyfin) {
        next.has_jellyfin=1;
        memcpy(next.url,edit->url,sizeof(next.url));memcpy(next.user_id,edit->user_id,sizeof(next.user_id));
        memcpy(next.access_token,edit->access_token,sizeof(next.access_token));memcpy(next.device_id,edit->device_id,sizeof(next.device_id));
    }
    if(have&&current.version==PJ_PROV2_VERSION&&pj_pw_same_record(&current,&next)){rc=PJ_PROV_WRITE_NO_CHANGE;goto out;}
    if(!pj_prov2_build(&next,image)){rc=PJ_PROV_WRITE_INVALID;goto out;}
    /* Step 3: the other sector. */
    if(!pj_pw_allowed(new_at,PJ_PW_SECTOR)){rc=PJ_PROV_WRITE_INVALID;goto out;}
    if(flash->erase(flash->ctx,new_at,PJ_PW_SECTOR)){rc=PJ_PROV_WRITE_ERASE_FAILED;goto out;}
    if(flash->write(flash->ctx,new_at,image,PJ_PW_SECTOR)){rc=PJ_PROV_WRITE_WRITE_FAILED;goto out;}
    {
        uint8_t *back=new_at==PJ_PROV_FLASH_A?a:b,*other=new_at==PJ_PROV_FLASH_A?b:a;
        if(flash->read(flash->ctx,new_at,back,PJ_PW_SECTOR)||memcmp(back,image,PJ_PW_SECTOR)||
           !pj_provision_current(a,b,PJ_PW_SECTOR,&check)||check.sequence!=next.sequence||
           pj_provision_any(back,PJ_PW_SECTOR,&check)!=1||check.sequence!=next.sequence){rc=PJ_PROV_WRITE_VERIFY_FAILED;goto out;}
        /* Step 4: the old copy goes, unless it was already empty. */
        if(!pj_pw_erased(other)) {
            if(!pj_pw_allowed(old_at,PJ_PW_SECTOR)){rc=PJ_PROV_WRITE_INVALID;goto out;}
            if(flash->erase(flash->ctx,old_at,PJ_PW_SECTOR))rc=PJ_PROV_WRITE_ERASE_FAILED; /* new record already in force */
        }
    }
out:
    if(a){memset(a,0,PJ_PW_SECTOR);free(a);}
    if(b){memset(b,0,PJ_PW_SECTOR);free(b);}
    if(image){memset(image,0,PJ_PW_SECTOR);free(image);}
    memset(&current,0,sizeof(current));memset(&next,0,sizeof(next));memset(&check,0,sizeof(check));
    return rc;
}
#ifdef ESP_PLATFORM
#include "esp_flash.h"
#include "esp_attr.h"
/* IDF refuses any erase or write inside the running app partition (CONFIG_SPI_FLASH_DANGEROUS_WRITE_ABORTS:
 * abort), and these two sectors are the last 8 KiB of the launcher's own slot (partitions.csv). So the
 * erase and write go through a private copy of the main flash chip whose hook table differs from IDF's in
 * one entry: region_protected lets through exactly sector A or sector B, whole, and nothing else. IDF's
 * own start/end hooks (bus lock, both CPUs' caches and interrupts) are kept unchanged, so the sound task
 * on the other core is paused as for any flash write. The copy lives on the stack for one commit; the
 * default chip, and every other caller, keep IDF's full protection (bootloader, table, running app).
 * Not esp_flash_app_disable_protect: that swaps in the no-OS hooks, which leave the other CPU running
 * with its cache off. */
typedef struct {esp_flash_os_functions_t os;esp_flash_t chip;} pj_pw_device;
static IRAM_ATTR esp_err_t pj_pw_region(void *arg,size_t start,size_t size) {
    (void)arg;
    return ((start==PJ_PROV_FLASH_A||start==PJ_PROV_FLASH_B)&&size==PJ_PW_SECTOR)?ESP_OK:ESP_ERR_NOT_SUPPORTED;
}
/* The only three esp_flash calls that may touch these sectors; the address check comes first. */
static int pj_pw_dev_read(void *ctx,uint32_t offset,void *out,uint32_t size) {
    (void)ctx;if(!pj_pw_allowed(offset,size))return -1;return esp_flash_read(NULL,out,offset,size)==ESP_OK?0:-1;
}
static int pj_pw_dev_erase(void *ctx,uint32_t offset,uint32_t size) {
    if(!ctx)return -1;
    (void)ctx;if(!pj_pw_allowed(offset,size))return -1;return esp_flash_erase_region(&((pj_pw_device*)ctx)->chip,offset,size)==ESP_OK?0:-1;
}
static int pj_pw_dev_write(void *ctx,uint32_t offset,const void *data,uint32_t size) {
    if(!ctx)return -1;
    (void)ctx;if(!pj_pw_allowed(offset,size))return -1;return esp_flash_write(&((pj_pw_device*)ctx)->chip,data,offset,size)==ESP_OK?0:-1;
}
int pj_provision_commit(const pj_prov_edit *edit) {
    pj_pw_device device;
    if(!esp_flash_default_chip||!esp_flash_default_chip->os_func||!esp_flash_default_chip->chip_drv)return PJ_PROV_WRITE_READ_FAILED; /* nothing written */
    device.os=*esp_flash_default_chip->os_func;
    device.os.region_protected=pj_pw_region;
    device.chip=*esp_flash_default_chip;
    device.chip.os_func=&device.os;
    const pj_prov_flash dev={&device,pj_pw_dev_read,pj_pw_dev_erase,pj_pw_dev_write};
    return pj_provision_commit_with(&dev,edit);
}
#endif
