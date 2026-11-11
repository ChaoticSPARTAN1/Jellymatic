# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Byte framing over full-duplex link-port serial, including polling idles.

0xff is an idle byte supplied by an empty physical transmitter. It is ignored
even between an escape prefix and its value. Delimiters resynchronize packets.
The enclosed PJB1/PJAV packet supplies its own length, sequence and CRC.
"""
FLAG,ESCAPE,IDLE=0x7e,0x7d,0xff
LIMIT=32768+32


def encode_frame(frame):
    if not 4<=len(frame)<=LIMIT:
        raise ValueError('Invalid link frame size')
    out=bytearray([FLAG])
    for byte in frame:
        if byte in (FLAG,ESCAPE,IDLE):out.extend((ESCAPE,byte^0x20))
        else:out.append(byte)
    out.append(FLAG)
    return bytes(out)


class Decoder:
    def __init__(self,capacity=LIMIT):
        if not 4<=capacity<=LIMIT:raise ValueError('Invalid link receive bound')
        self.capacity=capacity
        self.buffer=bytearray()
        self.active=False
        self.escaped=False
        self.errors=0

    def feed(self,data):
        result=[]
        for byte in data:
            if byte==IDLE:continue
            if byte==FLAG:
                if self.active and (self.buffer or self.escaped):
                    if not self.escaped and len(self.buffer)>=4:result.append(bytes(self.buffer))
                    else:self.errors+=1
                self.buffer.clear();self.active=True;self.escaped=False
                continue
            if not self.active:continue
            if self.escaped:
                if byte not in (FLAG^0x20,ESCAPE^0x20,IDLE^0x20):
                    self.errors+=1;self.active=False;self.buffer.clear();self.escaped=False
                    continue
                byte^=0x20;self.escaped=False
            elif byte==ESCAPE:
                self.escaped=True;continue
            if len(self.buffer)>=self.capacity:
                self.errors+=1;self.buffer.clear();self.active=False;self.escaped=False
            else:self.buffer.append(byte)
        return result

    def finish(self):
        if self.buffer or self.escaped:
            self.errors+=1;self.buffer.clear();self.active=False;self.escaped=False
            return False
        return True
