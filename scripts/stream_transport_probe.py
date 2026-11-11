# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""PC-only compressed stream experiments. No USB, FPGA or cartridge access.

Uses synthetic media by default. --jellyfin reads the private connection locally,
selects one accessible video without exposing its identity and passes only a
temporary loopback relay URL to FFmpeg. Results do not prove hardware decoding.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
import json
import math
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)
import shutil
import subprocess

from connection_config import ROOT, DEFAULT_CONFIG, load_connection
from jellyfin_client import JellyfinClient, media_relay


@dataclass(frozen=True)
class Profile:
    key: str
    video: str
    fps: int
    audio: str
    audio_rate: int
    video_bps: int = 0
    width: int = 160
    height: int = 144
    quality: int = 8
    audio_block: int = 256
    frame_rate: str = ''  # exact rational for the fps filter; empty means `fps`


PROFILES = (
    Profile('chromatic-mjpeg-30', 'mjpeg', 30, 'pcm_s16le', 22050),
    Profile('pocket-link-h264-24', 'libx264', 24, 'adpcm_ima_wav', 16000, 96000),
    Profile('pocket-link-h264-30', 'libx264', 30, 'adpcm_ima_wav', 16000, 96000),
    Profile('pocket-link-h264-60', 'libx264', 60, 'adpcm_ima_wav', 16000, 96000),
    Profile('pocket-link-jpeg-30', 'mjpeg', 30, 'adpcm_ima_wav', 16000, quality=24),
    Profile('pocket-link-jpeg-small-30', 'mjpeg', 30, 'adpcm_ima_wav', 16000, width=128, height=96, quality=16),
    Profile('pocket-link-jpeg-low-30', 'mjpeg', 30, 'adpcm_ima_wav', 16000, width=128, height=96, quality=24),
    Profile('pocket-link-jpeg-min-24', 'mjpeg', 24, 'adpcm_ima_wav', 16000, width=128, height=96, quality=16),
    Profile('pocket-link-jpeg-motion-60', 'mjpeg', 60, 'adpcm_ima_wav', 16000, width=128, height=96, quality=24),
    # Stored on the card, where bandwidth is not the limit. True 16:9, and the
    # title's own cadence, so film stays film. See film_profile().
    Profile('pocket-card-film-23976', 'mjpeg', 24, 'adpcm_ima_wav', 48000, width=256, height=144, quality=7,
            audio_block=1024, frame_rate='24000/1001'),
    Profile('pocket-card-film-24', 'mjpeg', 24, 'adpcm_ima_wav', 48000, width=256, height=144, quality=7,
            audio_block=1024),
    Profile('pocket-card-pal-25', 'mjpeg', 25, 'adpcm_ima_wav', 48000, width=256, height=144, quality=7,
            audio_block=1024),
    Profile('pocket-card-video-2997', 'mjpeg', 30, 'adpcm_ima_wav', 48000, width=256, height=144, quality=8,
            audio_block=1024, frame_rate='30000/1001'),
    Profile('pocket-card-video-30', 'mjpeg', 30, 'adpcm_ima_wav', 48000, width=256, height=144, quality=8,
            audio_block=1024),
    # Chromatic over home Wi-Fi: its whole 160x144 screen, letterboxed, at the title's own
    # cadence (24 fps or more, the project rule), decoded on the ESP32. Audio is sent so
    # the stream is complete; the stock FPGA ignores it until the media update.
    Profile('chromatic-wifi-film-23976', 'mjpeg', 24, 'adpcm_ima_wav', 24000, quality=8, audio_block=512,
            frame_rate='24000/1001'),
    Profile('chromatic-wifi-film-24', 'mjpeg', 24, 'adpcm_ima_wav', 24000, quality=8, audio_block=512),
    Profile('chromatic-wifi-pal-25', 'mjpeg', 25, 'adpcm_ima_wav', 24000, quality=8, audio_block=512),
    Profile('chromatic-wifi-video-2997', 'mjpeg', 30, 'adpcm_ima_wav', 24000, quality=8, audio_block=512,
            frame_rate='30000/1001'),
    Profile('chromatic-wifi-video-30', 'mjpeg', 30, 'adpcm_ima_wav', 24000, quality=8, audio_block=512),
)


