# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Incremental compressed title streaming, with PC-only authentication.

No hardware access or listener. Only a live worker imports PyAV. Neither a full
movie nor an ever-growing packet/metrics list is retained in memory or on disk.
"""
from contextlib import nullcontext
import json
import math
import shutil
import subprocess

from av_stream import (Packet, encode, configuration, CONFIG, VIDEO, AUDIO, END,
                       KEYFRAME, JPEG, H264)
from jellyfin_client import media_relay
from jpeg_payload import compact
from stream_transport_probe import stream_command


def packets(container, profile, session, duration=None, first_frame=None, adapt=None):
    """Shared packetization for finite probes and full-title workers.

    With `adapt` (adaptive_stream.Adaptation) the container holds one video stream per
    quality; each picture is held until every copy has arrived, then the copy for the
    current level is sent. Copies complete in timestamp order, so output stays ordered.
    """
    if profile.audio != 'adpcm_ima_wav':
        raise ValueError('Unsupported audio profile')
    codec = JPEG if profile.video == 'mjpeg' else H264
    if adapt is not None and codec != JPEG:
        raise ValueError('Adaptive quality needs JPEG pictures')
    sequence = 0
    copies = len(adapt.qualities) if adapt is not None else 1
    ranks = {}
    held = {}
    frame_index = 0

    def make(kind, payload=b'', pts=0, flags=0):
        nonlocal sequence
        if sequence >= 0xffffffff:
            raise ValueError('Session sequence exhausted')
        packet = Packet(kind, session, sequence, pts, payload, flags)
        encode(packet)  # Apply the same length/format bounds before handoff.
        sequence += 1
        return packet

    yield make(CONFIG, configuration(profile.width, profile.height, profile.fps,
        codec, profile.audio_rate, block=profile.audio_block))
    audio_blocks = video_frames = 0
    video_end = 0
    for packet in container.demux():
        if not packet.size:
            continue
        if packet.pts is None or packet.time_base is None:
            raise ValueError('Encoder omitted timestamps')
        pts = max(0, int(packet.pts * packet.time_base * 90000))
        data = bytes(packet)
        if packet.stream.type == 'video' and copies > 1:
            rank = ranks.setdefault(packet.stream.index, len(ranks))
            if rank >= copies:
                raise ValueError('Unexpected video stream')
            picture = held.setdefault(pts, {})
            if rank in picture or len(held) > 16:
                raise ValueError('Quality copies out of step')
            picture[rank] = data
            if len(picture) < copies:
                continue
            del held[pts]
            if any(stamp < pts for stamp in held):
                raise ValueError('Quality copies out of order')
            video_end = max(video_end, pts + math.ceil(90000 / profile.fps))
            keep = adapt.keep(frame_index)
            frame_index += 1
            if not keep:
                continue
            original = picture[adapt.rank()]
            data, geometry = compact(original)
            if geometry != (profile.width, profile.height):
                raise ValueError('Video geometry changed')
            if not video_frames and first_frame:
                first_frame(original, data)
            yield make(VIDEO, data, pts, KEYFRAME)
            video_frames += 1
        elif packet.stream.type == 'video':
            if codec == JPEG:
                original = data
                data, geometry = compact(data)
                if geometry != (profile.width, profile.height):
                    raise ValueError('Video geometry changed')
                if not video_frames and first_frame:
                    first_frame(original, data)
                keyframe = True
            else:
                if not data.startswith((b'\0\0\1', b'\0\0\0\1')):
                    raise ValueError('Video is not Annex-B')
                keyframe = packet.is_keyframe
            yield make(VIDEO, data, pts, KEYFRAME if keyframe else 0)
            video_frames += 1
            video_end = max(video_end, pts + math.ceil(90000 / profile.fps))
        elif packet.stream.type == 'audio':
            block = profile.audio_block
            if packet.stream.codec_context.name != 'adpcm_ima_wav' or len(data) % block:
                raise ValueError('Independent IMA-WAV blocks required')
            samples = 1 + 2 * (block - 4)
            for index, offset in enumerate(range(0, len(data), block)):
                stamp = pts + index * samples * 90000 // profile.audio_rate
                yield make(AUDIO, data[offset:offset + block], stamp)
                audio_blocks += 1
    if not video_frames or not audio_blocks or held:
        raise ValueError('Incomplete encoded session')
    # Stop at the video timeline, trimming the codec's padded final audio block.
    # An early server EOF remains early; never claim the requested length played.
    end = min(video_end, round(duration * 90000)) if duration is not None else video_end
    yield make(END, pts=end)


def encoded_stream(client, identifier, profile, session, seek, duration, stop, process, local_path=None,
                   adapt=None):
    """Blocking generator; caller owns cancellation, queue bounds and watchdog.

    All upstream URLs/auth stay behind the temporary loopback media relay.
    FFmpeg errors are discarded and PyAV diagnostics silenced, not reflected.
    client=None reads local_path, or a synthetic source when that is also None.
    """
    import av
    av.logging.set_level(av.logging.PANIC)
    relay = media_relay(client, identifier) if client is not None else nullcontext(local_path)
    with relay as source:
        silent = False
        if source:
            probe = subprocess.Popen([shutil.which('ffprobe'), '-v', 'error', '-select_streams', 'a:0',
                '-show_entries', 'stream=index', '-of', 'json', source],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            process(probe)
            try:
                raw, _ = probe.communicate(timeout=15)
                if probe.returncode or stop.is_set():
                    raise ValueError('Local media inspection failed')
                details = json.loads(raw)
            finally:
                if probe.poll() is None:
                    probe.kill()
                    probe.wait(timeout=3)
                process(None)
            silent = not bool(details.get('streams'))
        args = stream_command(profile, source, 'pipe:1', duration, seek=seek, silent_audio=silent,
                              ladder=adapt.qualities if adapt is not None else ())
        args[-1:-1] = ['-flush_packets', '1']
        args[0] = shutil.which('ffmpeg')
        if not args[0] or stop.is_set():
            raise ValueError('Encoder unavailable or cancelled')
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        process(proc)
        try:
            class ReadPipe:
                def read(self, size):
                    return proc.stdout.read(size)
                def readable(self):
                    return True
                def seekable(self):
                    return False
            with av.open(ReadPipe(), format='avi', buffer_size=4096) as container:
                for packet in packets(container, profile, session, duration, adapt=adapt):
                    if stop.is_set():
                        break
                    # END is released only after the encoder succeeds, below.
                    if packet.kind == END:
                        ending = encode(packet)
                    else:
                        yield encode(packet)
            if stop.is_set() or proc.wait(timeout=10):
                raise ValueError('Encoder interrupted')
            yield ending
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            proc.stdout.close()
            process(None)
