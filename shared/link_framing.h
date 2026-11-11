/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md */
#ifndef PJ_LINK_FRAMING_H
#define PJ_LINK_FRAMING_H
#include <stddef.h>
#include <stdint.h>
#define PJ_WIRE_LIMIT 32800u
#define PJ_WIRE_FLAG 0x7eu
#define PJ_WIRE_ESCAPE 0x7du
#define PJ_WIRE_IDLE 0xffu
typedef struct {
    uint8_t *buffer;
    size_t capacity,length;
    uint8_t active,escaped;
} pj_wire_rx;
static inline int pj_wire_rx_init(pj_wire_rx *r,uint8_t *buffer,size_t capacity) {
    if(!r||!buffer||capacity<4||capacity>PJ_WIRE_LIMIT)return 0;
    r->buffer=buffer;r->capacity=capacity;r->length=0;r->active=0;r->escaped=0;
    return 1;
}
/* 1: complete frame in caller-owned buffer; 0: incomplete/idle; -1: dropped.
 * Consume a complete frame before feeding another byte into this state. */
static inline int pj_wire_rx_byte(pj_wire_rx *r,uint8_t byte,size_t *frame_size) {
    if(byte==PJ_WIRE_IDLE)return 0;
    if(byte==PJ_WIRE_FLAG) {
        int result=0;
        if(r->active&&(r->length||r->escaped)) {
            if(!r->escaped&&r->length>=4){*frame_size=r->length;result=1;}
            else result=-1;
        }
        r->length=0;r->active=1;r->escaped=0;
        return result;
    }
    if(!r->active)return 0;
    if(r->escaped) {
        if(byte!=(PJ_WIRE_FLAG^0x20)&&byte!=(PJ_WIRE_ESCAPE^0x20)&&byte!=(PJ_WIRE_IDLE^0x20)) {
            r->active=0;r->escaped=0;r->length=0;return -1;
        }
        byte^=0x20;r->escaped=0;
    } else if(byte==PJ_WIRE_ESCAPE){r->escaped=1;return 0;}
    if(r->length>=r->capacity){r->active=0;r->escaped=0;r->length=0;return -1;}
    r->buffer[r->length++]=byte;return 0;
}
typedef struct {
    const uint8_t *data;
    size_t size,offset;
    uint8_t phase,pending,escaped_byte;
} pj_wire_tx;
static inline int pj_wire_tx_init(pj_wire_tx *t,const uint8_t *data,size_t size) {
    if(!t||!data||size<4||size>PJ_WIRE_LIMIT)return 0;
    t->data=data;t->size=size;t->offset=0;t->phase=0;t->pending=0;t->escaped_byte=0;
    return 1;
}
/* Generates one byte at a time without a second escaped-frame allocation.
 * Caller retains input bytes until this returns 0 and calls only when the
 * transmit FIFO has room; otherwise stop and resume on the next service tick. */
static inline int pj_wire_tx_next(pj_wire_tx *t,uint8_t *out) {
    if(t->phase==0){*out=PJ_WIRE_FLAG;t->phase=1;return 1;}
    if(t->pending){*out=t->escaped_byte;t->pending=0;return 1;}
    if(t->offset<t->size) {
        uint8_t byte=t->data[t->offset++];
        if(byte==PJ_WIRE_FLAG||byte==PJ_WIRE_ESCAPE||byte==PJ_WIRE_IDLE) {
            *out=PJ_WIRE_ESCAPE;t->escaped_byte=byte^0x20;t->pending=1;
        } else *out=byte;
        return 1;
    }
    if(t->phase==1){*out=PJ_WIRE_FLAG;t->phase=2;return 1;}
    return 0;
}
#endif