def film_profile(rate):
    """Nearest card profile for a source frame rate, keeping its real cadence.

    Resampling 23.976 to 24 would repeat a frame every 42 seconds. Anything
    faster than 30 fps is stepped down to the closest rate the decoder can hold.
    """
    choices = (('pocket-card-film-23976', 24000 / 1001), ('pocket-card-film-24', 24.0),
               ('pocket-card-pal-25', 25.0), ('pocket-card-video-2997', 30000 / 1001),
               ('pocket-card-video-30', 30.0))
    if not rate or rate > 31:
        rate = rate / 2 if rate and rate <= 62 else 24000 / 1001
    key = min(choices, key=lambda choice: abs(choice[1] - rate))[0]
    return next(profile for profile in PROFILES if profile.key == key)


def chromatic_profile(rate):
    """Chromatic profile for a source frame rate, keeping the title's own cadence."""
    choices = (('chromatic-wifi-film-23976', 24000 / 1001), ('chromatic-wifi-film-24', 24.0),
               ('chromatic-wifi-pal-25', 25.0), ('chromatic-wifi-video-2997', 30000 / 1001),
               ('chromatic-wifi-video-30', 30.0))
    if not rate or rate > 31:
        rate = rate / 2 if rate and rate <= 62 else 24000 / 1001
    key = min(choices, key=lambda choice: abs(choice[1] - rate))[0]
    return next(profile for profile in PROFILES if profile.key == key)


def command(profile, source, destination, seconds):
    if profile not in PROFILES or not 1 <= seconds <= 30:
        raise ValueError('Invalid experiment profile/duration')
    return stream_command(profile, source, destination, seconds)


