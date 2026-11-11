# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic Jellyfin player (chromatic/jellyfin/jf_app.h) on the PC, against a fake server.

Synthetic library and artwork only; the video stream is the real server sample in
private/chromatic-samples/sample.avi when present (scripts/capture_chromatic_sample.py),
else a synthetic MJPEG AVI made here. No hardware, no real server, nothing private printed.
Writes screen previews to staging/chromatic-player/*.png for review.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from user_paths import PRIVATE  # noqa: E402  (per-user folder, see user_paths.py)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'staging' / 'chromatic-player'
OUT.mkdir(parents=True, exist_ok=True)  # build output, ignored by Git
GCC = shutil.which('gcc') or r'C:\msys64\ucrt64\bin\gcc.exe'
USER = '0123456789abcdef0123456789abcdef'
TICKS = 10_000_000


def ident(n):
    return f'{n:032x}'


MOVIES, SHOWS = ident(1), ident(2)
SERIES, SEASON = ident(10), ident(11)
EPISODES = [ident(20 + i) for i in range(15)]
FILMS = [ident(40 + i) for i in range(5)]


def item(i, name, kind, **extra):
    base = {'Id': i, 'Name': name, 'Type': kind, 'IsFolder': kind in ('CollectionFolder', 'Series', 'Season'),
            'ImageTags': {'Primary': 'p'}, 'BackdropImageTags': ['b'],
            'UserData': {'PlaybackPositionTicks': 0, 'Played': False}}
    base.update(extra)
    return base


def episode(n, position=0):
    return item(EPISODES[n], f'Episode name {n + 1}', 'Episode', SeriesName='Synthetic Show', SeriesId=SERIES,
                SeriesPrimaryImageTag='s', ParentIndexNumber=1, IndexNumber=n + 1, RunTimeTicks=1500 * TICKS,
                UserData={'PlaybackPositionTicks': position * TICKS, 'Played': n == 0})


def film(n):
    return item(FILMS[n], f'Synthetic Film {n + 1}', 'Movie', ProductionYear=2001 + n, RunTimeTicks=(5400 + 60 * n) * TICKS)


def avi_chunks(data, pos, end):
    """Yields (fourcc, start, size) for the RIFF chunks in data[pos:end]; a size of 0xFFFFFFFF (a live
    transcode's unknown size) or one past the end means 'to the end'."""
    while pos + 8 <= end:
        fourcc, size = data[pos:pos + 4], struct.unpack('<I', data[pos + 4:pos + 8])[0]
        if size == 0xFFFFFFFF or pos + 8 + size > end:
            size = end - pos - 8
        yield fourcc, pos + 8, size
        pos += 8 + size + (size & 1)


def capped_avi(src, dst, cap):
    """The server's side of maxFramerate: the stream at min(title rate, cap) pictures per second.
    Pictures are dropped the way ffmpeg -r does (JPEG bytes unchanged), avih and the video strh say
    the new rate, audio chunks are kept. Returns the source itself when the cap does not bind."""
    data = bytearray(src.read_bytes())
    rate = scale = None
    strh_at = avih_at = None
    movi = None
    for fourcc, at, size in avi_chunks(data, 12, len(data)):
        if fourcc != b'LIST':
            continue
        kind = data[at:at + 4]
        if kind == b'hdrl':
            for f2, at2, size2 in avi_chunks(data, at + 4, at + size):
                if f2 == b'avih':
                    avih_at = at2
                elif f2 == b'LIST' and data[at2:at2 + 4] == b'strl':
                    for f3, at3, _ in avi_chunks(data, at2 + 4, at2 + size2):
                        if f3 == b'strh' and data[at3:at3 + 4] == b'vids' and strh_at is None:
                            strh_at = at3
                            scale, rate = struct.unpack('<II', data[at3 + 20:at3 + 28])
        elif kind == b'movi':
            movi = (at - 8, at + 4, at + size)
    if not (rate and scale and movi and avih_at is not None):
        return src
    fps = rate / scale
    if fps <= cap + 1e-6:
        return src
    out = bytearray(data[:movi[0]])
    body = bytearray()
    index = kept = 0
    for fourcc, at, size in avi_chunks(data, movi[1], movi[2]):
        if fourcc in (b'00dc', b'00db'):
            keep = int((index + 1) * cap / fps) > int(index * cap / fps)
            index += 1
            if not keep:
                continue
            kept += 1
        elif fourcc == b'idx1':
            continue
        body += data[at - 8:at + size] + (b'\0' if size & 1 else b'')
    out += b'LIST' + struct.pack('<I', 4 + len(body)) + b'movi' + body
    struct.pack_into('<I', out, avih_at, round(1_000_000 / cap))
    struct.pack_into('<I', out, avih_at + 16, kept)
    struct.pack_into('<II', out, strh_at + 20, 1, cap)
    struct.pack_into('<I', out, strh_at + 32, kept)
    struct.pack_into('<I', out, 4, len(out) - 8)
    dst.write_bytes(out)
    return dst


def video_bytes_per_s(avi):
    """The sample's own video rate (bytes of pictures per second of pictures), from its chunks."""
    data = avi.read_bytes()
    us = pictures = video = 0
    for fourcc, at, size in avi_chunks(data, 12, len(data)):
        if fourcc == b'LIST':
            kind = data[at:at + 4]
            if kind == b'hdrl':
                for f2, at2, _ in avi_chunks(data, at + 4, at + size):
                    if f2 == b'avih':
                        us = struct.unpack('<I', data[at2:at2 + 4])[0]
            elif kind == b'movi':
                for f2, _, size2 in avi_chunks(data, at + 4, at + size):
                    if f2 in (b'00dc', b'00db'):
                        pictures += 1
                        video += size2
    return video / (pictures * us / 1e6) if pictures and us else 0


def rated_avi(src, dst, rate):
    """The server's side of videoBitRate: the pictures re-encoded by FFmpeg at `rate` bit/s (sound
    copied as is, same interleave), when the sample's own video rate is above it; else the source."""
    if dst.exists():
        return dst
    if video_bytes_per_s(src) * 8 <= rate * 1.1:
        return src
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-c:v', 'mjpeg', '-b:v', str(rate),
                    '-maxrate', str(rate), '-bufsize', str(rate // 4), '-c:a', 'copy', str(dst)], check=True)
    return dst


class FakeServer:
    def __init__(self, folder, stream):
        self.folder, self.stream = folder, stream
        self.requests, self.bodies, self.streams = [], [], []
        self.fail = {}  # path prefix -> status
        self.fail_once = {}  # path prefix -> status, for the first matching request only
        self.honour_bitrate = False  # re-encode for videoBitRate (slow): only the checks that step the quality down
        self.media_streams = None  # 1.3.2: the title's MediaStreams (None: Japanese default + English audio, English subtitle)
        self.subs_stream = None  # 1.3.2: a request with subtitleMethod=Encode gets 'missing' (no stream) or 'empty' (no bytes)

    def stream_file(self, path):
        """The AVI for a stream request: the sample, capped to its maxFramerate like the real server, and
        (when honour_bitrate is set) re-encoded at its videoBitRate when the sample's own rate is higher."""
        src = self.stream
        if 'subtitleMethod=Encode' in path and self.subs_stream == 'missing':
            return self.folder / 'no-such-stream.avi'  # the server refused: the console cannot open the stream
        if 'subtitleMethod=Encode' in path and self.subs_stream == 'empty':
            empty = self.folder / 'empty-stream.avi'
            empty.write_bytes(b'')
            return empty  # opened, then nothing: the server's encoder gave up
        m = re.search(r'maxFramerate=(\d+)', path)
        if m:
            cap = int(m.group(1))
            src = capped_avi(src, self.folder / f'{src.stem}-fps{cap}.avi', cap)
        m = re.search(r'videoBitRate=(\d+)', path)
        if m and self.honour_bitrate:
            rate = int(m.group(1))
            src = rated_avi(src, self.folder / f'{src.stem}-rate{rate}.avi', rate)
        return src

    def image(self, w, h, seed):
        path = self.folder / f'img_{w}x{h}_{seed}.jpg'
        if not path.exists():
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            f'gradients=s={w}x{h}:seed={seed}:n=2', '-frames:v', '1', '-q:v', '4', str(path)], check=True)
        return path.read_bytes()

    def reply(self, method, path):
        self.requests.append((method, path))
        for prefix, status in list(self.fail_once.items()):
            if path.startswith(prefix):
                del self.fail_once[prefix]
                return status, b''
        for prefix, status in self.fail.items():
            if path.startswith(prefix):
                return status, b''
        route, _, query = path.partition('?')
        q = dict(part.split('=', 1) for part in query.split('&') if '=' in part)
        if method == 'POST':
            return 204, b''
        if route.startswith('/Items/') and '/Images/' in route:
            seed = int(route.split('/')[2][-4:], 16) % 97
            return 200, self.image(int(q['fillWidth']), int(q['fillHeight']), seed)
        if 'userId' in q and q['userId'] != USER:
            return 400, b''
        if route == '/UserViews':
            body = {'Items': [item(MOVIES, 'Movies', 'CollectionFolder'), item(SHOWS, 'Shows', 'CollectionFolder')]}
        elif route == '/UserItems/Resume':
            body = {'Items': [episode(3, 300), film(1) | {'UserData': {'PlaybackPositionTicks': 1200 * TICKS}}]}
        elif route == '/Shows/NextUp':
            body = {'Items': [episode(4)]}
        elif route == '/Items/Latest':
            body = [film(i) for i in range(5)] if q.get('parentId') == MOVIES else [
                item(SERIES, 'Synthetic Show', 'Series', ProductionYear=2019)]
        elif route == '/Items' and 'ids' in q:  # Details or the lean track list, both the list form
            wanted = q['ids']
            pool = [film(i) for i in range(5)] + [episode(i, 300 if i == 3 else 0) for i in range(15)]
            match = next((x for x in pool if x['Id'] == wanted), None)
            if not match:
                return 200, json.dumps({'Items': [], 'TotalRecordCount': 0}).encode()
            if 'MediaStreams' in q.get('fields', ''):  # English-audio track selection (jf_pick_streams)
                streams = self.media_streams or [
                    {'Type': 'Video', 'Index': 0, 'Codec': 'h264'},
                    {'Type': 'Audio', 'Index': 1, 'Language': 'jpn', 'IsDefault': True},
                    {'Type': 'Audio', 'Index': 2, 'Language': 'eng', 'IsDefault': False},
                    {'Type': 'Subtitle', 'Index': 3, 'Language': 'eng', 'IsDefault': False, 'Codec': 'subrip',
                     'IsTextSubtitleStream': True}]
                body = {'Items': [{'Id': wanted, 'MediaStreams': streams}], 'TotalRecordCount': 1}
                return 200, json.dumps(body).encode()
            body = {'Items': [dict(match, Overview='A synthetic overview that is long enough to wrap over several '
                                         'lines on the small screen, to check the layout.',
                                   Genres=['Drama', 'Comedy'])], 'TotalRecordCount': 1}
        elif route == '/Items':
            parent, start, limit = q.get('parentId'), int(q.get('startIndex', 0)), int(q.get('limit', 12))
            everything = {MOVIES: [film(i) for i in range(5)],
                          SHOWS: [item(SERIES, 'Synthetic Show', 'Series')],
                          SERIES: [item(SEASON, 'Season 1', 'Season')],
                          SEASON: [episode(i) for i in range(15)]}.get(parent, [])
            body = {'Items': everything[start:start + limit], 'TotalRecordCount': len(everything)}
        elif re.fullmatch(r'/Items/[0-9a-f]{32}', route):
            wanted = route.split('/')[2]
            pool = [film(i) for i in range(5)] + [episode(i, 300 if i == 3 else 0) for i in range(15)]
            match = next((x for x in pool if x['Id'] == wanted), None)
            if not match:
                return 404, b''
            body = dict(match, Overview='A synthetic overview that is long enough to wrap over several lines '
                                        'on the small screen, to check the layout.', Genres=['Drama', 'Comedy'])
        else:
            return 404, b''
        return 200, json.dumps(body).encode()


def run(exe, server, script, env=None, name='run'):
    shots = OUT / name
    shots.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(exe), script, str(shots)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env={**os.environ, **(env or {})})
    # Drain stderr concurrently: the harness writes a STATE/SOUND/UI block per `state` step, and a long
    # script's blocks overrun the OS pipe buffer and deadlock the child if stderr is read only at the end.
    err_chunks = []
    err_thread = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    err_thread.start()
    while True:
        line = proc.stdout.readline().decode()
        if not line:
            break
        if line.startswith('REQ '):
            _, method, path = line.strip().split(' ', 2)
            if method == 'POST':
                header = proc.stdout.readline().decode()
                n = int(header.split()[1])
                server.bodies.append((path, json.loads(proc.stdout.read(n))))
            status, data = server.reply(method, path)
            proc.stdin.write(f'REP {status} {len(data)}\n'.encode() + data)
            proc.stdin.flush()
        elif line.startswith('STREAM '):
            server.streams.append(line.strip()[7:])
            name_bytes = str(server.stream_file(server.streams[-1])).encode()
            proc.stdin.write(f'FILE {len(name_bytes)}\n'.encode() + name_bytes)
            proc.stdin.flush()
        elif line.strip() == 'END':
            break
    err_thread.join(timeout=60)
    err = (err_chunks[0] if err_chunks else b'').decode()
    code = proc.wait(timeout=60)
    states = [l for l in err.splitlines() if l.startswith('STATE')]
    run.ui = [l for l in err.splitlines() if l.startswith('UI ')]  # menu, layout and scrolling (one per state)
    run.err = err  # SOUND lines (one per state) when the sound model is on
    size = next((int(l.split('=')[1]) for l in err.splitlines() if l.startswith('SIZE app=')), 0)
    assert 0 < size <= 56 * 1024, ('app must fit the console (about 75 KB free with Wi-Fi)', size)
    run.size = size
    if code:
        raise AssertionError(f'{name}: harness exit {code}: {err[-800:]}')
    for ppm in shots.glob('*.ppm'):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(ppm), '-vf', 'scale=480:432:flags=neighbor',
                        str(ppm.with_suffix('.png'))], check=True)
        ppm.unlink()
    return states


def field(state, key):
    return re.search(rf'{key}=(\S*)', state).group(1)


# Console figures, launcher 1.2.4 on the stock FPGA (docs/Design-notes.md diagnostics):
# free heap while playing 22-24 KB, lowest 6.6 KB; its sizes then: two 5,120 B task stacks, an 8,192 B stream
# buffer, 10 dynamic Wi-Fi RX buffers (~1.6 KB each: the dip at a stream start is theirs).
CONSOLE_FREE_PLAYING_B = 22 * 1024
CONSOLE_LOWEST_B = 6600
CONSOLE_STACK_B, CONSOLE_STREAM_B, CONSOLE_RX_BUFFERS = 5120, 8192, 10
HEAP_HDR = 4  # TLSF block header on the ESP32 heap; allocations are 4-byte aligned
TCB_B = 352   # FreeRTOS TCB with newlib reentrancy (IDF 5.3, 1 TLS pointer + deletion callback)
STREAMBUF_B = 76  # StreamBuffer_t with its spinlock


def memory_budget():
    """Every byte the sound path allocates on the ESP32, from the binding's constants and the IDF 5.3 I2S
    driver's allocations, against the console's measured no-sound figures. Returns the model's numbers."""
    src = (ROOT / 'chromatic/launcher/main/jellyfin_esp32.c').read_text(encoding='utf-8')
    def const(name):
        return int(re.search(rf'#define {name} (\d+)u', src).group(1))
    stack, audio_stack, stream_b = const('PJ_JF_STACK'), const('PJ_JF_AUDIO_STACK'), const('PJ_JF_STREAM_BUFFER')
    ring, bufs, frames = const('PJ_JF_AUDIO_RING'), const('PJ_JF_AUDIO_DMA_BUFS'), const('PJ_JF_AUDIO_DMA_FRAMES')
    assert 'I2S_SLOT_MODE_STEREO' in src and 'I2S_DATA_BIT_WIDTH_16BIT' in src, 'the model assumes 16-bit stereo frames (4 B)'
    defaults = (ROOT / 'chromatic/launcher/sdkconfig.defaults').read_text(encoding='utf-8')
    rx = int(re.search(r'^CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=(\d+)', defaults, re.M).group(1))
    # i2s_common.c / i2s_std.c (IDF 5.3): i2s_controller_t, i2s_channel_obj_t, xQueueCreateWithCaps(desc_num-1, 4),
    # mutex, binary, calloc(i2s_std_config_t), two pointer arrays, per descriptor one lldesc_t (12 B) and one buffer
    # of frames * 4 B (heap_caps_aligned_calloc(4): a plain block), esp_intr_alloc_intrstatus (shared vector).
    i2s_b = (44 + 156 + (84 + 4 * (bufs - 1)) + 96 + 96 + 76 + 2 * (bufs * 4 + HEAP_HDR) + 84
             + bufs * (12 + HEAP_HDR) + bufs * (frames * 4 + HEAP_HDR))
    ring_b = ring + 1 + HEAP_HDR + STREAMBUF_B + HEAP_HDR
    task_b = audio_stack + HEAP_HDR + TCB_B + HEAP_HDR
    sound_b = i2s_b + ring_b + task_b
    saved_b = 2 * (CONSOLE_STACK_B - stack) + (CONSOLE_STREAM_B - stream_b)
    steady = CONSOLE_FREE_PLAYING_B + saved_b - sound_b
    dip = (CONSOLE_FREE_PLAYING_B - CONSOLE_LOWEST_B) * rx / CONSOLE_RX_BUFFERS
    return dict(i2s_b=i2s_b, ring_b=ring_b, task_b=task_b, sound_b=sound_b, saved_b=saved_b, rx=rx,
                steady_kb=steady / 1024, lowest_kb=(steady - dip) / 1024)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=OUT))
    try:
        exe = work / 'check_app.exe'
        subprocess.run([GCC, '-O1', '-Wall', '-Wextra', '-Werror', '-std=c11', '-Ichromatic/launcher/shared',
                        '-o', str(exe), 'chromatic/jellyfin/check_app.c', '-ljpeg'], cwd=ROOT, check=True)
        stream = PRIVATE / 'chromatic-samples' / 'sample.avi'
        if not stream.exists():
            stream = work / 'synthetic.avi'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=24000/1001',
                            '-f', 'lavfi', '-i', 'sine=f=440:r=48000', '-t', '180', '-c:v', 'mjpeg', '-q:v', '31',
                            '-pix_fmt', 'yuvj420p', '-c:a', 'libmp3lame', '-ac', '1', str(stream)], check=True)
        checks = []

        # 1. Home loads in Jellyfin's order, artwork arrives, every path is plain and token-free.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,expect:1,shot:home,state,press:DOWN,wait:800,shot:home-continue,'
                             'press:DOWN,press:DOWN,press:DOWN,wait:800,shot:home-latest', name='home')
        assert field(s[0], 'rows') == '5', s
        assert all(m in ('GET', 'POST') and p.startswith('/') and 'token' not in p.lower() and 'api_key' not in p.lower()
                   for m, p in server.requests), server.requests
        assert any('/Images/' in p for _, p in server.requests), 'artwork requested'
        checks.append('home: 5 rows (My Media, Continue Watching, Next Up, Latest x2), artwork, no token in paths')

        # 2. Resume from Continue Watching: stream starts at the resume point, keeps the title's
        #    cadence, reports Playing/Progress/Stopped with the position, seeks by reopening.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,expect:3,shot:details,'
                             'press:A,wait:12000,expect:4,state,shot:player,press:SELECT,wait:200,shot:player-osd,'
                             'press:RIGHT,wait:3000,state,press:A,wait:1000,state,shot:paused,press:A,wait:2000,'
                             'press:B,wait:500,expect:3,state', name='play')
        assert server.streams and 'startTimeTicks=3000000000' in server.streams[0], server.streams[:1]
        assert all(t in server.streams[0] for t in ('videoCodec=mjpeg', 'maxWidth=160', 'maxHeight=120', 'deviceId=',
                                                    'videoBitRate=1000000', 'maxFramerate=15', 'audioCodec=mp3')), server.streams[0]
        playing = s[0]
        shown, dropped = int(field(playing, 'shown')), int(field(playing, 'dropped'))
        assert field(playing, 'fps') == '15', ('the server caps the title to 15 pictures a second', playing)
        expected = 12 * 15
        assert expected * 0.9 <= shown + dropped <= expected * 1.1, (shown, dropped, expected)
        assert dropped <= expected * 0.02, ('drops on an unconstrained link', dropped)
        assert int(field(playing, 'pos')) in range(300 + 10, 300 + 14), playing
        assert len(server.streams) == 2 and 'startTimeTicks=3' in server.streams[1], server.streams
        seek_at = int(re.search(r'startTimeTicks=(\d+)', server.streams[1]).group(1)) // TICKS
        assert seek_at in range(320, 326), seek_at
        assert field(s[2], 'paused') == '1', s[2]
        names = [p for p, _ in server.bodies]
        assert names[0] == '/Sessions/Playing' and '/Sessions/Playing/Progress' in names and names[-1] == '/Sessions/Playing/Stopped', names
        stopped = server.bodies[-1][1]
        assert stopped['ItemId'] == EPISODES[3] and stopped['PositionTicks'] >= 320 * TICKS, stopped
        assert any(p.startswith('/Items?') and 'ids=' + EPISODES[3] in p for _, p in server.requests), 'lean details request'
        after = s[-1]
        assert int(field(after, 'swaps')) >= 1 and int(field(after, 'art')) > 0, ('artwork back after the video', after)
        checks.append(f'playback: resume at 5:00, {shown} pictures shown / {dropped} late in 12 s at the title cadence, '
                      'pause, seek +10 s, Playing/Progress/Stopped reported, artwork handed to the video and back')

        # 3. Browse: My Media > Shows > series > season > episode details, and back out.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:RIGHT,wait:300,press:A,wait:1000,expect:2,shot:folder-shows,press:A,'
                             'wait:1000,press:A,wait:1500,expect:2,shot:folder-episodes,press:DOWN,press:DOWN,press:DOWN,press:DOWN,'
                             'wait:1500,shot:folder-page2,press:A,wait:1500,expect:3,shot:episode-details,press:B,'
                             'wait:300,expect:2,press:B,wait:1000,press:B,wait:1000,press:B,wait:1000,expect:1', name='browse')
        assert any(f'parentId={SEASON}' in p and 'startIndex=8' in p for _, p in server.requests), 'paged to 9-15'
        checks.append('browse: library > series > season > 15 episodes over 2 pages of 8 > details, back to Home')

        # 4. A slow, dipping Wi-Fi link (12 KB/s, under the ~24 KB/s the 15 fps stream needs): pictures
        #    that arrive late re-anchor the clock and are shown (none skipped: skipping would not make
        #    the network faster), the quality steps down, playback never stops.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:15000,state',
                env={'RATE': '12000', 'GAP_EVERY': '5000', 'GAP_MS': '1500'}, name='slow')
        assert field(s[0], 'playing') == '1' and int(field(s[0], 'shown')) > 50, s
        assert int(field(s[0], 'rebased')) > 0 and int(field(s[0], 'dropped')) == 0, ('late pictures shown, not skipped', s)
        assert int(field(s[0], 'softened')) >= 1 and int(field(s[0], 'bitrate')) < 1000000, ('quality steps down', s)
        assert any('videoBitRate=500000' in p for p in server.streams[1:]), server.streams
        checks.append(f'slow Wi-Fi (12 KB/s, 1.5 s dips every 5 s): still playing, quality stepped down to '
                      f'{field(s[0], "bitrate")} bit/s, {field(s[0], "shown")} shown, {field(s[0], "dropped")} skipped')

        # 4b. The console's budget, as 1.2.1's diagnostics would report it: about 30 ms per picture in the
        #     ROM decoder, 4 ms to send the overlay frame, a 10 ms main loop. The 30 fps sample cannot be
        #     decoded in time (1.2.0 skipped a quarter, or over half at 60 ms, and reopened every 5 s), so
        #     the player asks for 15 pictures a second and shows them all.
        console = {'RATE': '400000', 'DECODE_MS': '30', 'FRAME_SEND_MS': '4', 'LOOP_MS': '10'}
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:20000,state', env=console, name='console')
        c = s[0]
        assert field(c, 'playing') == '1' and field(c, 'fps') == '15' and field(c, 'cap') == '15', c
        assert int(field(c, 'shown')) > 250 and int(field(c, 'dropped')) <= int(field(c, 'shown')) // 30, ('no skipping at 15 fps', c)
        assert field(c, 'slowed') == '0' and field(c, 'softened') == '0' and len(server.streams) == 1, ('no reopen', c, server.streams)
        assert 40 <= int(field(c, 'cycle')) <= 50, ('measured decode + send + loop', c)
        checks.append(f'console budget (30 ms decode, 4 ms frame send, 10 ms loop): 15 fps asked for and served, '
                      f'{field(c, "shown")} shown / {field(c, "dropped")} skipped in 20 s, measured cycle {field(c, "cycle")} ms')

        # 4c. A decoder twice as slow as hoped (90 ms): the player measures it, lowers the frame rate
        #     once (one reopen, "Adjusting frame rate") and then shows every picture.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:10000,state,wait:15000,state',
                env=dict(console, DECODE_MS='90'), name='slow-decoder')
        first, last = s[0], s[1]
        assert field(last, 'playing') == '1' and int(field(last, 'slowed')) >= 1 and int(field(last, 'cap')) <= 9, last
        assert len(server.streams) == 2 and re.search(r'maxFramerate=[6-9]&', server.streams[1]), server.streams
        assert field(last, 'fps') == field(last, 'cap'), ('served at the lower rate', last)
        shown_after = int(field(last, 'shown')) - int(field(first, 'shown'))
        dropped_after = int(field(last, 'dropped')) - int(field(first, 'dropped'))
        assert shown_after > 80 and dropped_after <= shown_after // 20, ('smooth at the lower rate', first, last)
        checks.append(f'slow decoder (90 ms): one reopen at {field(last, "cap")} fps, then {shown_after} shown / '
                      f'{dropped_after} skipped over 15 s')

        # 4d. The 1.2.0 console failure: a starved link (5 KB/s, the Wi-Fi stack out of memory) delivers
        #     pictures far behind the clock. The old pacing judged every one late and skipped it (1 shown,
        #     63 skipped in 30 s here; 35 shown / 485 skipped on the console); now each picture that
        #     arrives is shown, the position advances and playback never stops.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:30000,state', env={'RATE': '5000'}, name='starved')
        c = s[0]
        assert field(c, 'playing') == '1' and int(field(c, 'shown')) >= 40 and field(c, 'dropped') == '0', c
        assert int(field(c, 'rebased')) > 0 and int(field(c, 'frame')) > 0, c
        checks.append(f'starved link (5 KB/s): {field(c, "shown")} pictures shown, none skipped, still playing after 30 s')

        # 5. Replies that take a while (like Wi-Fi): Play pressed the instant Details opens, while
        #    its details and artwork are still out, must wait for the request slot and then play
        #    cleanly; browsing while artwork loads must not lose the press; B while Loading cancels.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:6000,press:DOWN,wait:100,press:A,press:A,wait:8000,state,press:B,wait:2000,'
                             'press:B,wait:1500,press:UP,press:RIGHT,press:A,wait:4000,state,press:A,wait:3000,state,'
                             'press:B,press:B,wait:3000,press:DOWN,press:A,wait:300,press:A,press:B,wait:3000,state',
                env={'DELAY_MS': '400'}, name='slow-replies')
        played, folder, cancelled = s[0], s[1], s[3]
        assert field(played, 'screen') == '4' and int(field(played, 'shown')) > 80 and int(field(played, 'dropped')) <= 5, played
        assert field(folder, 'screen') == '2' and int(field(folder, 'folder')) > 0, ('browse during artwork', folder)
        assert field(cancelled, 'playing') == '0' and field(cancelled, 'screen') == '3', ('B while Loading', cancelled)
        checks.append('slow replies (400 ms): Play during loading waits and plays cleanly, browsing during '
                      'artwork keeps the press, B while Loading cancels')

        # 6. The stream drops mid-play: reopen at the same position and keep going (no frozen picture).
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:6000,state,wait:6000,state',
                env={'FAIL_AT_MS': '7000'}, name='reconnect')
        assert len(server.streams) == 2, server.streams
        resumed = int(re.search(r'startTimeTicks=(\d+)', server.streams[1]).group(1)) // TICKS
        assert resumed in range(301, 310), resumed
        assert field(s[1], 'playing') == '1' and int(field(s[1], 'shown')) > 100, ('pictures after reconnect', s)
        checks.append(f'stream drop: reopened at {resumed // 60}:{resumed % 60:02d} and kept playing '
                      f'({field(s[1], "shown")} pictures since)')

        # 6b. The Wi-Fi link drops under the stream (launcher 1.2.9): the app says so at once in the strip, waits for the
        # shell's rejoin without spending its reconnect tries, and reopens with a fresh session as soon as the link is back;
        # a link gone for longer than JF_LINK_WAIT_MS stops the video with a plain message instead of waiting forever.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:5000,state,link:0,wait:3000,state,'
                             'shot:player-link-down,wait:20000,state,link:1,wait:4000,state', name='linkdown')
        assert len(server.streams) == 2, server.streams
        down, later, back = s[1], s[2], s[3]
        assert field(down, 'link_down') == '1' and field(down, 'playing') == '1' and down.split('msg=')[1] == 'Wi-Fi dropped: rejoining...', down
        assert field(later, 'reconnects') == '0' and field(later, 'screen') == '4' and field(later, 'shown') == field(down, 'shown'), later
        assert field(back, 'link_down') == '0' and int(field(back, 'shown')) > int(field(later, 'shown')) + 20, ('plays on after the link is back', back)
        ids = [re.search(r'playSessionId=([0-9a-f]+)', p).group(1) for p in server.streams]
        assert ids[0] != ids[1], 'a reopen gets a fresh playSessionId'
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:5000,link:0,wait:95000,expect:5,state', name='linklost')
        assert 'Wi-Fi was lost' in s[-1].split('msg=')[1] and len(server.streams) == 1, s[-1]
        checks.append('Wi-Fi link down under the stream: "Wi-Fi dropped: rejoining..." at once, no reconnect tries spent while the '
                      'shell rejoins, reopened (fresh playSessionId) the moment the link is back; 90 s without a link stops with a plain message')

        # 7. Sign-in expired and server unreachable: plain message, A tries again.
        server = FakeServer(work, stream)
        server.fail['/UserViews'] = 401
        s = run(exe, server, 'wait:2000,expect:5,shot:error-signin', name='signin')
        server = FakeServer(work, stream)
        server.fail['/UserViews'] = -1
        s = run(exe, server, 'wait:20000,expect:5,shot:error-offline,state', name='offline')
        assert sum(1 for _, p in server.requests if p.startswith('/UserViews')) == 6, 'five retries with back-off'
        assert field(s[-1], 'exit_to') == '0'
        # 1.3.0 "Find my server again": on the unreachable error Select leaves for Setup's Find screen (exit_to=2);
        # after "sign-in expired" Select opens Setup (exit_to=1); B still leaves for the launcher menu (exit_to=0).
        server = FakeServer(work, stream)
        server.fail['/UserViews'] = -1
        s = run(exe, server, 'wait:20000,expect:5,state,press:SELECT,wait:100,expect:6,state', name='offline-find')
        assert field(s[-1], 'exit_to') == '2', s[-1]
        server = FakeServer(work, stream)
        server.fail['/UserViews'] = -1
        s = run(exe, server, 'wait:20000,expect:5,press:B,wait:100,expect:6,state', name='offline-back')
        assert field(s[-1], 'exit_to') == '0', s[-1]
        server = FakeServer(work, stream)
        server.fail['/UserViews'] = 401
        s = run(exe, server, 'wait:2000,expect:5,press:SELECT,wait:100,expect:6,state', name='signin-setup')
        assert field(s[-1], 'exit_to') == '1', s[-1]
        server = FakeServer(work, stream)
        server.fail['/UserViews'] = 500  # another failure: Select does nothing
        s = run(exe, server, 'wait:2000,expect:5,press:SELECT,wait:100,expect:5,state', name='refused-select')
        ui_text = (ROOT / 'chromatic/jellyfin/jf_ui.h').read_text(encoding='utf-8')
        assert '"A: retry  Select: find  B: back"' in ui_text and '"A: retry  Select: sign in  B: back"' in ui_text
        checks.append('errors: sign-in expired and unreachable server shown plainly; 5 retries with back-off; 1.3.0: Select on '
                      '"Can\'t reach" opens Find my server again (exit_to=2), on "sign-in expired" opens Setup (exit_to=1), '
                      'on other errors does nothing; B leaves for the menu')

        # 8. A details request fails (Wi-Fi blip) and Play is pressed before its 1 s retry: the
        #    stale retry must never fire into playback (it would flip to Details and write a
        #    reply into the buffer the video is using).
        server = FakeServer(work, stream)
        server.fail_once['/Items?userId=' + USER + '&ids=' + EPISODES[3]] = -1
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:100,press:A,wait:5000,state',
                name='retry-vs-play')
        assert field(s[0], 'screen') == '4' and field(s[0], 'playing') == '1' and int(field(s[0], 'shown')) > 60, s
        started = next(i for i, (_, p) in enumerate(server.requests) if p.startswith('/Sessions/Playing'))
        assert not any('ids=' + EPISODES[3] in p for _, p in server.requests[started:]), server.requests
        checks.append('offline retry vs Play: Play pressed during a pending details retry plays cleanly; '
                      'the retry never fires into playback')

        # 9. The Jellyfin menu (Select): launcher look, six rows that scroll (Home, Cards, Audio, Playback
        #    info, Credits, Chromatic menu; the Subtitles row is hidden since 1.2.7: burned-in subtitles fail
        #    on the owner's server). Cards/Audio change in place (RAM only); Credits and Playback info are
        #    pages; bigger cards fetch their own image sizes.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,expect:1,press:SELECT,wait:200,expect:7,shot:menu,state,'
                         'press:DOWN,wait:50,shot:menu-cards,press:A,wait:50,shot:menu-cards-bigger,state,'
                         'press:DOWN,wait:50,press:A,press:A,wait:50,shot:menu-audio,state,'
                         'press:DOWN,wait:50,press:A,wait:50,expect:7,shot:menu-info,state,press:B,wait:50,'
                         'press:DOWN,press:DOWN,wait:50,press:A,wait:50,expect:7,shot:menu-credits,state,press:B,wait:50,'
                         'press:UP,press:UP,press:UP,press:UP,press:UP,wait:50,press:A,wait:1500,expect:1,shot:home-big,state,'
                         'press:RIGHT,wait:300,press:A,wait:1500,expect:2,shot:folder-big-posters,press:A,wait:1000,'
                         'press:A,wait:1500,expect:2,shot:folder-big-episodes,'
                         'press:SELECT,press:UP,wait:100,expect:7,state,shot:menu-leave,press:A,wait:200,expect:6,state', name='menu')
        ui = run.ui
        assert field(ui[0], 'screen') == '7' and field(ui[0], 'menu_sel') == '0', ui[0]
        cards = next(u for u in ui if field(u, 'menu_sel') == '1')
        assert field(cards, 'big') == '1', ('A on the Cards row: bigger cards', cards)
        audio = next(u for u in ui if field(u, 'menu_sel') == '2')
        assert field(audio, 'audio_lang') == '2', ('A twice on Audio: English -> Original -> Japanese', audio)
        info = next(u for u in ui if field(u, 'menu_sel') == '3')
        assert field(info, 'menu_page') == '1', ('Playback info page', info)
        credits = next(u for u in ui if field(u, 'menu_sel') == '6')  # 1.3.0: the hidden Pocket link row is 5
        assert field(credits, 'menu_page') == '2', ('Credits page', credits)
        home = next(u for u in ui if field(u, 'screen') == '1')
        assert field(home, 'big') == '1', ('bigger cards on Home', home)
        leave = next(u for u in ui if field(u, 'menu_sel') == '7')
        assert field(leave, 'screen') == '7', leave
        assert field(s[-1], 'exit_to') == '0', ('Chromatic menu leaves for the launcher menu', s[-1])
        assert all(field(u, 'subs') == '0' for u in ui), ('no Subtitles row: subs stays Off through the whole menu', ui)
        menu_src = (ROOT / 'chromatic/jellyfin/jf_menu.h').read_text(encoding='utf-8')
        app_src = (ROOT / 'chromatic/jellyfin/jf_app.h').read_text(encoding='utf-8')
        assert '#define JF_SUBS_ROW 0\n' in app_src and 'if(row==JF_M_SUBS)return JF_SUBS_ROW;' in menu_src, \
            'Subtitles row hidden in the console build (JF_SUBS_ROW 0) until a console run proves burn-in'
        ui_src = (ROOT / 'chromatic/jellyfin/jf_ui.h').read_text(encoding='utf-8')
        assert 'Created by ChaoticSPARTAN1' in ui_src and 'jf_home_credit(a,fb)' in ui_src, 'Home credit line drawn'
        shot_home = (OUT / 'home' / 'home.png')
        assert shot_home.exists(), 'Home preview (with the credit line) written'

        
        images = [p for _, p in server.requests if '/Images/' in p]
        assert any('fillWidth=72&fillHeight=40' in p for p in images), 'bigger thumbs fetched at 72x40'
        assert any('fillWidth=42&fillHeight=63' in p for p in images), 'bigger posters fetched at 42x63'
        assert not any(m == 'POST' and ('Settings' in p or 'DisplayPreferences' in p) for m, p in server.requests), \
            'no setting is written anywhere (RAM only)'
        checks.append('Jellyfin menu: six scrolling rows (Home, Cards, Audio language, Playback info, '
                      'Credits, Chromatic menu); Cards/Audio/Subtitles change in place (RAM only, 72x40/42x63 artwork), '
                      'Credits and info pages, exit to the Chromatic menu')

        # The menu's "Wi-Fi & sign-in" row (1.2.9): fifth row, leaves the app with exit_to=1 so the launcher opens Setup
        # (after closing the app and freeing its memory) instead of returning to its start-up menu.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,expect:1,press:SELECT,wait:200,expect:7,press:DOWN,press:DOWN,press:DOWN,press:DOWN,wait:50,'
                             'shot:menu-setup,state,press:A,wait:100,expect:6,state', name='menu-setup')
        assert 'menu_sel=4' in run.ui[0] and field(s[-1], 'exit_to') == '1', (run.ui[0], s[-1])
        ui_text = (ROOT / 'chromatic/jellyfin/jf_ui.h').read_text(encoding='utf-8')
        assert '"Wi-Fi & sign-in"' in ui_text and 'JF_M_SETUP' in ui_text
        checks.append('Jellyfin menu: "Wi-Fi & sign-in" row leaves the app for Setup (exit_to=1); "Chromatic menu" still leaves for the launcher menu (exit_to=0)')
        # 1.3.0: the "Pocket link" row exists only on the link FPGA (POCKET_LINK: jf_config.pocket_link). Without it five
        # Downs land on Credits exactly as in 1.2.9 (the row is skipped); with it the sixth row leaves with exit_to=3.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,expect:1,press:SELECT,wait:200,expect:7,press:DOWN,press:DOWN,press:DOWN,press:DOWN,'
                             'press:DOWN,wait:50,state,press:UP,wait:50,state', name='menu-no-plink')
        assert field(run.ui[0], 'menu_sel') == '6' and field(run.ui[1], 'menu_sel') == '4', run.ui
        # Launcher 1.5.0 (owner's request): with the link the "Pocket link" row is the FIRST row, one Up from Home; every
        # other row keeps its order. Launcher 1.5.1 (owner's request): the menu OPENS on Pocket link when it is shown,
        # every time (Home again after closing and reopening it); without the row it opens on Home (menu-no-plink above).
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,expect:1,shot:home-tabs,press:SELECT,wait:200,expect:7,wait:50,shot:menu-plink-top,state,'
                             'press:DOWN,wait:50,shot:menu-plink-home,state,press:B,wait:100,expect:1,shot:home-tabs-closed,'
                             'press:SELECT,wait:200,expect:7,state,press:DOWN,press:UP,wait:50,shot:menu-plink,state,'
                             'press:A,wait:100,expect:6,state', env={'POCKET_LINK': '1'},
                name='menu-plink')
        assert field(run.ui[0], 'menu_sel') == '5' and field(run.ui[1], 'menu_sel') == '0', ('opens on Pocket link, Down: Home', run.ui)
        assert field(run.ui[2], 'menu_sel') == '5', ('reopened after Home was focused: Pocket link again', run.ui)
        assert field(run.ui[3], 'menu_sel') == '5' and field(s[-1], 'exit_to') == '3', (run.ui, s[-1])
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,expect:1,press:SELECT,wait:200,expect:7,press:DOWN,press:DOWN,press:DOWN,press:DOWN,'
                             'press:DOWN,press:DOWN,wait:50,state,press:DOWN,press:DOWN,wait:50,state', env={'POCKET_LINK': '1'},
                name='menu-plink-order')
        assert field(run.ui[0], 'menu_sel') == '6' and field(run.ui[1], 'menu_sel') == '5', ('Down past the last row wraps to Pocket link', run.ui)
        menu_h = (ROOT / 'chromatic/jellyfin/jf_menu.h').read_text(encoding='utf-8')
        assert '"Pocket link"' in ui_text and 'jf_menu_shown' in menu_h
        assert 'jf_menu_order[JF_M_COUNT]={JF_M_PLINK,JF_M_HOME,JF_M_LAYOUT,JF_M_AUDIO,JF_M_SUBS,JF_M_INFO,JF_M_SETUP,' in menu_h, \
            'Pocket link is the first row of the menu (1.5.0)'
        assert 'a->menu_sel=a->cfg.pocket_link?JF_M_PLINK:JF_M_HOME;' in menu_h, 'the menu opens on its first row (1.5.1)'
        checks.append('Jellyfin menu (1.3.0, first row since 1.5.0, opens on it since 1.5.1): "Pocket link" row only on the link FPGA, '
                      'first in the list and focused at every opening (Down: Home; Down from the last row wraps to it), leaves with '
                      'exit_to=3; on stock and the 18.40 sound image the row is skipped, the menu is 1.2.9\'s and opens on Home')

        # 10. A name that does not fit scrolls (focused card only, pauses at both ends). LIVE_DRAW draws
        #     like the console loop and checks every label-only redraw against a full redraw.
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:RIGHT,wait:300,press:A,wait:1000,press:A,wait:1000,shot:season,state,'
                             'press:A,wait:1200,state,wait:2000,state,shot:scroll-mid,wait:1500,state,wait:3500,state',
                env={'LIVE_DRAW': '1'}, name='scroll')
        ui = run.ui
        season, hold, early, later, wrapped = ui[:5]
        assert season.endswith('label=Season 1') and int(field(season, 'over')) > 0, ('focused season: full name', season)
        assert field(hold, 'scroll') == '0' and int(field(hold, 'over')) > 0, ('pause at the start', hold)
        assert 0 < int(field(early, 'scroll')) < int(field(later, 'scroll')), (early, later)
        assert int(field(wrapped, 'scroll')) < int(field(later, 'scroll')), ('back to the start after the end pause', wrapped)
        assert all(field(u, 'label_decodes') == '0' and field(u, 'label_mismatch') == '0' for u in ui), ui
        times = [int(re.search(r' t=(\d+)', x).group(1)) for x in s]  # simulated ms
        labels = int(field(later, 'draws_label')) - int(field(early, 'draws_label'))
        fulls = int(field(later, 'draws_full')) - int(field(early, 'draws_full'))
        seconds = (times[3] - times[2]) / 1000
        assert labels <= seconds * 1000 / 80 + 1 and fulls == 0, ('scroll redraw budget', labels, fulls, seconds)
        checks.append(f'scrolling names: focused card only, {labels} label-only redraws in {seconds:.1f} s '
                      f'(<= 12.5/s), no full redraw and no artwork decoded while scrolling, identical to a full redraw')
        # 11-15. Sound (docs/Design-notes.md): with a platform that plays audio (the FPGA with
        #     media audio) the harness models the ESP32 binding: a 1,536-byte ADPCM ring, a consumer that
        #     decodes into an I2S DMA at 16 kHz in simulated time, silence on underrun. The stream is the
        #     real server sample with IMA ADPCM when present, else a synthetic one.
        sound = PRIVATE / 'chromatic-samples' / 'sample-adpcm.avi'
        if not sound.exists():
            sound = work / 'synthetic-adpcm.avi'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=15',
                            '-f', 'lavfi', '-i', 'sine=f=440:r=48000', '-t', '180', '-c:v', 'mjpeg', '-q:v', '31',
                            '-pix_fmt', 'yuvj420p', '-c:a', 'adpcm_ima_wav', '-ar', '16000', '-ac', '1', str(sound)], check=True)
        ref = work / 'sound-ref.raw'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(sound), '-vn', '-f', 's16le', '-c:a', 'pcm_s16le', str(ref)], check=True)
        ref_bytes = ref.read_bytes()
        audio = dict(console, AUDIO='1')
        play = 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,state,'  # a state at the play start (its t)

        def sound_lines():
            return [l for l in run.err.splitlines() if l.startswith('SOUND ')]

        def sim_seconds(states):  # simulated time between the first and last state (decoding adds time; see 4b)
            return (int(field(states[-1], ' t')) - int(field(states[0], ' t'))) / 1000

        # 6b with sound (1.2.9): the sink was declared dead during the outage and never revived, so the
        # video came back silent. Now the dead test is held while the link is down and a reopen revives the sink.
        server = FakeServer(work, sound)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:8000,state,link:0,wait:3000,state,'
                             'wait:4000,state,link:1,wait:8000,state', env=dict(audio, RATE='400000'), name='linkdown-sound')
        sounds = sound_lines()
        before, down, after = sounds[0], sounds[2], sounds[3]
        assert field(before, 'on') == '1' and field(before, 'dead') == '0', before
        assert field(down, 'dead') == '0', ('no dead sink while the link is down', down)
        assert field(after, 'on') == '1' and field(after, 'dead') == '0' and int(field(after, 'blocks')) > int(field(down, 'blocks')) + 10, ('sound back after the link', after)
        assert int(field(s[3], 'shown')) > int(field(s[2], 'shown')) + 20, s[3]
        checks.append(f'link down with sound: sink not declared dead during the outage, sound blocks {field(down, "blocks")} -> '
                      f'{field(after, "blocks")} after the link is back')

        # 11. Sound plays and is the clock: the request asks for IMA ADPCM 16 kHz mono, every block reaches the
        #     ring, the DMA plays as many seconds as pass, no underrun after the start, the pictures follow the
        #     sound at the title's cadence, and the PCM played is FFmpeg's decode byte for byte.
        server = FakeServer(work, sound)
        pcm = work / 'played.raw'
        s = run(exe, server, play + 'wait:20000,state', env=dict(audio, AUDIO_OUT=str(pcm)), name='sound')
        c, snd, secs = s[-1], sound_lines()[-1], sim_seconds(s)
        assert all(t in server.streams[0] for t in ('audioCodec=adpcm_ima_wav', 'audioSampleRate=16000', 'audioChannels=1')) \
            and 'audioBitRate' not in server.streams[0], server.streams[0]
        assert field(snd, 'on') == '1' and field(snd, 'clock') == '1' and field(snd, 'dead') == '0', snd
        assert field(snd, 'fmt') == '17' and field(snd, 'ch') == '1' and field(snd, 'rate') == '16000', snd
        played, shown = int(field(snd, 'played')), int(field(c, 'shown'))
        assert secs - 1.5 <= played / 16000 <= secs, ('the sound plays through the whole run', played, secs, snd)
        assert abs(played / 16000 - shown / 15) <= 0.6, ('pictures follow the sound at 15 fps', played, shown)
        assert int(field(snd, 'underruns')) == 0 and int(field(snd, 'silence')) < 16000 // 2, ('no underrun on a good link', snd)
        assert int(field(snd, 'blocks')) >= secs / 0.128 - 2 and int(field(snd, 'fallbacks')) == 0, (snd, secs)
        assert field(c, 'playing') == '1' and int(field(c, 'dropped')) <= shown // 50, c
        assert len(server.streams) == 1, ('no reopen', server.streams)
        got = pcm.read_bytes()
        assert len(got) == played * 2 and got == ref_bytes[:len(got)], ('played PCM is FFmpeg\'s decode', len(got), len(ref_bytes))
        base_rate = shown / secs
        checks.append(f'sound: IMA ADPCM 16 kHz asked for, {field(snd, "blocks")} blocks through a 1.5 KB ring, {played} samples '
                      f'({played / 16000:.1f} s) played in {secs:.1f} s with 0 underruns, {shown} pictures shown / {field(c, "dropped")} '
                      f'skipped on the sound clock, played PCM byte-identical to FFmpeg')

        # 12. The sound runs 3% fast against the video's clock (crystal drift, exaggerated): pictures follow the sound.
        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:20000,state', env=dict(audio, AUDIO_DRIFT_PPM='30000'), name='sound-drift')
        c, snd = s[-1], sound_lines()[-1]
        ratio = int(field(c, 'shown')) / sim_seconds(s) / base_rate
        assert field(snd, 'clock') == '1' and 1.02 <= ratio <= 1.045, ('pictures paced by the sound', ratio, c, snd)
        assert int(field(c, 'dropped')) <= int(field(c, 'shown')) // 50 and int(field(snd, 'underruns')) == 0, (c, snd)
        checks.append(f'sound drift +3%: picture rate {ratio:.3f}x the undrifted run, none skipped, no underrun')

        # 13. The sink stops playing (a dead I2S path) 20 s of simulated time in (between the two states: the
        #     simulated clock runs about 1.5x the waits, see 4b): the video must go on alone on the frame clock.
        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:7000,state,wait:13000,state', env=dict(audio, AUDIO_STALL_AT_MS='20000'), name='sound-dead')
        before, after = s[1], s[2]
        snd_b, snd_a = sound_lines()[1:3]  # one SOUND line per state (the harness adds a final state)
        assert field(snd_b, 'clock') == '1' and field(snd_b, 'dead') == '0', snd_b
        assert field(snd_a, 'dead') == '1' and field(snd_a, 'clock') == '0' and int(field(snd_a, 'fallbacks')) >= 1, snd_a
        assert field(after, 'playing') == '1' and int(field(after, 'shown')) - int(field(before, 'shown')) >= 150, (before, after)
        # The position keeps counting when the sound clock falls back (it used to jump back to the
        # start of the segment, which would save a wrong resume point).
        assert int(field(after, 'pos')) >= int(field(before, 'pos')) + 10, ('position went back', before, after)
        checks.append(f'dead sink mid-play: sound dropped, video went on ({int(field(after, "shown")) - int(field(before, "shown"))} '
                      f'pictures in the next 13 s of waits)')

        # 14. Pause holds the sound (no samples, no underrun counted), resume continues, a seek flushes the ring
        #     and restarts the sound clock with the new segment.
        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:5000,state,press:A,wait:2000,state,press:A,wait:2000,state,press:RIGHT,wait:3000,state',
                env=audio, name='sound-pause')
        p0, p1, p2, p3 = [int(field(l, 'played')) for l in sound_lines()[1:5]]
        u = [int(field(l, 'underruns')) for l in sound_lines()[1:5]]
        assert p1 - p0 <= 16000 // 8 and u[1] == u[0], ('paused: sound held', p0, p1, u)
        assert p2 - p1 >= 16000 * 1.7, ('resumed', p1, p2)
        assert p3 < p2 and p3 < 16000 * 8 and len(server.streams) == 2 and field(s[-1], 'playing') == '1', ('seek restarts the clock', p2, p3, server.streams)
        checks.append('sound pause/resume/seek: ring held while paused (no underrun counted), continues on resume, flushed on seek')

        # 15. A slow, dipping link with sound (12 KB/s for a 34 KB/s stream): the sound starves (underruns) and
        #     comes back, the clock falls back to the pictures while it is stale and returns, nothing is skipped and
        #     playback never stops. Since 1.2.7 the starving sound also steps the quality down (the fake server keeps
        #     serving the same bytes here, so it steps again: every step is a reopen, and `played` restarts with the
        #     segment); the sink is never declared dead.
        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:20000,state', env=dict(audio, RATE='12000', GAP_EVERY='5000', GAP_MS='1500'), name='sound-slow')
        c, snd = s[-1], sound_lines()[-1]
        assert field(c, 'playing') == '1' and int(field(c, 'shown')) > 50 and int(field(c, 'dropped')) == 0, c
        assert int(field(snd, 'underruns')) >= 1 and field(snd, 'dead') == '0' and int(field(snd, 'played')) > 0, snd
        assert int(field(snd, 'starved')) >= 1 and int(field(c, 'bitrate')) < 1000000 and len(server.streams) >= 2, ('the starving sound lowers the quality', snd, c)
        checks.append(f'slow Wi-Fi with sound: {field(snd, "underruns")} underruns (silence, then sound resumes), '
                      f'{field(snd, "fallbacks")} clock fallbacks, {field(c, "shown")} shown / 0 skipped, quality stepped down '
                      f'{field(snd, "starved")}x for the starving sound, still playing')

        # 16. The main loop is held 600 ms every 4 s (a Wi-Fi task hogging the CPU, as when the heap ran out on
        #     the console's second sound play: fps stepped down 6 -> 3 reopens, "looped the intro"). One stalled
        #     loop must not read as a slow decoder: no frame-rate step-down, no reopen, playback goes on.
        stall = dict(console, STALL_EVERY='4000', STALL_MS='600')
        server = FakeServer(work, stream)
        s = run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:A,wait:25000,state', env=stall, name='loop-stall')
        c = s[0]
        assert int(field(c, 'stalls')) >= 4 and field(c, 'playing') == '1', c
        assert field(c, 'slowed') == '0' and field(c, 'cap') == '15' and len(server.streams) == 1, ('a stalled loop is not a slow decoder', c, server.streams)
        assert int(field(c, 'cycle')) <= 60 and int(field(c, 'dropped')) <= int(field(c, 'shown')) // 30, c
        checks.append(f'loop stalls (600 ms every 4 s, {field(c, "stalls")} of them): no frame-rate step-down, no reopen, '
                      f'cycle still {field(c, "cycle")} ms, {field(c, "shown")} shown / {field(c, "dropped")} skipped')

        # 17. The same stalls with sound: the DMA plays the ring out (an underrun), the pictures behind come in a
        #     burst and are late against the sound clock, which never jumps; they are skipped to catch up but
        #     as the network's (rebased), never as the decoder's: no step-down, no reopen, no "dropped".
        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:25000,state', env=dict(audio, STALL_EVERY='4000', STALL_MS='600'), name='sound-loop-stall')
        c, snd = s[-1], sound_lines()[-1]
        assert int(field(c, 'stalls')) >= 4 and field(c, 'playing') == '1' and field(snd, 'on') == '1' and field(snd, 'dead') == '0', (c, snd)
        assert field(c, 'slowed') == '0' and field(c, 'cap') == '15' and len(server.streams) == 1, ('sound-clock catch-up is not a slow decoder', c, server.streams)
        assert field(c, 'dropped') == '0' and int(field(c, 'rebased')) >= 1, ('skips to catch up with the sound count as the network\'s', c)
        assert int(field(snd, 'underruns')) >= 1 and int(field(snd, 'played')) > 16000 * 15, snd
        checks.append(f'loop stalls with sound: {field(snd, "underruns")} underruns, {field(c, "rebased")} pictures skipped to catch '
                      f'up with the sound (none counted as dropped), no step-down, no reopen')

        # 18. The sink cannot start at the first block (its memory not back yet from the last play, or the task
        #     still leaving, as on the console's second play): it is asked again at every block, starts within
        #     JF_AUDIO_TRY_MS and becomes the clock from that block's media time, so the pictures never wait for
        #     a clock that starts at 0 (the old code tried once per play, then played silent).
        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:20000,state', env=dict(audio, AUDIO_REFUSE_UNTIL_MS='8000'), name='sound-late-start')
        c, snd, secs = s[-1], sound_lines()[-1], sim_seconds(s)
        assert field(snd, 'on') == '1' and field(snd, 'clock') == '1' and field(snd, 'dead') == '0', snd
        assert int(field(snd, 'retries')) >= 10 and 1500 <= int(field(snd, 'base')) <= 4500, ('retried, clock based at the start block', snd)
        played, shown = int(field(snd, 'played')), int(field(c, 'shown'))
        assert secs - 5 <= played / 16000 <= secs - 2, ('sound from the late start on', played, secs)
        assert abs(shown / 15 - secs) <= 1.2 and field(c, 'dropped') == '0' and len(server.streams) == 1, ('pictures never waited for the late clock', shown, secs, c)
        checks.append(f'late sink start: refused {field(snd, "retries")} times, started {int(field(snd, "base")) / 1000:.1f} s in, '
                      f'{played / 16000:.1f} s played in a {secs:.1f} s run, {shown} pictures shown (no wait, no skip)')

        # 19. Memory budget of the ESP32 binding with sound (chromatic/launcher/main/jellyfin_esp32.c), against
        #     the console's own figures from launcher 1.2.4 on the stock FPGA (22 KB free while playing, lowest
        #     6.6 KB: the dip is Wi-Fi's dynamic RX buffers at a stream start) and ESP-IDF 5.3's I2S driver
        #     (i2s_common.c / i2s_std.c: controller, channel, 2-deep message queue, mutex, binary semaphore,
        #     mode info, descriptor arrays, one lldesc_t and one buffer per DMA descriptor, shared interrupt).
        budget = memory_budget()
        assert budget['steady_kb'] >= 12 and budget['lowest_kb'] >= 6, budget
        checks.append('sound memory budget: sound costs {sound_b} B (I2S {i2s_b}, ring {ring_b}, task {task_b}); stacks and the '
                      'stream buffer give back {saved_b} B; modelled free while playing with sound {steady_kb:.1f} KB '
                      '(>= 12), lowest {lowest_kb:.1f} KB (>= 6) with {rx} Wi-Fi RX buffers'.format(**budget))

        def sessions_in(streams):
            return [re.search(r'playSessionId=([0-9a-f]+)', p).group(1) for p in streams]

        # 20. English audio: a lean MediaStreams request is made and the stream asks for the English audio
        #     track (audioStreamIndex), not the file's own default (Japanese in the fixture). The reply is
        #     small (only Type/Index/Language/IsDefault parsed). Subtitles are off by default.
        server = FakeServer(work, sound)
        run(exe, server, play + 'wait:6000,state', env=audio, name='english-audio')
        ms = [p for _, p in server.requests if p.startswith('/Items?') and 'ids=' in p and 'fields=MediaStreams' in p]
        assert ms and 'enableUserData=false' in ms[0], ('one lean MediaStreams request', ms)
        assert 'audioStreamIndex=2' in server.streams[0], ('English audio track chosen, not the default', server.streams[0])
        assert 'subtitleStreamIndex' not in server.streams[0], ('subtitles off by default', server.streams[0])
        assert field(run.ui[-1], 'aidx') == '2' and field(run.ui[-1], 'sidx') == '-1', run.ui[-1]
        checks.append('English audio: lean MediaStreams request, English audio track (audioStreamIndex) chosen over the '
                      'default, subtitles off by default')

        # 21. Subtitles are hidden (1.2.7): the row that used to be third is Playback info now, pressing A on
        #     it and backing out changes nothing, and the stream never asks the server to burn subtitles in
        #     (SubtitleMethod=Encode fails on the owner's server). The request code is kept for later.
        server = FakeServer(work, sound)
        run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1200,expect:3,'
                         'press:SELECT,wait:100,press:DOWN,press:DOWN,press:DOWN,press:A,wait:100,press:B,wait:100,press:B,wait:100,'
                         'press:A,wait:6000,state', env=audio, name='subs-hidden')
        assert 'subtitleStreamIndex' not in server.streams[0] and 'subtitleMethod' not in server.streams[0], server.streams[0]
        assert 'audioStreamIndex=2' in server.streams[0], ('English audio still chosen', server.streams[0])
        assert field(run.ui[-1], 'subs') == '0' and field(run.ui[-1], 'sidx') == '-1', run.ui[-1]
        checks.append('subtitles hidden: no menu row, no subtitleStreamIndex/subtitleMethod in the stream request')

        # 22. Audio language "Original" -> no audioStreamIndex (the server's own default track).
        server = FakeServer(work, sound)
        run(exe, server, 'wait:3000,press:DOWN,wait:300,press:A,wait:1200,expect:3,press:SELECT,wait:100,'
                         'press:DOWN,press:DOWN,press:A,press:B,wait:100,press:A,wait:4000,state', env=audio, name='audio-original')
        assert server.streams and 'audioStreamIndex' not in server.streams[0], ('Original: the server default track', server.streams[0])
        checks.append('audio language Original: no audioStreamIndex (the server default track)')

        # 22b (1.3.2). Mid-play audio switch. Menu over a playing video pauses it with the Audio row focused; changing the
        #     row and closing the menu reopens the stream at the same position with the new audioStreamIndex and a fresh
        #     playSessionId (as seek does), from the kept track list (no second MediaStreams request). The diagnostics
        #     line names the index, the 3-letter code and the count only.
        tracks_re = re.compile(r'^jellyfin tracks audio=-?\d+ lang=[a-z]{3} of=\d+ subs=-?\d+$')

        def tracks_lines():
            return [l[len('TRACKS '):] for l in run.err.splitlines() if l.startswith('TRACKS ')]

        def notes():
            return [l for l in run.err.splitlines() if l.startswith('NOTE ')]

        def start_s(path):
            return int(re.search(r'startTimeTicks=(\d+)', path).group(1)) // 10_000_000

        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:6000,state,press:MENU,wait:300,state,shot:menu-over-video,press:RIGHT,press:B,wait:1500,state,shot:audio-original-note,'
                                    'wait:3000,press:MENU,wait:200,press:RIGHT,press:B,wait:1500,state,wait:3000,state',
                env=audio, name='audio-switch')
        ui, tl, nt = run.ui, tracks_lines(), notes()
        assert len(server.streams) == 3, ('one stream, then one reopen per switch', server.streams)
        ms = [p for _, p in server.requests if 'fields=MediaStreams' in p]
        assert len(ms) == 1, ('the track list is kept: no refetch for a switch', ms)
        assert 'audioStreamIndex=2' in server.streams[0], server.streams[0]
        assert 'audioStreamIndex' not in server.streams[1], ('Original: the server default track', server.streams[1])
        assert 'audioStreamIndex=1' in server.streams[2], ('Japanese', server.streams[2])
        sessions = sessions_in(server.streams)
        assert len(set(sessions)) == 3, ('a fresh playSessionId for every reopen', sessions)
        at_switch = int(field(s[1], 'pos'))
        assert at_switch >= 5 and abs(start_s(server.streams[1]) - at_switch) <= 1, ('reopened where it was', at_switch, server.streams[1])
        assert start_s(server.streams[2]) >= start_s(server.streams[1]) + 3, ('the second switch later on', server.streams[2])
        assert field(ui[2], 'screen') == '7' and field(ui[2], 'menu_sel') == '2' and field(s[2], 'paused') == '1', \
            ('Menu over the video: paused, Audio row focused', ui[2], s[2])
        assert field(s[3], 'playing') == '1' and field(s[3], 'paused') == '0' and field(s[5], 'playing') == '1', s
        assert int(field(s[3], 'shown')) > int(field(s[1], 'shown')), ('pictures again after the switch', s[3])
        assert all(tracks_re.match(l) for l in tl), tl
        assert tl[1] == 'jellyfin tracks audio=2 lang=eng of=2 subs=-1', tl[1]
        assert tl[3] == 'jellyfin tracks audio=-1 lang=jpn of=2 subs=-1', ('Original: the default track, Japanese', tl[3])
        assert tl[5] == 'jellyfin tracks audio=1 lang=jpn of=2 subs=-1', tl[5]
        assert 'warn=0 on=1 text=Audio: Original' in nt[3], ('the result, plain', nt[3])
        assert 'on=0' in nt[-1], ('the note goes after a few seconds', nt[-1])
        checks.append(f'mid-play audio switch (1.3.2): Menu pauses with the Audio row focused; English > Original > Japanese '
                      f'reopened at {at_switch} s and later with the new audioStreamIndex and 3 distinct playSessionIds, one '
                      f'MediaStreams request; diag line "{tl[1]}"')
        # 1.5.1: with the Pocket link row shown (the 18.41 link image), the menu over a video still opens on Audio.
        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:4000,press:MENU,wait:300,state,shot:menu-over-video-plink,press:B,wait:1000,state',
                env=dict(audio, POCKET_LINK='1'), name='menu-plink')
        assert field(run.ui[1], 'screen') == '7' and field(run.ui[1], 'menu_sel') == '2' and field(s[1], 'paused') == '1', \
            ('Menu over the video with the link row: paused, Audio row focused', run.ui[1], s[1])
        assert field(s[2], 'playing') == '1' and field(s[2], 'paused') == '0', ('B: the video plays on', s[2])
        checks.append('Jellyfin menu over a video with the Pocket link row (1.5.1): opens on Audio, B plays on')

        # 22c (1.3.2). No English track: the original plays and a yellow note says so, at the play start and after a
        #     mid-play switch back to English.
        server = FakeServer(work, sound)
        server.media_streams = [{'Type': 'Video', 'Index': 0}, {'Type': 'Audio', 'Index': 1, 'Language': 'ja', 'IsDefault': True}]
        s = run(exe, server, play + 'wait:800,state,shot:no-english-note,press:MENU,press:RIGHT,press:B,wait:800,state,'
                                    'press:MENU,press:LEFT,press:B,wait:800,state', env=audio, name='audio-no-english')
        tl, nt = tracks_lines(), notes()
        assert all('audioStreamIndex' not in p for p in server.streams) and len(server.streams) == 3, server.streams
        assert nt[1].startswith('NOTE warn=1 on=1 text=No English audio in this video: playing the original'), nt[1]
        assert nt[2].startswith('NOTE warn=0 on=1 text=Audio: Original'), nt[2]
        assert nt[3].startswith('NOTE warn=1 on=1 text=No English audio in this video: playing the original'), nt[3]
        assert tl[1] == 'jellyfin tracks audio=-1 lang=jpn of=1 subs=-1', ('a 2-letter tag reported as its 3-letter code', tl[1])
        assert field(s[-1], 'playing') == '1', s[-1]
        checks.append('no English audio: the original plays, yellow "No English audio in this video: playing the original" '
                      'at the start and again after switching back to English mid-play')

        # 22e (1.3.2). The default track is picked exactly as 1.2.6-1.3.1 did (the owner's server plays English with it):
        #     in list order the first English track, replaced by a later English one marked default; the tag is matched
        #     by its first three letters ("English" counts) or as a 2-letter code ("en", "en-US").
        for streams, want in (([{'Type': 'Audio', 'Index': 1, 'Language': 'jpn', 'IsDefault': True},
                                {'Type': 'Audio', 'Index': 2, 'Language': 'English'},
                                {'Type': 'Audio', 'Index': 4, 'Language': 'eng', 'IsDefault': True},
                                {'Type': 'Audio', 'Index': 5, 'Language': 'eng'}], 'audioStreamIndex=4'),
                              ([{'Type': 'Audio', 'Index': 1, 'Language': 'jpn', 'IsDefault': True},
                                {'Type': 'Audio', 'Index': 3, 'Language': 'en-US'},
                                {'Type': 'Audio', 'Index': 6, 'Language': 'eng'}], 'audioStreamIndex=3')):
            server = FakeServer(work, sound)
            server.media_streams = [{'Type': 'Video', 'Index': 0}] + streams
            run(exe, server, play + 'wait:500,state', env=audio, name='audio-default-rule')
            assert want + '&' in server.streams[0] or server.streams[0].endswith(want), (want, server.streams[0])
        checks.append('default audio track: the 1.2.6 rule unchanged (first English, a later English default wins; '
                      '"English" and "en-US" tags count)')

        # 22d (1.3.2). Subtitles, built with JF_SUBS_ROW=1 (the row stays hidden in the console build until a console run
        #     proves burn-in): the request names the English text track with subtitleMethod=Encode; a stream that does
        #     not open, or brings no picture, reopens at the same place without subtitles and says so in yellow.
        subs_exe = work / 'check_app_subs.exe'
        subprocess.run([GCC, '-O1', '-Wall', '-Wextra', '-Werror', '-std=c11', '-DJF_SUBS_ROW=1', '-Ichromatic/launcher/shared',
                        '-o', str(subs_exe), 'chromatic/jellyfin/check_app.c', '-ljpeg'], cwd=ROOT, check=True)
        subs_on = 'wait:3000,press:DOWN,wait:300,press:A,wait:1500,press:SELECT,wait:100,press:DOWN,press:DOWN,press:DOWN,press:A,press:B,' \
                  'press:A,state,'
        server = FakeServer(work, sound)
        s = run(subs_exe, server, subs_on + 'wait:4000,state', env=audio, name='subs-request')
        assert 'subtitleStreamIndex=3&subtitleMethod=Encode' in server.streams[0] and 'audioStreamIndex=2' in server.streams[0], \
            server.streams[0]
        assert field(run.ui[-1], 'subs') == '1' and int(field(s[-1], 'shown')) > 0, (run.ui[-1], s[-1])
        assert tracks_lines()[-1] == 'jellyfin tracks audio=2 lang=eng of=2 subs=3', tracks_lines()
        for mode in ('missing', 'empty'):
            server = FakeServer(work, sound)
            server.subs_stream = mode
            s = run(subs_exe, server, subs_on + 'wait:700,state,shot:subs-failed-note,wait:4000,state', env=audio, name=f'subs-{mode}')
            assert len(server.streams) == 2 and 'subtitleMethod' in server.streams[0] and 'subtitle' not in server.streams[1], \
                (mode, server.streams)
            assert start_s(server.streams[0]) == start_s(server.streams[1]), (mode, 'reopened at the same place', server.streams)
            assert field(s[-1], 'playing') == '1' and int(field(s[-1], 'shown')) > 0, (mode, 'playback never breaks', s[-1])
            assert any(n.startswith('NOTE warn=1 on=1 text=Subtitles did not work here: playing without them') for n in notes()),                 (mode, 'said in yellow', notes())
            assert tracks_lines()[-1].endswith('subs=-1'), tracks_lines()
        checks.append('subtitles (JF_SUBS_ROW=1 build): subtitleStreamIndex=<English text track>&subtitleMethod=Encode; a stream '
                      'that fails to open or ends before any picture reopens at the same place without subtitles, yellow note')

        # 23. Position never goes backwards over a long run with Wi-Fi dips, loop stalls (underruns) and
        #     adaptation/reconnect reopens; and every (re)open uses a FRESH playSessionId. The owner's
        #     "video keeps resetting to the start" is Jellyfin replaying a transcode whose playSessionId it
        #     has already served, from that file's first byte: proven on the real server by
        #     scripts/probe_chromatic_reopen.py. A fresh id per open stops the replay; monotonic position is
        #     proven here (the app never rewinds its own clock across a reopen).
        server = FakeServer(work, sound)
        states = run(exe, server, play + ','.join(['wait:2200,state'] * 15),
                     env=dict(audio, RATE='11000', GAP_EVERY='4000', GAP_MS='1400', STALL_EVERY='9000',
                              STALL_MS='700', FAIL_AT_MS='12000'), name='monotonic')
        positions = [int(field(s, 'pos')) for s in states]
        assert positions == sorted(positions), ('position went backwards (a reopen reset to the start)', positions)
        assert positions[-1] >= positions[0] + 8, ('time advanced over the run', positions)
        sess = sessions_in(server.streams)
        assert len(server.streams) >= 2, ('the stream was reopened (adaptation / reconnect)', server.streams)
        assert len(set(sess)) == len(sess), ('every (re)open uses a fresh playSessionId', sess)
        assert field(states[-1], 'playing') == '1', ('still playing after the whole run', states[-1])
        checks.append(f'position monotonic + fresh sessions: {len(server.streams)} (re)opens over ~33 s with dips, '
                      f'{len([1 for s in states])} samples, position never went backwards, every playSessionId distinct')

        # 24. A decoder cycle just over a frame time WITH sound must not step the frame rate down or reopen
        #     (the owner's "fps dropped to 6, slowed=3" with sound): the measured cycle includes waiting for
        #     the audio clock, and catch-up skips are the sound's, never the decoder's. (4c proves the same
        #     decode WITHOUT sound still steps down, so genuine slow decode is still handled.)
        server = FakeServer(work, sound)
        s = run(exe, server, play + 'wait:12000,state,wait:30000,state', env=dict(audio, RATE='100000', DECODE_MS='50',
                FRAME_SEND_MS='4', LOOP_MS='10', NET_BUF='4096'), name='sound-no-stepdown')
        mid, c, snd = s[1], s[-1], sound_lines()[1]
        assert field(mid, 'playing') == '1' and field(mid, 'slowed') == '0' and field(mid, 'cap') == '15', ('playing on the sound clock mid-run', mid)
        assert field(snd, 'on') == '1' and field(snd, 'dead') == '0', ('sound is the clock', snd)
        assert int(field(c, 'cycle')) > 59, ('the cycle is over 90% of a 66 ms frame (the old step-down line)', c)
        assert field(c, 'slowed') == '0' and field(c, 'cap') == '15', ('no fps step-down with sound', c)
        assert len(server.streams) == 1 and field(c, 'dropped') == '0', ('no reopen; nothing charged to the decoder', c, server.streams)
        checks.append(f'no fps step-down from sound: 50 ms decode (cycle {field(c, "cycle")} ms > a 66 ms frame) with sound '
                      f'held 15 fps, 0 reopens, 0 dropped, {field(c, "shown")} shown on the sound clock')
        # 25. Launcher 1.2.6 on the console: steady sound underruns (~6 a second), 92% of the samples
        #     played, 13.4 of 15 pictures a second, position monotonic, nothing dropped or rebased, cycle 56-58 ms,
        #     ~100 KB/s from the network. Modelled exactly: the console's measured figures (44 ms decode, 4 ms frame
        #     send, 10 ms loop, 4 KB stream buffer, 2 KB ring, 512-frame DMA) and a 100 KB/s link carrying the real
        #     1 Mbit/s server stream (sample-adpcm-1m.avi, ~122 KB/s: 7.6 KB pictures + 8 KB/s of sound; else a
        #     synthetic one at that rate). With the sound as the clock a link that cannot carry the stream makes
        #     nothing "late": the clock stalls with the ring, so 1.2.6 never adapted and played slower with gaps.
        #     1.2.7 measures the sound itself: fewer samples played than wall time asks for (>4% over a 5 s window)
        #     halves the quality once, the rate that starved is never earned back, and the underruns stop.
        fast = PRIVATE / 'chromatic-samples' / 'sample-adpcm-1m.avi'
        if not fast.exists():
            fast = work / 'synthetic-adpcm-1m.avi'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=15',
                            '-f', 'lavfi', '-i', 'sine=f=440:r=48000', '-t', '70', '-c:v', 'mjpeg', '-b:v', '1000000',
                            '-maxrate', '1000000', '-bufsize', '250000', '-pix_fmt', 'yuvj420p', '-c:a', 'adpcm_ima_wav',
                            '-ar', '16000', '-ac', '1', str(fast)], check=True)
        console_1_2_6 = dict(audio, RATE='100000', DECODE_MS='44', FRAME_SEND_MS='4', LOOP_MS='10', NET_BUF='4096',
                             AUDIO_RING='2048', AUDIO_DMA='512')
        server = FakeServer(work, fast)
        server.honour_bitrate = True
        s = run(exe, server, play + ','.join(['wait:5000,state'] * 7), env=console_1_2_6, name='console-starved')
        snd = sound_lines()
        # before the first 5 s window closes: the 1.2.6 picture (the step-down happens at the first window)
        early, early_snd = s[1], snd[1]
        t0, t1 = int(field(s[0], ' t')), int(field(early, ' t'))
        early_played, early_silence = int(field(early_snd, 'played')), int(field(early_snd, 'silence'))
        assert int(field(early_snd, 'underruns')) >= 8, ('1.2.6 reproduced: steady underruns on a 100 KB/s link', early_snd)
        assert early_silence >= 0.04 * (early_played + early_silence), ('a few percent of the time is silence', early_snd)
        assert field(early, 'rebased') == '0' and field(early, 'dropped') == '0', ('nothing is "late" on the stalled clock', early)
        assert int(field(early, 'shown')) < 0.97 * 15 * (t1 - t0) / 1000, ('fewer than 15 pictures a second', early, t1 - t0)
        # then the fix: one quality step-down for the starving sound, the rate remembered, and no more underruns
        last = s[-1]
        assert field(last, 'bitrate') == '500000' and field(last, 'softened') == '1' and len(server.streams) == 2, (last, server.streams)
        assert field(snd[-1], 'starved') == '1' and field(snd[-1], 'ratecap') == '1000000', snd[-1]
        assert 'videoBitRate=500000' in server.streams[1], server.streams[1]
        settled = next(i for i in range(len(s)) if field(s[i], 'bitrate') == '500000')
        after = [(int(field(s[i], ' t')), int(field(s[i], 'shown')), int(field(snd[i], 'underruns')), int(field(snd[i], 'played')))
                 for i in range(settled + 1, len(s)) if field(s[i], 'playing') == '1']
        assert len(after) >= 3, ('at least 15 s of playback after the step-down', after)
        assert after[-1][2] == after[0][2], ('no underrun after the step-down', after)
        span = (after[-1][0] - after[0][0]) / 1000
        assert abs((after[-1][1] - after[0][1]) / span - 15) <= 0.5, ('15 pictures a second after the step-down', after)
        assert abs((after[-1][3] - after[0][3]) / span - 16000) <= 300, ('the sound keeps up after the step-down', after)
        positions = [int(field(x, 'pos')) for x in s]
        assert positions == sorted(positions) and field(last, 'slowed') == '0' and field(last, 'cap') == '15', (positions, last)
        checks.append(f'console 1.2.6 reproduced and fixed: 100 KB/s link, real 1 Mbit/s stream, 44 ms decode: '
                      f'{field(early_snd, "underruns")} underruns and {100 * early_silence // (early_played + early_silence)}% silence '
                      f'in the first {(t1 - t0) / 1000:.0f} s with nothing "late" (1.2.6); then one step-down to 500 kbit/s '
                      f'for the starving sound, {after[-1][1] - after[0][1]} pictures in {span:.0f} s at 15 fps, 0 underruns after')

        # 26. The sound task itself (jellyfin_esp32.c): up to 1.2.6 an empty ring wrote a buffer of silence at once,
        #     even with 24 ms of sound still in the DMA, so every moment the ring ran empty between two main-loop
        #     feeds became an 8 ms gap. Modelled with AUDIO_WAIT=0 on a link that carries the stream after the
        #     step-down: the hitches go on (and the deficit rule keeps halving); 1.2.7's task waits for the ring
        #     while the DMA plays what it holds (AUDIO_WAIT=1, check 25): none after the step-down.
        server = FakeServer(work, fast)
        server.honour_bitrate = True
        s = run(exe, server, play + ','.join(['wait:5000,state'] * 7), env=dict(console_1_2_6, RATE='120000', AUDIO_WAIT='0'),
                name='console-old-task')
        snd = sound_lines()
        settled = next((i for i in range(len(s)) if field(s[i], 'bitrate') != '1000000'), None)
        if settled is None and fast.name.startswith('synthetic'):
            # The made-up test pattern compresses too well to starve the old task's sound; this regression model
            # needs a real-looking 1 Mbit/s sample (put one at <your Jellymatic folder>/chromatic-samples/).
            checks.append('old sound task model: SKIPPED with the synthetic sample (needs a real 1 Mbit/s sample)')
        else:
            old_after = int(field(snd[-1], 'underruns')) - int(field(snd[settled], 'underruns'))
            assert old_after >= 10 and int(field(snd[-1], 'pushed')) > 0, ('the old task keeps hitching once the link suffices', snd[-1], old_after)
            checks.append(f'old sound task modelled (silence written the moment the ring is empty): {old_after} more underruns after the '
                          f'step-down on a link that carries the stream; the 1.2.7 task (check 25) has 0')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    checks.append(f'memory: the whole app is {run.size} bytes (budget 56 KB; the console has about 75 KB free with Wi-Fi)')
    for c in checks:
        print('PASS:', c)
    print(f'Previews in {OUT}')


if __name__ == '__main__':
    os.environ['PATH'] = r'C:\msys64\ucrt64\bin;' + os.environ['PATH']
    main()
