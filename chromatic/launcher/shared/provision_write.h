/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * The launcher's ONE flash write: the provisioning record (docs/Design-notes.md),
 * two alternating sectors at the end of the launcher's own slot. Everything is in
 * provision_write.c; this header is the contract. Nothing else in the launcher may erase or
 * write flash (scripts/test_chromatic_launcher.py source_rules and ELF checks). */
#ifndef PJ_PROVISION_WRITE_H
#define PJ_PROVISION_WRITE_H
#include "provision.h"
/* Absolute flash addresses: launcher slot 0x260000 + PJ_PROVISION_OFFSET_A/B. */
#define PJ_PROV_FLASH_SLOT 0x260000u
#define PJ_PROV_FLASH_A (PJ_PROV_FLASH_SLOT+PJ_PROVISION_OFFSET_A) /* 0x3FE000 */
#define PJ_PROV_FLASH_B (PJ_PROV_FLASH_SLOT+PJ_PROVISION_OFFSET_B) /* 0x3FF000 */
enum {PJ_PROV_NET_KEEP,PJ_PROV_NET_SAVE,PJ_PROV_NET_FORGET,PJ_PROV_NET_FIRST};
/* What an on-console edit changes. Everything else (PC gateway, the networks not named, the
 * part not edited) is read from flash inside the writer and kept, so no password or token has
 * to stay in RAM to edit something else. */
typedef struct {
    uint8_t net_op;         /* PJ_PROV_NET_SAVE: add net, or replace the saved network of that name;
                             * PJ_PROV_NET_FORGET: drop the saved network of that name (psk unused);
                             * PJ_PROV_NET_FIRST: move the saved network of that name to the top of the list
                             * (psk unused; the list order is the join priority, launcher 1.2.9) */
    pj_prov_network net;
    uint8_t set_jellyfin;   /* replace the Jellyfin sign-in with url/user_id/access_token/device_id */
    uint8_t clear_jellyfin; /* forget the Jellyfin sign-in */
    uint8_t set_address;    /* launcher 1.3.0, "Find my server again": replace ONLY the server address with `url`,
                             * and only while the saved sign-in is the one for `user_id` (the server the owner
                             * picked answered as that user with this console's own token). The token, user id and
                             * device id stay exactly as saved on flash; nothing else changes. Refused (INVALID,
                             * nothing written) with no sign-in, another user, or set/clear_jellyfin in the same edit. */
    char url[129],user_id[33],access_token[65],device_id[65];
} pj_prov_edit;
/* Flash access for the writer. The device build supplies esp_flash_* inside provision_write.c;
 * host tests supply a model that also simulates power cuts. Offsets are absolute; return 0 ok. */
typedef struct {
    void *ctx;
    int (*read)(void *ctx,uint32_t offset,void *out,uint32_t size);
    int (*erase)(void *ctx,uint32_t offset,uint32_t size);
    int (*write)(void *ctx,uint32_t offset,const void *data,uint32_t size);
} pj_prov_flash;
enum {
    PJ_PROV_WRITE_OK=0,
    PJ_PROV_WRITE_READ_FAILED=-1,   /* could not read the sectors: nothing written */
    PJ_PROV_WRITE_INVALID=-2,       /* the edited record would not parse (or 5th network): nothing written */
    PJ_PROV_WRITE_ERASE_FAILED=-3,
    PJ_PROV_WRITE_WRITE_FAILED=-4,
    PJ_PROV_WRITE_VERIFY_FAILED=-5, /* read-back differs or is not the current record */
    PJ_PROV_WRITE_NO_MEMORY=-6,     /* no heap for the three 4 KiB work buffers: nothing written */
    PJ_PROV_WRITE_NO_CHANGE=1       /* the record already says this: nothing written */
};
/* Applies `edit` to the current record and writes the result, sequence + 1, to the sector that
 * does not hold the current record; verifies it; then erases the old sector so no older secret
 * survives a completed change. A power cut anywhere leaves at least one valid record (the old
 * one until the new one is verified, the new one after). Only PJ_PROV_FLASH_A and _B are ever
 * touched, 4096 bytes at a time. Wipes every secret from its own RAM before returning. */
int pj_provision_commit_with(const pj_prov_flash *flash,const pj_prov_edit *edit);
#ifdef ESP_PLATFORM
int pj_provision_commit(const pj_prov_edit *edit); /* the device entry: esp_flash_* on the two sectors */
#endif
#endif