def stream_command(profile, source, destination, seconds=None, *, seek=0, silent_audio=False, ladder=()):
    """Shared encoder settings; a title stream ends at EOF, not a probe limit.

    Only the PC worker supplies the ephemeral source URL. The experiment entry
    point above retains its separate 30-second cap.
    """
    if (profile not in PROFILES or not math.isfinite(seek) or not 0 <= seek <= 172800
            or (seconds is not None and (not math.isfinite(seconds) or not 0 < seconds <= 172800))):
        raise ValueError('Invalid streaming settings')
    result = ['ffmpeg', '-nostdin', '-v', 'error', '-y']
    if source is None:
        result += ['-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=60',
                   '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
        audio = '1:a:0'
    else:
        if seek:
            result += ['-ss', str(seek)]
        result += ['-i', source]
        audio = '0:a:0?'
        if silent_audio:
            result += ['-f', 'lavfi', '-i', f'anullsrc=r={profile.audio_rate}:cl=mono']
            audio = '1:a:0'
    if seconds is not None:
        result += ['-t', str(seconds)]
    # Keep an audio clock through video EOF, including short/silent audio tracks.
    # Without this, a sparse title can stall an audio-master device before END.
    result += ['-af', 'apad', '-shortest']
    picture = (f'scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
               f'pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2,fps={profile.frame_rate or profile.fps}')
    if ladder and (profile.video != 'mjpeg' or ladder[0] != profile.quality
                   or list(ladder) != sorted(set(ladder)) or len(ladder) > 3 or ladder[-1] > 31):
        raise ValueError('Invalid quality ladder')
    if len(ladder) > 1:
        # One decode and scale, then the same pictures at each quality (adaptive_stream.py).
        labels = ''.join(f'[v{i}]' for i in range(len(ladder)))
        result += ['-filter_complex', f'[0:v:0]{picture},split={len(ladder)}{labels}']
        for i in range(len(ladder)):
            result += ['-map', f'[v{i}]']
        result += ['-map', audio, '-map_metadata', '-1', '-map_chapters', '-1',
                   '-c:v', 'mjpeg', '-pix_fmt', 'yuvj420p']
        for i, quality in enumerate(ladder):
            result += [f'-q:v:{i}', str(quality)]
        result += ['-huffman', 'default', '-threads:v', '1']
    else:
        result += ['-map', '0:v:0', '-map', audio,
            '-map_metadata', '-1', '-map_chapters', '-1',
            '-vf', picture,
            '-c:v', profile.video, '-pix_fmt', 'yuvj420p' if profile.video == 'mjpeg' else 'yuv420p']
    if profile.video == 'mjpeg' and len(ladder) <= 1:
        result += ['-q:v', str(profile.quality), '-huffman', 'default', '-threads:v', '1']
    elif profile.video != 'mjpeg':
        result += ['-preset', 'veryfast', '-tune', 'zerolatency', '-profile:v', 'main',
            '-b:v', str(profile.video_bps), '-maxrate', str(profile.video_bps),
            '-bufsize', str(profile.video_bps), '-g', str(profile.fps), '-bf', '0']
    result += ['-c:a', profile.audio, '-ar', str(profile.audio_rate), '-ac', '1',
               '-f', 'avi', str(destination)]
    if profile.audio == 'adpcm_ima_wav':
        result[-3:-3] = ['-block_size', str(profile.audio_block)]
    return result


def quiet_run(args, timeout):
    args = list(args)
    resolved = shutil.which(args[0])
    if not resolved:
        raise ValueError('Required local media tool unavailable')
    args[0] = resolved
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('Local media experiment failed')
    return result.stdout


def budget(packets, seconds, rate=25600, startup=1.0):
    """Ideal serialized transport, reserving 20% of the 256 kbit/s link.

    Charged overhead is 32 bytes per AV packet plus 16 per 4096-byte fragment.
    This is a conservative planning model, NOT a measured link driver.
    """
    if not math.isfinite(seconds) or seconds <= 0 or rate <= 0 or startup < 0:
        raise ValueError('Invalid transport model')
    charged = 0
    worst = 0.0
    payload = 0
    timeline = []
    for packet in packets:
        size = int(packet['size'])
        timestamp = float(packet.get('pts_time', packet.get('dts_time', 0)))
        if not 0 < size <= 4 * 1024 * 1024 or not math.isfinite(timestamp):
            raise ValueError('Invalid media packet')
        timeline.append((max(0, timestamp), size))
    for timestamp, size in sorted(timeline):
        payload += size
        charged += size + 32 + math.ceil(size / 4096) * 16
        worst = max(worst, charged / rate - timestamp - startup)
    return {'payload_bytes_per_second': round(payload / seconds),
        'framed_bytes_per_second': round(charged / seconds),
        'assumed_usable_link_bytes_per_second': rate, 'startup_buffer_seconds': startup,
        'ideal_link_deadlines_met': charged / seconds <= rate and worst <= 0,
        'additional_buffer_needed_seconds': round(max(0, worst), 3),
        'hardware_link_measured': False, 'hardware_decoder_tested': False}


def probe(profile, source, folder, seconds):
    destination = folder / (profile.key + '.avi')
    quiet_run(command(profile, source, destination, seconds), 180)
    details = json.loads(quiet_run(['ffprobe', '-v', 'error', '-show_packets',
        '-show_streams', '-show_format', '-show_entries',
        'packet=size,pts_time,dts_time:stream=codec_type,codec_name,nb_frames,r_frame_rate:format=duration',
        '-of', 'json', str(destination)], 30))
    if not details.get('packets'):
        raise ValueError('No encoded packets')
    duration = float(details['format']['duration'])
    # Also prove FFmpeg can decode the whole result, including audio tail.
    quiet_run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(destination),
               '-f', 'null', '-'], 90)
    return {'profile': profile.key, 'fps': profile.fps, 'width': profile.width, 'height': profile.height,
        'video_codec': profile.video, 'audio_codec': profile.audio,
        'audio_sample_rate': profile.audio_rate, 'duration_seconds': duration,
        'container_bytes': destination.stat().st_size,
        'streams': details.get('streams', []), 'pc_full_decode_passed': True,
        **budget(details['packets'], duration)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jellyfin', action='store_true')
    parser.add_argument('--seconds', type=int, default=4, choices=range(1, 31))
    parser.add_argument('--output', type=Path, default=PRIVATE / 'stream-transport-probes')
    args = parser.parse_args()
    from datetime import datetime
    folder = args.output.resolve() / datetime.now().strftime('%Y%m%d-%H%M%S')
    if not folder.is_relative_to(PRIVATE.resolve()):
        raise ValueError('Media experiment output must stay private')
    folder.mkdir(parents=True, exist_ok=False)
    client = None
    if args.jellyfin:
        client = JellyfinClient(load_connection(DEFAULT_CONFIG))
        client.connect()
        items = client.json('/Items', {'userId': client.user_id, 'recursive': 'true',
            'includeItemTypes': 'Movie,Episode,Video', 'limit': 1}).get('Items', [])
        if not items:
            raise ValueError('No accessible video')
        relay = media_relay(client, items[0]['Id'])
    else:
        relay = nullcontext(None)
    results = []
    with relay as source:
        for profile in PROFILES:
            result = probe(profile, source, folder, args.seconds)
            results.append(result)
            print(f"{profile.key}: PC encode/decode passed; framed payload "
                  f"{result['framed_bytes_per_second']} B/s; ideal link budget "
                  f"{'fits' if result['ideal_link_deadlines_met'] else 'exceeded'}. "
                  'Hardware untested.', flush=True)
    report = {'source': 'private-jellyfin' if args.jellyfin else 'synthetic',
        'hardware_accessed': False, 'credentials_exported': False, 'profiles': results}
    (folder / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Local stream probe failed; no server details were output.')
        raise SystemExit(1)
