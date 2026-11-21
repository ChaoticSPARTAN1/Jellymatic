# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Chromatic launcher 1.6.0: the TV and ChromaTube apps (chromatic/jellyfin/jf_app.h) on the PC.

A fake Jellyfin server that reproduces what was measured on the owner's server (docs/Design-notes.md.6.md):
live channels open only through PlaybackInfo; their AVI header says 15 pictures a second (the cap asked for), they
carry up to about 83 KB for a second of video with sound, and the server sends the first seconds faster than real
time; a channel's pictures can stop while its sound goes on; a ChromaTube video comes as full-size pictures with
no sound unless PlaybackInfo was asked first. Streams whose header says more than the cap (24 to 60 a second) are
kept as tests of the player's guard for them. The console's side is
modelled with its measured figures (docs/Design-notes.md): a link of about 100 KB/s, 44 ms to
decode a picture, 4 ms to send it, a 10 ms loop, a 4 KB stream buffer and a 2 KB sound ring.
Synthetic channels, videos and streams only: no hardware, no real server, nothing private read or printed.
Screen previews go to staging/chromatic-tv/*/; `--previews` also copies the 1.6.0 set to
chromatic/launcher/previews-1.6.0/ (not done by an ordinary run).
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
from urllib.parse import unquote

import test_chromatic_player as base

ROOT = base.ROOT
OUT = ROOT / 'staging' / 'chromatic-tv'
PREVIEWS = ROOT / 'chromatic' / 'launcher' / 'previews-1.6.0'
NEW = ROOT / 'chromatic' / 'launcher' / 'previews-1.6.4'   # the not-set-up pages (--previews or --not-set-up-previews)
GCC = base.GCC
USER = base.USER
APP_BYTES_1_5_1 = 26048  # sizeof(jf_app) of launcher 1.5.1 (scripts/test_chromatic_player.py, 2026-11-19)
# The console as measured (the independent review's figures): link, decoder, overlay send, loop, buffers.
CONSOLE = {'RATE': '100000', 'DECODE_MS': '44', 'FRAME_SEND_MS': '4', 'LOOP_MS': '10', 'NET_BUF': '4096', 'AUDIO_RING': '2048'}

ODD_NAME = 'T\u00e9l\u00e9\U0001F4FA\u00d1and\u00fa\u2013\u00df\u65e5\u672c'  # accents, an emoji, a dash, sharp s, two CJK letters
CHANNELS = [(f'{n}', name) for n, name in enumerate(
    ['Owner One', 'Owner Two', 'Owner Three', 'Owner Four', 'Owner Five', 'Owner Six', 'Owner Seven', 'Owner Eight',
     'Owner Nine', ODD_NAME] + [f'Cartoon Channel {i + 1} with a long name' for i in range(25)], 2)]
TUBE = base.ident(0x700)
TUBE_SERIES, TUBE_SEASON = base.ident(0x710), base.ident(0x711)
TUBE_VIDEOS = [base.ident(0x720 + i) for i in range(3)]
ODD_LIVE = 'f00d|native_live 7/\u00e9'  # a live stream id with characters that must be escaped in a query


def channel_id(index):
    return base.ident(0x500 + index)


def channel(index):
    number, name = CHANNELS[index]
    return {'Name': name, 'ServerId': 'f' * 32, 'Id': channel_id(index), 'ChannelNumber': number, 'Number': number,
            'IsFolder': False, 'Type': 'TvChannel', 'ImageTags': {}, 'BackdropImageTags': [], 'ImageBlurHashes': {},
            'LocationType': 'Remote', 'MediaType': 'Video', 'ChannelType': 'TV', 'SortName': f'{int(number):05d}-{name}',
            'PremiereDate': '2026-01-01T00:00:00.0000000Z', 'ChannelId': None, 'PlayAccess': 'Full', 'CanDelete': False,
            'EnableMediaSourceDisplay': True, 'CompletionPercentage': 0.0, 'ExternalUrls': [], 'Taglines': [], 'Genres': []}


def media_stream(kind, index, **extra):
    """A MediaStreams entry about as long as the real server's."""
    body = {'Codec': 'h264' if kind == 'Video' else 'aac', 'TimeBase': '1/90000', 'VideoRange': 'SDR', 'VideoRangeType': 'SDR',
            'AudioSpatialFormat': 'None', 'DisplayTitle': '1080p H264 SDR' if kind == 'Video' else 'AAC - Stereo',
            'IsInterlaced': False, 'IsAVC': kind == 'Video', 'BitRate': 4000000, 'IsDefault': True, 'IsForced': False,
            'IsHearingImpaired': False, 'Profile': 'High', 'Type': kind, 'AspectRatio': '16:9', 'Index': index,
            'IsExternal': False, 'IsTextSubtitleStream': False, 'SupportsExternalStream': False, 'PixelFormat': 'yuv420p',
            'Level': 40, 'IsAnamorphic': False, 'NalLengthSize': '4', 'RefFrames': 1, 'AverageFrameRate': 29.97,
            'RealFrameRate': 29.97, 'ReferenceFrameRate': 29.97, 'ChannelLayout': 'stereo', 'Channels': 2, 'SampleRate': 48000}
    body.update(extra)
    return body


class Streams:
    """The fake server's conversions: 160x90 MJPEG with IMA ADPCM sound at a picture rate and bit rate, made once."""

    def __init__(self, folder):
        self.folder = folder
        self.wrong = folder / 'wrong.avi'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=1920x1080:r=30', '-t', '2', '-c:v', 'mjpeg',
                        '-q:v', '4', '-pix_fmt', 'yuvj420p', '-an', str(self.wrong)], check=True)
        head = self.wrong.read_bytes()[:16384]
        assert b'auds' not in head and (1920).to_bytes(4, 'little') + (1080).to_bytes(4, 'little') in head, 'the wrong stream: 1920x1080, no sound'

    def live(self, fps='15', rate=400000, seconds=75):
        path = self.folder / f'live-{fps.replace("/", "_")}-{rate}-{seconds}.avi'
        if not path.exists():
            # busy pictures (noise over the test card) so the encoder really spends the bit rate it is given
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=s=160x90:r={fps},noise=alls=14:allf=t',
                            '-f', 'lavfi', '-i', 'sine=f=440:r=16000', '-t', str(seconds), '-c:v', 'mjpeg', '-b:v', str(rate),
                            '-minrate', str(rate), '-maxrate', str(rate), '-bufsize', str(rate // 2), '-qmin', '1', '-qmax', '31',
                            '-pix_fmt', 'yuvj420p', '-c:a', 'adpcm_ima_wav', '-ac', '1', '-ar', '16000', str(path)], check=True)
            head = path.read_bytes()[:16384]
            assert b'auds' in head and (160).to_bytes(4, 'little') + (90).to_bytes(4, 'little') in head, 'the right stream: 160x90 with sound'
        return path

    def frozen(self, after_s=19):
        """As measured on a real channel: the pictures stop after `after_s` seconds, the sound goes on."""
        src, dst = self.live('15', 600000), self.folder / 'live-frozen.avi'
        if dst.exists():
            return dst
        data = bytearray(src.read_bytes())
        movi = None
        for fourcc, at, size in base.avi_chunks(data, 12, len(data)):
            if fourcc == b'LIST' and data[at:at + 4] == b'movi':
                movi = (at - 8, at + 4, at + size)
        out, body, pictures = bytearray(data[:movi[0]]), bytearray(), 0
        for fourcc, at, size in base.avi_chunks(data, movi[1], movi[2]):
            if fourcc == b'idx1':
                continue
            if fourcc in (b'00dc', b'00db'):
                pictures += 1
                if pictures > after_s * 15:
                    continue
            body += data[at - 8:at + size] + (b'\0' if size & 1 else b'')
        out += b'LIST' + struct.pack('<I', 4 + len(body)) + b'movi' + body
        struct.pack_into('<I', out, 4, len(out) - 8)
        dst.write_bytes(out)
        return dst

    def markers(self):
        """Review M2: the 24-a-second stream with an empty picture chunk in front of every picture and a header that
        says 48 a second, so every real picture sits on an odd frame number."""
        src, dst = self.live('24'), self.folder / 'live-markers.avi'
        if dst.exists():
            return dst
        data = bytearray(src.read_bytes())
        avih_at = strh_at = movi = None
        for fourcc, at, size in base.avi_chunks(data, 12, len(data)):
            if fourcc != b'LIST':
                continue
            if data[at:at + 4] == b'hdrl':
                for f2, at2, size2 in base.avi_chunks(data, at + 4, at + size):
                    if f2 == b'avih':
                        avih_at = at2
                    elif f2 == b'LIST' and data[at2:at2 + 4] == b'strl':
                        for f3, at3, _ in base.avi_chunks(data, at2 + 4, at2 + size2):
                            if f3 == b'strh' and data[at3:at3 + 4] == b'vids' and strh_at is None:
                                strh_at = at3
            elif data[at:at + 4] == b'movi':
                movi = (at - 8, at + 4, at + size)
        out, body = bytearray(data[:movi[0]]), bytearray()
        for fourcc, at, size in base.avi_chunks(data, movi[1], movi[2]):
            if fourcc == b'idx1':
                continue
            if fourcc in (b'00dc', b'00db'):
                body += b'00dc' + struct.pack('<I', 0)
            body += data[at - 8:at + size] + (b'\0' if size & 1 else b'')
        out += b'LIST' + struct.pack('<I', 4 + len(body)) + b'movi' + body
        scale, rate = struct.unpack('<II', data[strh_at + 20:strh_at + 28])
        struct.pack_into('<I', out, avih_at, struct.unpack('<I', data[avih_at:avih_at + 4])[0] // 2)
        struct.pack_into('<II', out, strh_at + 20, scale, rate * 2)
        struct.pack_into('<I', out, 4, len(out) - 8)
        dst.write_bytes(out)
        return dst


class TvServer(base.FakeServer):
    """LiveTv channels, PlaybackInfo, the two closes, and a ChromaTube library, on top of the player suite's server."""

    def __init__(self, folder, streams):
        super().__init__(folder, streams.live())
        self.files = streams
        self.fps = '15'               # the picture rate live channels come at: the cap, as measured; others test the guard
        self.measured = False         # the stream as measured: about 75 KB/s of pictures whatever bit rate is asked
        self.frozen = False           # the pictures stop after 19 s, the sound goes on
        self.short = None             # a channel whose stream ends after this many seconds
        self.markers = False          # review M2's stream
        self.log = []                 # ('REQ', method, path) and ('STREAM', path), in order
        self.live = {}                # live stream id -> channel id, while open on the "server"
        self.closed = []              # live stream ids closed, in order
        self.stopped = []             # playSessionIds whose conversion was stopped
        self.opened = 0
        self.dead = set()             # channel ids whose PlaybackInfo fails (500)
        self.no_stream = set()        # channel ids that open but whose stream cannot be opened
        self.empty_stream = set()     # channel ids that open, whose stream ends at once
        self.odd_live = False         # live stream ids with characters that need escaping
        self.live_length = 0          # live stream ids of this many characters
        self.tracks = 2               # media streams in a PlaybackInfo reply (16: larger than the console's buffer)
        self.info_delay = None        # hold PlaybackInfo's reply back this many ms
        self.info_lost = None         # None; 'empty': no reply (-1) after 30 s; 'partial': it breaks off after the ids
        self.probed = set()           # ChromaTube items the server has learnt (PlaybackInfo asked)
        self.served = []              # which file each stream request got
        self.libraries = None         # /UserViews items (None: Movies, Shows and ChromaTube)
        self.channel_count = None     # 1.6.4: channels on the server (None: all of CHANNELS; 0: no tuner / no channels yet)
        self.tube_empty = False       # 1.6.4: the ChromaTube library is there but holds nothing yet
        self.playinfo_bytes = 0

    def stream_file(self, path):
        ident = path.split('/')[2]
        q = dict(part.split('=', 1) for part in path.partition('?')[2].split('&') if '=' in part)
        if ident in TUBE_VIDEOS or 0x500 <= int(ident, 16) < 0x500 + len(CHANNELS):
            assert 'startTimeTicks' not in q, ('never a position for a live channel or a ChromaTube video', path)
            rate = int(q['videoBitRate'])
            assert rate <= 500000, ('TV and ChromaTube never ask for more than 500 kbit/s', path)
        if ident in TUBE_VIDEOS:
            # the measured fact: without PlaybackInfo first the server sends full-size pictures and no sound
            if ident in self.probed and q.get('mediaSourceId') == 'src' + ident[-6:]:
                self.served.append('good')
                return self.files.live('15', 130000) if self.measured else self.files.live('15', rate)
            self.served.append('wrong')
            return self.files.wrong
        if ident in self.no_stream:
            self.served.append('missing')
            return self.folder / 'no-such-stream.avi'
        if ident in self.empty_stream:
            self.served.append('empty')
            empty = self.folder / 'empty-stream.avi'
            empty.write_bytes(b'')
            return empty
        if 0x500 <= int(ident, 16) < 0x500 + len(CHANNELS):
            live = unquote(q.get('liveStreamId', ''))
            if self.live.get(live) != ident or q.get('mediaSourceId') != 'src' + ident[-6:]:
                self.served.append('missing')
                return self.folder / 'no-such-stream.avi'
            self.served.append('live')
            if self.frozen:
                return self.files.frozen()
            if self.short:
                return self.files.live('15', rate, self.short)
            if self.measured:
                return self.files.live('15', 600000)
            return self.files.markers() if self.markers else self.files.live(self.fps, rate)
        return super().stream_file(path)

    def reply(self, method, path):
        route, _, query = path.partition('?')
        q = dict(part.split('=', 1) for part in query.split('&') if '=' in part)
        handled = route == '/LiveTv/Channels' or route.endswith('/PlaybackInfo') or route in (
            '/Videos/ActiveEncodings', '/LiveStreams/Close') or route == '/UserViews' or (
            route == '/Items' and (q.get('parentId') in (TUBE, TUBE_SERIES, TUBE_SEASON) or q.get('ids') in TUBE_VIDEOS))
        if not handled:
            return super().reply(method, path)
        self.requests.append((method, path))
        for table in (self.fail_once, self.fail):
            for prefix, status in list(table.items()):
                if path.startswith(prefix):
                    if table is self.fail_once:
                        del table[prefix]
                    return status, b''
        if 'userId' in q and q['userId'] != USER:
            return 400, b''
        if route == '/LiveTv/Channels' and method == 'GET':
            start, limit = int(q.get('startIndex', 0)), int(q.get('limit', 100))
            assert q.get('enableImages') == 'false' and q.get('enableUserData') == 'false' and q.get('addCurrentProgram') == 'false', path
            count = len(CHANNELS) if self.channel_count is None else self.channel_count
            body = {'Items': [channel(i) for i in range(start, min(start + limit, count))],
                    'TotalRecordCount': count, 'StartIndex': start}
            return 200, json.dumps(body).encode()
        if route.endswith('/PlaybackInfo') and method == 'POST':
            ident = route.split('/')[2]
            if ident in self.dead:
                return 500, b''
            names = ['Fran\u00e7ais (st\u00e9r\u00e9o) \u65e5\u672c\u8a9e \U0001F3A7 ' + 'x' * 40] * (self.tracks - 1)
            source = {'Protocol': 'Http', 'Id': 'src' + ident[-6:], 'Path': 'http://example.invalid/stream', 'Type': 'Default',
                      'Container': 'mpegts', 'IsRemote': True, 'SupportsTranscoding': True, 'SupportsDirectStream': False,
                      'SupportsDirectPlay': False, 'IsInfiniteStream': True, 'RequiresOpening': True, 'RequiresClosing': True,
                      'LiveStreamId': None, 'TranscodingUrl': '/videos/' + ident + '/master.m3u8?' + 'x' * 400,
                      'TranscodingSubProtocol': 'http', 'TranscodingContainer': 'avi', 'DefaultAudioStreamIndex': 1,
                      'MediaStreams': [media_stream('Video', 0, Width=1920, Height=1080)] + [
                          media_stream('Audio', i + 1, Title=name, DisplayTitle=name + ' "quoted" \\ {"Id":"decoy"}')
                          for i, name in enumerate(names)],
                      'MediaAttachments': [{'Id': 'decoy-attachment', 'LiveStreamId': 'decoy-live'}]}
            if ident in TUBE_VIDEOS:
                self.probed.add(ident)
                source['IsInfiniteStream'] = False
            else:
                self.opened += 1
                live = ODD_LIVE + str(self.opened) if self.odd_live else ('L' * (self.live_length - 4) + f'{self.opened:04d}') \
                    if self.live_length else f'{"a" * 32}_{ident}_{self.opened:032x}'
                self.live[live] = ident
                source['LiveStreamId'] = live
            data = json.dumps({'MediaSources': [source, {'Id': 'second-source', 'LiveStreamId': 'second-live'}],
                               'PlaySessionId': 'f' * 32}, ensure_ascii=self.opened % 2 == 0).encode()
            self.playinfo_bytes = max(self.playinfo_bytes, len(data))
            if self.info_lost == 'empty':
                return -1, b'', 30000
            if self.info_lost == 'partial':
                return -1, data[:data.index(b'TranscodingUrl')], 30000
            return (200, data, self.info_delay) if self.info_delay else (200, data)
        if route == '/Videos/ActiveEncodings' and method == 'DELETE':
            self.stopped.append(q.get('playSessionId'))
            return 204, b''
        if route == '/LiveStreams/Close' and method == 'POST':
            live = unquote(q.get('liveStreamId', ''))
            self.closed.append(live)
            self.live.pop(live, None)
            return 204, b''
        if route == '/UserViews':
            views = self.libraries if self.libraries is not None else [
                base.item(base.MOVIES, 'Movies', 'CollectionFolder'), base.item(base.SHOWS, 'Shows', 'CollectionFolder'),
                base.item(TUBE, 'ChromaTube', 'CollectionFolder', CollectionType='tvshows')]
            return 200, json.dumps({'Items': views, 'TotalRecordCount': len(views)}).encode()
        videos = [base.item(v, f'A video with a long title {i + 1}', 'Episode', SeriesName='Maker Channel', SeriesId=TUBE_SERIES,
                            ParentIndexNumber=2026, IndexNumber=i + 1, RunTimeTicks=600 * base.TICKS, IsFolder=False,
                            UserData={'PlaybackPositionTicks': (240 if i == 0 else 0) * base.TICKS, 'Played': False})
                  for i, v in enumerate(TUBE_VIDEOS)]
        if 'ids' in q:
            match = next(v for v in videos if v['Id'] == q['ids'])
            if 'MediaStreams' in q.get('fields', ''):
                return 200, json.dumps({'Items': [{'Id': q['ids'], 'MediaStreams': []}], 'TotalRecordCount': 1}).encode()
            return 200, json.dumps({'Items': [dict(match, Overview='A synthetic video.', Genres=[])], 'TotalRecordCount': 1}).encode()
        everything = {TUBE: [] if self.tube_empty else [base.item(TUBE_SERIES, 'Maker Channel', 'Series')],
                      TUBE_SERIES: [base.item(TUBE_SEASON, 'Season 2026', 'Season')], TUBE_SEASON: videos}[q['parentId']]
        start, limit = int(q.get('startIndex', 0)), int(q.get('limit', 12))
        return 200, json.dumps({'Items': everything[start:start + limit], 'TotalRecordCount': len(everything)}).encode()


def run(exe, server, script, env=None, name='run'):
    """The player suite's driver, plus: a POST without a body ("BODY 0"), DELETE, a reply held back for its own time,
    replies of any size (only PlaybackInfo's may be larger than the console's buffer), and one ordered log."""
    shots = OUT / name
    shots.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(exe), script, str(shots)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env={**os.environ, **(env or {})})
    err_chunks = []
    err_thread = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    err_thread.start()
    while True:
        line = proc.stdout.readline().decode()
        if not line:
            break
        if line.startswith('REQ '):
            _, method, path = line.strip().split(' ', 2)
            body = None
            if method == 'POST':
                n = int(proc.stdout.readline().decode().split()[1])
                body = json.loads(proc.stdout.read(n)) if n else None
                server.bodies.append((path, body))
            server.log.append(('REQ', method, path))
            answer = server.reply(method, path)
            status, data, delay = answer if len(answer) == 3 else (*answer, None)
            assert len(data) <= 14336 or status != 200 or 'PlaybackInfo' in path, ('a reply larger than the console keeps', path, len(data))
            proc.stdin.write(f'REP {status} {len(data)}{"" if delay is None else " " + str(delay)}\n'.encode() + data)
            proc.stdin.flush()
        elif line.startswith('STREAM '):
            path = line.strip()[7:]
            server.streams.append(path)
            server.log.append(('STREAM', path))
            name_bytes = str(server.stream_file(path)).encode()
            proc.stdin.write(f'FILE {len(name_bytes)}\n'.encode() + name_bytes)
            proc.stdin.flush()
        elif line.strip() == 'END':
            break
    err_thread.join(timeout=60)
    err = (err_chunks[0] if err_chunks else b'').decode(errors='replace')
    code = proc.wait(timeout=60)
    if code:
        raise AssertionError(f'{name}: harness exit {code}: {err[-1200:]}')
    run.err = err
    run.tv = [l for l in err.splitlines() if l.startswith('TV ')]
    run.sound = [l for l in err.splitlines() if l.startswith('SOUND')]
    run.quiesce = [l for l in err.splitlines() if l.startswith('QUIESCE')]
    run.size = next(int(l.split('=')[1]) for l in err.splitlines() if l.startswith('SIZE app='))
    for ppm in shots.glob('*.ppm'):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(ppm), '-vf', 'scale=640:576:flags=neighbor',
                        str(ppm.with_suffix('.png'))], check=True)
        ppm.unlink()
    return [l for l in err.splitlines() if l.startswith('STATE')]


field = base.field


def tv_lifecycle(server, leaked=0):
    """Every channel that was opened (a PlaybackInfo answered 200) is closed before anything else is opened:
    its conversion stopped when a stream was asked for, then its live stream closed, in that order."""
    pending = None  # [channel, stream session or None, stopped]
    opens = 0
    for entry in server.log:
        if entry[0] == 'STREAM':
            assert pending and '/Videos/' + pending[0] + '/' in entry[1], ('a stream without its PlaybackInfo', entry)
            assert pending[1] is None, ('a second stream for one opening (no reopen of a live channel)', entry)
            pending[1] = re.search(r'playSessionId=([0-9a-f]+)', entry[1]).group(1)
            assert 'startTimeTicks' not in entry[1] and 'liveStreamId=' in entry[1], entry
            continue
        _, method, path = entry
        if path.endswith('/PlaybackInfo?userId=' + USER) and method == 'POST':
            assert pending is None, ('a channel opened before the last one was closed', path)
            ident = path.split('/')[2]
            if ident not in server.dead and server.info_lost != 'empty':
                pending = [ident, None, False]
                opens += 1
        elif path.startswith('/Videos/ActiveEncodings') and method == 'DELETE':
            assert pending and pending[1] and not pending[2], ('a stop with nothing to stop', path)
            assert f'playSessionId={pending[1]}' in path and 'deviceId=chromatic-test' in path, path
            pending[2] = True
        elif path.startswith('/LiveStreams/Close') and method == 'POST':
            assert pending, ('a close with nothing open', path)
            assert (pending[1] is None) or pending[2], ('the live stream closed before its conversion was stopped', path)
            pending = None
        else:
            assert not path.startswith('/Sessions/'), ('a live channel is never reported', path)
    assert pending is None, ('a channel was left open on the server', pending)
    assert len(server.live) == leaked, ('live streams left open', server.live)
    return opens


def sound_share(sound, first, last):
    """Sound really played between two SOUND lines, as a share of the time, and underruns a second of sound time."""
    played = int(field(sound[last], 'played')) - int(field(sound[first], 'played'))
    silence = int(field(sound[last], 'silence')) - int(field(sound[first], 'silence'))
    under = int(field(sound[last], 'underruns')) - int(field(sound[first], 'underruns'))
    return played / max(played + silence, 1), under / max((played + silence) / 16000, 1e-9)


def main():
    previews = '--previews' in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=OUT))
    checks = []
    try:
        exe = work / 'check_app.exe'
        subprocess.run([GCC, '-O1', '-Wall', '-Wextra', '-Werror', '-std=c11', '-Ichromatic/launcher/shared', '-o', str(exe),
                        'chromatic/jellyfin/check_app.c', '-ljpeg'], cwd=ROOT, check=True)
        streams = Streams(work)
        tv = {'MODE': 'tv', 'WX': '1'}
        rate_400 = base.video_bytes_per_s(streams.live('15', 400000)) / 1000
        rate_meas = base.video_bytes_per_s(streams.live('15', 600000)) / 1000
        assert 40 <= rate_400 <= 60 and 68 <= rate_meas <= 82, ('the fake conversions: about 50 KB/s at 400 kbit/s, 75 as measured', rate_400, rate_meas)

        # 1. The channel list: the built-in channel first, seven of the server's a page, lean requests, paging and wrap.
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,expect:8,state,shot:tv-list,' + 'press:DOWN,' * 7 + 'state,press:DOWN,wait:300,state,shot:tv-list-page2,'
                'press:UP,wait:300,state,' + 'press:UP,' * 7 + 'state,press:UP,wait:300,state,shot:tv-list-last,press:DOWN,wait:300,state',
                env=tv, name='tv-list')
        t = run.tv
        first = server.requests[0]
        assert first == ('GET', f'/LiveTv/Channels?userId={USER}&startIndex=0&limit=7&enableImages=false&enableUserData=false'
                                '&addCurrentProgram=false'), first
        assert all(m == 'GET' and p.startswith('/LiveTv/Channels?') and 'token' not in p.lower() and 'api_key' not in p.lower()
                   for m, p in server.requests), server.requests
        assert not server.streams and not any('/Images/' in p or 'UserViews' in p for _, p in server.requests)
        assert (field(t[0], 'focus'), field(t[0], 'count'), field(t[0], 'total'), field(t[0], 'number')) == ('0', '7', '35', 'WX'), t[0]
        assert (field(t[1], 'focus'), field(t[1], 'start'), field(t[1], 'number')) == ('7', '0', '8'), t[1]
        assert (field(t[2], 'focus'), field(t[2], 'start'), field(t[2], 'number')) == ('0', '7', '9'), t[2]
        assert (field(t[3], 'focus'), field(t[3], 'start'), field(t[3], 'number')) == ('7', '0', '8'), t[3]
        assert (field(t[4], 'focus'), field(t[4], 'number')) == ('0', 'WX'), t[4]
        assert (field(t[5], 'focus'), field(t[5], 'start'), field(t[5], 'count'), field(t[5], 'number')) == ('6', '28', '7', '36'), t[5]
        assert (field(t[6], 'focus'), field(t[6], 'start'), field(t[6], 'number')) == ('0', '0', 'WX'), t[6]
        starts = [int(re.search(r'startIndex=(\d+)', p).group(1)) for _, p in server.requests]
        assert starts == [0, 7, 0, 28, 0], starts
        page = len(json.dumps({'Items': [channel(i) for i in range(7)], 'TotalRecordCount': 35, 'StartIndex': 0}))
        checks.append(f'TV channel list: built-in channel first, 7 server channels a page ({page} B for a page of this fake, buffer 14,336 B), '
                      'no images / user data / programme asked for, Down and Up page and wrap at both ends, no other request')
        server = TvServer(work, streams)
        run(exe, server, 'wait:1500,expect:8,state,shot:tv-list-plain,press:UP,wait:300,state', env={'MODE': 'tv'}, name='tv-list-plain')
        assert (field(run.tv[0], 'number'), field(run.tv[1], 'number'), field(run.tv[1], 'start')) == ('2', '36', '28'), run.tv
        checks.append('TV channel list without the built-in channel: the server channels alone, Up from the first wraps to the last')
        # names the console's font cannot show: accents folded, typographic marks plain, emoji and other scripts left out
        server = TvServer(work, streams)
        run(exe, server, 'wait:1500,' + 'press:DOWN,' * 7 + 'wait:300,press:DOWN,press:DOWN,wait:300,state,shot:tv-list-names', env={'MODE': 'tv'},
            name='tv-names')
        assert field(run.tv[0], 'number') == '11' and field(run.tv[0], 'name') == 'TeleNandu-ss', run.tv[0]
        checks.append('TV names: "T\\u00e9l\\u00e9<emoji>\\u00d1and\\u00fa\\u2013\\u00df<two CJK letters>" shows as "TeleNandu-ss" (no "?" runs)')

        # 2. Tuning: PlaybackInfo (the proven body, 500 kbit/s), then the stream with its media source and live stream
        #    and no startTimeTicks; no playback report; B closes the stream, stops the conversion, closes the live
        #    stream, in that order; B on the list leaves only after the closes.
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:1200,state,shot:tv-banner,wait:5000,expect:4,state,shot:tv-playing,'
                             'press:SELECT,wait:200,state,press:B,wait:500,expect:8,state,press:B,wait:500,expect:6,state', env=tv, name='tv-tune')
        t = run.tv
        ident = channel_id(0)
        path, body = server.bodies[0]
        assert path == f'/Items/{ident}/PlaybackInfo?userId={USER}', path
        profile = body['DeviceProfile']
        assert body['UserId'] == USER and body['AutoOpenLiveStream'] is True and body['EnableDirectPlay'] is False \
            and body['EnableDirectStream'] is False and body['EnableTranscoding'] is True and body['MaxStreamingBitrate'] == 400000 \
            and profile['MaxStreamingBitrate'] == 400000 and profile['DirectPlayProfiles'] == [] and profile['TranscodingProfiles'] == [
                {'Container': 'avi', 'Type': 'Video', 'VideoCodec': 'mjpeg', 'AudioCodec': 'mp3', 'Protocol': 'http',
                 'Context': 'Streaming'}], body
        assert len(json.dumps(body, separators=(',', ':'))) < 448, 'the body fits the platform request buffer'
        stream = server.streams[0]
        live = next(iter(server.closed))
        assert re.fullmatch(rf'/Videos/{ident}/stream\.avi\?mediaSourceId=src{ident[-6:]}&playSessionId=[0-9a-f]{{16}}&deviceId=chromatic-test'
                            r'&videoCodec=mjpeg&audioCodec=mp3&audioChannels=1&audioBitRate=32000&maxWidth=160&maxHeight=120&maxFramerate=15'
                            rf'&videoBitRate=400000&liveStreamId={re.escape(live)}', stream), stream
        assert len(stream) < 600, len(stream)
        assert field(t[0], 'banner') == '1' and field(t[0], 'number') == '2' and field(t[0], 'name') == 'Owner', t[0]
        assert field(t[1], 'banner') == '0' and field(s[1], 'playing') == '1' and int(field(s[1], 'shown')) > 60 and field(run.tv[1], 'thinned') == '0', (t[1], s[1])
        assert field(t[2], 'banner') == '1', ('Select shows which channel is on', t[2])
        assert field(s[1], 'fps') == '15', ('the channel comes at the 15 pictures a second that were asked for', s[1])
        assert tv_lifecycle(server) == 1 and server.served == ['live'], server.served
        kinds = [(e[0], e[1] if e[0] == 'REQ' else '', (e[2] if e[0] == 'REQ' else e[1]).split('?')[0]) for e in server.log]
        assert kinds[-4:] == [('REQ', 'POST', f'/Items/{ident}/PlaybackInfo'), ('STREAM', '', f'/Videos/{ident}/stream.avi'),
                              ('REQ', 'DELETE', '/Videos/ActiveEncodings'), ('REQ', 'POST', '/LiveStreams/Close')], kinds[-4:]
        assert server.bodies[-1] == (f'/LiveStreams/Close?liveStreamId={live}', None), server.bodies[-1]
        assert field(s[-1], 'exit_to') == '0' and field(t[-1], 'close') == '0', (s[-1], t[-1])
        checks.append('TV tune: PlaybackInfo (the proven body at 400,000 bit/s) -> stream.avi with mediaSourceId + liveStreamId, '
                      'videoBitRate=400000 and no startTimeTicks; banner for 3 s; no playback report; B: stream closed, DELETE '
                      'ActiveEncodings (same playSessionId), POST LiveStreams/Close, in that order; B on the list returns to the start-up menu; '
                      'without the FPGA\'s sound the profile and the stream both name mp3 (review M6)')
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:8000,state,press:B,wait:500,press:B,wait:500,expect:6', env=dict(tv, AUDIO='1'),
                name='tv-tune-sound')
        assert 'audioCodec=adpcm_ima_wav&audioChannels=1&audioSampleRate=16000&maxWidth=160&maxHeight=120&maxFramerate=15&videoBitRate=400000' \
            in server.streams[0], server.streams[0]
        assert server.bodies[0][1]['DeviceProfile']['TranscodingProfiles'][0]['AudioCodec'] == 'adpcm_ima_wav'
        snd = run.sound[0]
        assert field(snd, 'on') == '1' and field(snd, 'rate') == '16000' and int(field(snd, 'played')) > 16000 * 6, snd
        tv_lifecycle(server)
        checks.append('TV tune with sound: adpcm_ima_wav in the profile and in the stream request (1 channel, 16000 Hz, 160x120, 15, 400000), sound plays')

        # 3a. The streams as measured on the owner's server (second review pass): header 15 a second, about 75 KB/s of
        #     pictures and 8 KB/s of sound for a second of video, the first 20 s of video offered 1.7 times faster than
        #     real time, then in real time; on a 100 and on an 85 KB/s link with the console's decode figures. Paced as
        #     the Jellyfin row: every picture shown (about 15 a second), 99% sound or more, nothing thinned, one stream.
        measured = []
        serve = {'SERVE_BPS': str(int(rate_meas * 1000) + 8000), 'SERVE_BURST_S': '20', 'SERVE_X100': '170'}
        for link in ('100000', '85000'):
            server = TvServer(work, streams)
            server.measured = True
            s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:1000,state,' + 'wait:1500,state,' * 8 + 'press:B,wait:500,press:B,wait:500,expect:6',
                    env=dict(tv, AUDIO='1', **dict(CONSOLE, RATE=link), **serve), name='tv-measured-' + link)
            first, last = s[0], s[8]
            span = (int(field(last, ' t')) - int(field(first, ' t'))) / 1000
            shown = (int(field(last, 'shown')) - int(field(first, 'shown'))) / span
            followed = (int(field(last, 'frame')) - int(field(first, 'frame'))) / span
            share, under = sound_share(run.sound, 0, 8)
            assert span >= 30 and len(server.streams) == 1 and tv_lifecycle(server) == 1, (span, server.streams)
            assert field(last, 'fps') == '15' and field(run.tv[8], 'thinned') == '0' and field(run.tv[8], 'stepped') == '0' and field(last, 'softened') == '0', (last, run.tv[8])
            assert 14.5 <= shown <= 15.2 and abs(followed - 15) <= 0.3 and share >= 0.99, ('about 15 shown a second, clean sound', link, shown, followed, share, under)
            measured.append(f'{int(link) // 1000} KB/s link: {shown:.1f} shown a second, sound {100 * share:.2f}%, {under:.2f} underruns/s')
        checks.append(f'TV, the stream as measured on the server (header 15/s, {rate_meas:.0f} KB/s pictures + 8 KB/s sound, first 20 s at 1.7x), console '
                      'figures, with sound, one stream, nothing thinned, no step down: ' + '; '.join(measured))
        # 3a'. The same at what TV now asks (400 kbit/s) on an 85 KB/s link, and a ChromaTube video as measured (21 KB/s,
        #      offered far faster than real time).
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:1000,state,' + 'wait:1500,state,' * 8 + 'press:B,wait:500,press:B,wait:500,expect:6',
                env=dict(tv, AUDIO='1', **dict(CONSOLE, RATE='85000')), name='tv-400')
        span = (int(field(s[8], ' t')) - int(field(s[0], ' t'))) / 1000
        shown = (int(field(s[8], 'shown')) - int(field(s[0], 'shown'))) / span
        share, under = sound_share(run.sound, 0, 8)
        assert 14.5 <= shown <= 15.2 and share >= 0.99 and field(run.tv[8], 'thinned') == '0' and len(server.streams) == 1, (shown, share)
        checks.append(f'TV at 400 kbit/s honoured ({rate_400:.0f} KB/s of pictures), 85 KB/s link, console figures: {shown:.1f} shown a second, sound {100 * share:.2f}%')
        # 3b. A channel whose pictures stop while its sound goes on (measured on a real channel): "No signal" 20 s after
        #     the last picture, the closes sent, no step down taken for it.
        server = TvServer(work, streams)
        server.frozen = True
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,' + 'wait:1500,state,' * 20 + 'wait:3500,expect:8,press:B,wait:500,expect:6',
                env=dict(tv, AUDIO='1', **CONSOLE), name='tv-frozen')
        states = [field(x, 'state') for x in run.tv[:20]]
        lost = next(i for i, x in enumerate(states) if x == '5')
        playing = [x for x in s[:lost] if field(x, 'playing') == '1']
        frozen_at = next(int(field(x, ' t')) for x in playing if field(x, 'shown') == field(playing[-1], 'shown'))
        gone_by = int(field(s[lost], ' t'))
        assert int(field(playing[-1], 'shown')) in range(270, 290) and 12000 <= gone_by - frozen_at <= 27000, (playing[-1], frozen_at, gone_by)
        assert field(s[lost], 'msg') == 'No' and all(field(x, 'stepped') == '0' for x in run.tv) and len(server.streams) == 1 and tv_lifecycle(server) == 1
        snd_before = sound_share(run.sound, 2, lost - 1)[0]
        assert snd_before >= 0.99, ('the sound went on while the picture stood', snd_before)
        checks.append(f'TV, pictures stop after 19 s while the sound goes on: the sound plays on ({100 * snd_before:.1f}%), "No signal" about '
                      f'{(gone_by - frozen_at) / 1000:.0f} s after the last picture, closes sent, no step down')

        # 3c. Guard for streams whose HEADER says more than the cap (review B1, M1): 24 / 25 / 29.97 / 30 / 50 / 59.94
        #     pictures a second at the console's figures, with sound: pictures thinned, sound first, one stream.
        results = []
        for fps, show_low, show_high in (('24', 8, 12.5), ('25', 8, 13), ('30000/1001', 6, 15.5), ('30', 6, 15.5),
                                         ('50', 5, 13), ('60000/1001', 5, 12.5)):
            server = TvServer(work, streams)
            server.fps = fps
            s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:2500,state,' + 'wait:3000,state,' * 6 + 'press:B,wait:500,press:B,wait:500,expect:6',
                    env=dict(tv, AUDIO='1', **CONSOLE), name='tv-console-' + fps.split('/')[0])
            first, last = s[0], s[6]
            real = eval(fps)
            assert len(server.streams) == 1 and tv_lifecycle(server) == 1, server.streams
            assert field(last, 'playing') == '1' and field(last, 'bitrate') == '400000' and field(last, 'softened') == '0' \
                and field(last, 'slowed') == '0' and field(last, 'cap') == '15', last
            span = (int(field(last, ' t')) - int(field(first, ' t'))) / 1000
            assert span >= 25, ('at least 25 s of the channel judged', span)
            followed = (int(field(last, 'frame')) - int(field(first, 'frame'))) / span
            assert abs(followed / real - 1) <= 0.02, ('the stream is followed at its own rate: no lag builds up', fps, followed)
            shown = (int(field(last, 'shown')) - int(field(first, 'shown'))) / span
            assert show_low <= shown <= show_high and field(last, 'dropped') == '0', ('pictures shown a second', fps, shown, last)
            share, under = sound_share(run.sound, 0, 6)
            assert share >= 0.99 and under <= 0.5, ('clean sound on the console\'s link', fps, share, under)
            results.append(f'{real:.2f}: followed {followed:.1f}, shown {shown:.1f}, sound {100 * share:.2f}%, {under:.2f} underruns/s')
        checks.append('TV guard for over-rate streams (header above the cap), console figures (100 KB/s link, 44 ms decode, 4 ms send, 10 ms loop, '
                      '4 KB buffer, 2 KB ring), 400 kbit/s, with sound, one stream each, pictures a second: ' + '; '.join(results))
        server = TvServer(work, streams)
        server.fps = '24'
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:2500,state,' + 'wait:3000,state,' * 6 + 'press:B,wait:500,press:B,wait:500,expect:6',
                env=dict(tv, **CONSOLE), name='tv-console-silent')
        span = (int(field(s[6], ' t')) - int(field(s[0], ' t'))) / 1000
        followed = (int(field(s[6], 'frame')) - int(field(s[0], 'frame'))) / span
        assert len(server.streams) == 1 and abs(followed - 24) <= 0.5 and field(s[6], 'dropped') == '0' and field(s[6], 'slowed') == '0', (followed, s[6])
        checks.append(f'TV guard without sound (stock FPGA image), header 24 a second: one stream, {followed:.1f} of 24 pictures a second followed, none late')
        # Review M2: pictures on every second frame number only (empty chunks between): still shown.
        server = TvServer(work, streams)
        server.markers = True
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:2500,state,' + 'wait:3000,state,' * 3 + 'press:B,wait:500,press:B,wait:500,expect:6',
                env=dict(tv, AUDIO='1', **CONSOLE), name='tv-markers')
        span = (int(field(s[3], ' t')) - int(field(s[0], ' t'))) / 1000
        shown = (int(field(s[3], 'shown')) - int(field(s[0], 'shown'))) / span
        share, _ = sound_share(run.sound, 0, 3)
        assert field(s[3], 'fps') == '48' and 7 <= shown <= 12.5 and share >= 0.99 and field(s[3], 'playing') == '1', (shown, share, s[3])
        checks.append(f'TV thinning counts delivered pictures (review M2): a stream with an empty chunk before every picture (header 48 a second) '
                      f'shows {shown:.1f} pictures a second with {100 * share:.1f}% sound')
        # Review B1 and N3: a link that cannot carry the channel: after two starved 5 s windows in a row the channel is
        # closed and tuned again at 250 kbit/s under the reopen rule; once for that channel; the next channel starts at
        # 400 again.
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,' + 'wait:1500,state,' * 22 + 'press:UP,wait:2500,wait:1500,state,press:B,wait:500,press:B,wait:500,expect:6',
                env=dict(tv, AUDIO='1', **dict(CONSOLE, RATE='45000')), name='tv-step-down')
        rates = [int(re.search(r'videoBitRate=(\d+)', p).group(1)) for p in server.streams]
        assert rates[:2] == [400000, 250000] and rates[2:] == [400000], ('one step down for that channel; the next channel at 400 again', rates)
        stepped_at = next(int(field(x, ' t')) for x in s if field(x, 'softened') == '1')
        opened_at = next(int(field(x, ' t')) for x in s if field(x, 'playing') == '1')
        assert stepped_at - opened_at >= 5000, ('not on the first starved window (the first state is read some seconds into the stream)', stepped_at - opened_at)
        assert any(b and b.get('MaxStreamingBitrate') == 250000 for _, b in server.bodies)
        assert tv_lifecycle(server) == 3
        opens = [i for i, e in enumerate(server.log) if e[0] == 'REQ' and 'PlaybackInfo' in e[2]]
        gap = next(int(field(x, ' t')) for x in s if field(x, 'streams') == '2') - next(int(field(x, ' t')) for x in s if field(x, 'softened') == '1')
        after = [i for i, x in enumerate(s[:22]) if field(x, 'streams') == '2' and field(x, 'playing') == '1']
        share, under = sound_share(run.sound, after[1], after[-1])
        assert share >= 0.99, ('clean after the step down', share)
        assert len(opens) == 3
        checks.append(f'TV on a 45 KB/s link: starved at 400 kbit/s for two 5 s windows in a row, the channel is closed '
                      f'and tuned again once at 250 kbit/s under the reopen rule; then {100 * share:.1f}% sound; no second step; the next channel asks 400 again')
        # N3: one bad stretch (the link silent for 4 s, once) is not a slow link: no step down
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,' + 'wait:1500,state,' * 16 + 'press:B,wait:500,press:B,wait:500,expect:6',
                env=dict(tv, AUDIO='1', **CONSOLE, DIP_AT_MS='20000', DIP_MS='4000'), name='tv-one-dip')
        assert len(server.streams) == 1 and all(field(x, 'stepped') == '0' for x in run.tv) and field(s[-1], 'softened') == '0', (server.streams, run.tv[-1])
        # N3: a stream that ends is "No signal", never a step down
        server = TvServer(work, streams)
        server.short = 14
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,' + 'wait:1000,state,' * 7 + 'wait:3500,expect:8,press:B,wait:500,expect:6',
                env=dict(tv, AUDIO='1', **CONSOLE), name='tv-ends')
        assert len(server.streams) == 1 and all(field(x, 'stepped') == '0' for x in run.tv) and any(field(x, 'state') == '5' for x in run.tv) \
            and not any('Adjusting' in x for x in s), (server.streams, run.tv[-1])
        checks.append('TV step down (review N3): a single 4 s dip of the link does not step down; a stream that ends is "No signal", not "Adjusting quality"')

        # 4. Up/Down while watching: what is on closes at once, the target steps with each press, and only the last
        #    target is tuned, 1.5 s after the last press, after the closes; Up is the next channel, Down the one before.
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:4000,state,press:UP,wait:400,state,shot:tv-surf,press:UP,wait:400,press:UP,wait:1000,state,'
                             'wait:700,state,wait:4000,state,press:DOWN,wait:1700,state,wait:3000,state,press:B,wait:500,press:B,wait:600,expect:6',
                env=tv, name='tv-surf')
        t = run.tv
        infos = [e[2].split('/')[2] for e in server.log if e[0] == 'REQ' and e[2].endswith('/PlaybackInfo?userId=' + USER)]
        assert infos == [channel_id(0), channel_id(3), channel_id(2)], infos
        assert field(t[1], 'state') == '1' and field(t[1], 'number') == '3' and field(s[1], 'playing') == '0', (t[1], s[1])
        assert field(t[2], 'state') == '1' and field(t[2], 'number') == '5' and field(s[2], 'streams') == '1', ('still stepping 1 s after the last press', t[2])
        assert field(s[4], 'streams') == '2' and field(s[4], 'playing') == '1' and field(t[4], 'number') == '5', (server.streams, s[4])
        assert field(t[6], 'number') == '4' and field(s[6], 'playing') == '1', t[6]
        assert tv_lifecycle(server) == 3
        checks.append('TV Up/Down while watching: the channel goes off at once, three quick presses open only the last target, 1.5 s after '
                      'the last press and after the closes; Up steps forward, Down back')
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,' + 'press:DOWN,' * 7 + 'press:A,wait:3000,state,press:UP,wait:2500,state,wait:2500,state,'
                             'press:DOWN,wait:2500,state,wait:2500,state,press:B,wait:500,press:B,wait:600,expect:6', env=tv, name='tv-surf-page')
        t = run.tv
        assert field(t[0], 'number') == '8' and field(t[2], 'number') == '9' and field(t[2], 'start') == '7' and field(s[2], 'playing') == '1', (t, s)
        assert field(t[4], 'number') == '8' and field(t[4], 'start') == '0' and field(s[4], 'playing') == '1', (t[4], s[4])
        assert tv_lifecycle(server) == 3
        checks.append('TV Up/Down across a list page: the page is fetched with the stream closed, then the channel is tuned')

        # 5. The same channel is never opened again within 10 s of its close (its conversion takes that long to go).
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:3000,state,press:B,wait:300,press:A,wait:9000,state,shot:tv-tuning,wait:1500,state,'
                             'wait:3000,state,press:B,wait:500,press:B,wait:600,expect:6', env=tv, name='tv-reopen')
        t = run.tv
        assert field(t[1], 'state') == '2' and field(s[1], 'playing') == '0' and field(s[1], 'streams') == '1', ('still waiting 9.3 s after the close', t[1])
        assert field(s[3], 'streams') == '2' and field(s[3], 'playing') == '1', (server.streams, s[3])
        server2 = TvServer(work, streams)
        s2 = run(exe, server2, 'wait:1500,press:DOWN,press:A,wait:3000,press:UP,wait:4000,state,press:DOWN,wait:1700,state,wait:3000,state,wait:6000,state,'
                               'press:B,wait:500,press:B,wait:600,expect:6', env=tv, name='tv-flip')
        assert field(s2[2], 'streams') == '2' and field(s2[2], 'playing') == '0', ('A is not reopened 8.7 s after its close', server2.streams)
        assert field(s2[3], 'streams') == '3' and field(s2[3], 'playing') == '1', s2[3]
        assert tv_lifecycle(server) == 2 and tv_lifecycle(server2) == 3
        checks.append('TV reopen rule: a channel closed less than 10 s ago waits ("Tuning...") until 10 s have passed, also on A -> B -> A')

        # 6. Dead channels: "No signal" for 3 s, then the list; whatever was opened is closed.
        for label, setup, env_more, wait in (
                ('PlaybackInfo refused (500)', lambda sv: sv.dead.add(channel_id(0)), {}, 600),
                ('the stream cannot be opened', lambda sv: sv.no_stream.add(channel_id(0)), {}, 600),
                ('the stream ends at once', lambda sv: sv.empty_stream.add(channel_id(0)), {}, 600),
                ('no picture for 20 s', lambda sv: None, {'GAP_EVERY': '1000000', 'GAP_MS': '999000'}, 20500)):
            server = TvServer(work, streams)
            setup(server)
            s = run(exe, server, f'wait:1500,press:DOWN,press:A,wait:{wait},expect:4,state,shot:tv-no-signal,wait:3200,expect:8,state,'
                                 'press:B,wait:500,expect:6', env=dict(tv, **env_more), name='tv-dead')
            assert field(run.tv[0], 'state') == '5' and field(s[0], 'msg') == 'No' and field(run.tv[1], 'state') == '0', (label, run.tv, s)
            tv_lifecycle(server)
        checks.append('TV dead channel (PlaybackInfo refused / stream refused / stream ends / no picture for 20 s): "No signal" for 3 s, '
                      'then the list; every opened channel closed')
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:31500,press:DOWN,press:A,wait:4000,state,wait:24000,state,wait:3500,expect:8,press:B,wait:500,expect:6',
                env=dict(tv, GAP_EVERY='40000', GAP_MS='30000'), name='tv-stall')  # the network delivers nothing from 40 s to 70 s
        assert field(s[0], 'playing') == '1' and field(run.tv[1], 'state') == '5' and tv_lifecycle(server) == 1, (s, run.tv)
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:5500,state,wait:3200,expect:8,press:B,wait:500,expect:6',
                env=dict(tv, FAIL_AT_MS='5000'), name='tv-lost')
        assert field(run.tv[0], 'state') == '5' and len(server.streams) == 1 and tv_lifecycle(server) == 1, (run.tv, server.streams)
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:3000,link:0,wait:200,state,link:1,wait:3200,expect:8,press:B,wait:500,expect:6',
                env=tv, name='tv-wifi')
        assert field(run.tv[0], 'state') == '5' and 'Wi-Fi' in s[0] and len(server.streams) == 1 and tv_lifecycle(server) == 1, (run.tv, s)
        checks.append('TV: a channel that stalls for 20 s, a lost connection, and Wi-Fi dropping end in "No signal" with one stream and the closes; no reconnect')
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:3000,press:DOWN,press:A,wait:400,press:B,wait:3000,expect:8,state,press:B,wait:2000,expect:6',
                env=dict(tv, DELAY_MS='1000'), name='tv-cancel')
        assert not server.streams and len(server.closed) == 1 and not server.stopped and not server.live, (server.streams, server.closed)
        checks.append('TV: B while the channel is opening: no stream, the live stream PlaybackInfo opened is closed')
        server = TvServer(work, streams)
        server.odd_live = True
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:3000,state,press:B,wait:500,press:B,wait:600,expect:6', env=tv, name='tv-odd-id')
        assert 'liveStreamId=f00d%7Cnative_live%207%2F%C3%A91' in server.streams[0] and server.closed == [ODD_LIVE + '1'], (server.streams, server.closed)
        assert field(s[0], 'playing') == '1' and tv_lifecycle(server) == 1
        checks.append('TV: a live stream id with | space / and a non-ASCII letter is percent-escaped in the stream request and in the close')

        # 6b. Review H2: no live stream is left open.
        #  - a reply larger than the console's buffer (16 media streams with long UTF-8 names): the ids are read as
        #    it arrives, the channel plays
        server = TvServer(work, streams)
        server.tracks = 20
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:3000,state,press:B,wait:500,press:B,wait:600,expect:6', env=tv, name='tv-big-reply')
        assert server.playinfo_bytes > 16000 and field(s[0], 'playing') == '1' and tv_lifecycle(server) == 1, (server.playinfo_bytes, s[0])
        assert f'mediaSourceId=src{channel_id(0)[-6:]}&' in server.streams[0] and 'decoy' not in server.streams[0] and 'second' not in server.streams[0]
        big = server.playinfo_bytes
        #  - a slow PlaybackInfo (12 s): waited for, B answers at once during the wait, then the channel plays
        server = TvServer(work, streams)
        server.info_delay = 12000
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:11000,expect:4,state,wait:3000,state,press:B,wait:500,press:B,wait:600,expect:6',
                env=tv, name='tv-slow-info')
        assert field(s[0], 'playing') == '0' and field(run.tv[0], 'state') == '3' and field(s[1], 'playing') == '1' and tv_lifecycle(server) == 1, (s, run.tv)
        server = TvServer(work, streams)
        server.info_delay = 6500
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:5000,press:B,wait:200,expect:8,state,wait:9000,state,press:B,wait:600,expect:6',
                env=tv, name='tv-slow-info-b')
        assert not server.streams and len(server.closed) == 1 and not server.live, ('B during the wait, the reply 1.5 s later: the list at once; its live stream closed', server.closed)
        #  - review N4: a PlaybackInfo the server sits on (25 s) does not hold the next action: 4 s after nobody waits
        #    for it any more it is given up; the other channel is then opened; the first one is not asked again for 10 s
        server = TvServer(work, streams)
        server.fail_once = {}
        slow_first = {'n': 0}
        plain = server.reply

        def slow_reply(method, path):
            answer = plain(method, path)
            if 'PlaybackInfo' in path:
                slow_first['n'] += 1
                if slow_first['n'] == 1:
                    return answer[0], answer[1], 25000
            return answer
        server.reply = slow_reply
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:500,press:B,wait:200,expect:8,press:DOWN,press:A,wait:2500,state,wait:3000,state,'
                             'press:B,wait:500,press:UP,press:A,wait:3000,state,wait:6000,state,press:B,wait:500,press:B,wait:600,expect:6', env=tv, name='tv-unwanted')
        infos = [e[2].split('/')[2] for e in server.log if e[0] == 'REQ' and 'PlaybackInfo' in e[2]]
        assert field(s[0], 'streams') == '0' and field(s[1], 'streams') == '1' and field(s[1], 'playing') == '1', ('the second channel plays once the first was given up, 4 s after B', s[:2])
        assert infos[:2] == [channel_id(0), channel_id(1)] and field(run.tv[2], 'state') == '2' and infos[2:] == [channel_id(0)], ('the abandoned channel waits out 10 s', infos, run.tv[2])
        assert len(server.live) == 1 and len(server.closed) == 2, ('the abandoned PlaybackInfo is the one stream the console cannot close', server.live, server.closed)
        n4 = 'a PlaybackInfo held 25 s by the server is given up 4 s after B, the next channel opens then, and the first is not asked again for 10 s'
        #  - the reply is lost after 30 s: with nothing received nothing can be closed (the one leak the console
        #    cannot help: the server never named the stream); the channel is not asked again for 10 s. With the ids
        #    received before the connection broke, the live stream is closed.
        server = TvServer(work, streams)
        server.info_lost = 'empty'
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:30500,expect:4,state,shot:tv-no-answer,wait:3200,expect:8,press:A,wait:5000,state,wait:3000,state,'
                             'press:B,wait:500,press:B,wait:31000,expect:6', env=tv, name='tv-lost-info')
        infos = sum(1 for e in server.log if e[0] == 'REQ' and 'PlaybackInfo' in e[2])
        assert 'msg=The server did not answer' in s[0] and field(run.tv[0], 'state') == '5', s[0]
        assert field(run.tv[1], 'state') == '2' and infos == 2, ('the same channel waits out the 10 s before it is asked again', run.tv[1], infos)
        server = TvServer(work, streams)
        server.info_lost = 'partial'
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:30500,expect:4,state,wait:3200,expect:8,press:B,wait:600,expect:6', env=tv, name='tv-cut-info')
        assert not server.streams and len(server.closed) == 1 and not server.live, ('ids read before the reply broke off: the live stream is closed', server.closed)
        #  - a live stream id too long to play with (300 characters): closed at once, "No signal"
        server = TvServer(work, streams)
        server.live_length = 300
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:600,expect:4,state,wait:3200,expect:8,press:B,wait:600,expect:6', env=tv, name='tv-long-id')
        assert not server.streams and len(server.closed) == 1 and len(server.closed[0]) == 300 and not server.live and field(s[0], 'msg') == 'No', (server.closed, s[0])
        #  - the hand-over to Games while a channel plays: the closes go out within the bound
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:3000,state,quiesce,state', env=tv, name='tv-handover')
        assert field(s[0], 'playing') == '1' and run.quiesce and field(run.quiesce[0], 'owes') == '0' and int(field(run.quiesce[0], 'ms')) <= 1500, run.quiesce
        assert tv_lifecycle(server) == 1
        server = TvServer(work, streams)
        server.info_delay = 600
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:200,quiesce,state', env=tv, name='tv-handover-opening')
        assert not server.streams and len(server.closed) == 1 and not server.live and field(run.quiesce[0], 'owes') == '0', (server.closed, run.quiesce)
        checks.append(f'TV never leaves a live stream open (review H2): a {big} B PlaybackInfo reply (20 media streams, long UTF-8 names, decoy ids) '
                      'is read as it arrives and plays; a 12 s reply is waited for and B answers during it; a reply cut off after the ids, '
                      'a 300-character id, B while opening, and the hand-over to Games (playing, or still opening) all close it; only a reply '
                      'of which nothing arrived cannot be closed, and that channel is not asked again for 10 s; review N4: ' + n4)

        # 7. Review M4: the server or its Live TV not answering: the list keeps the weather row and says why.
        server = TvServer(work, streams)
        server.fail['/LiveTv/Channels'] = -1
        s = run(exe, server, 'wait:1500,expect:8,state,shot:tv-list-retrying,wait:20000,expect:8,state,shot:tv-server-gone,press:DOWN,press:A,wait:300,state,'
                             'press:UP,press:A,wait:300,expect:6,state', env=tv, name='tv-gone')
        t = run.tv
        assert field(t[1], 'err') == '1' and field(t[1], 'number') == 'WX' and len(server.requests) >= 7, (t[1], len(server.requests))
        assert field(s[-1], 'exit_to') == '4', ('the weather row is still reachable', s[-1])
        server = TvServer(work, streams)
        server.fail_once['/LiveTv/Channels'] = 500
        s = run(exe, server, 'wait:1500,expect:8,state,press:DOWN,press:A,wait:500,state,press:B,wait:300,expect:6', env=tv, name='tv-retry')
        assert field(run.tv[0], 'err') == '3' and field(run.tv[1], 'err') == '0' and field(run.tv[1], 'count') == '7', run.tv
        server = TvServer(work, streams)
        server.fail['/LiveTv/Channels'] = 401
        s = run(exe, server, 'wait:1500,expect:8,state,shot:tv-sign-in,press:SELECT,wait:300,expect:6,state', env=tv, name='tv-401')
        assert field(run.tv[0], 'err') == '2' and field(s[-1], 'exit_to') == '1' and len(server.requests) == 1, (run.tv[0], s[-1])
        server = TvServer(work, streams)
        server.fail['/Items/'] = -1
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:600,expect:4,state,wait:3200,expect:8,press:B,wait:300,expect:6', env=tv, name='tv-gone-tune')
        assert 'msg=The server did not answer' in s[0] and not server.streams and not server.closed, s[0]
        server = TvServer(work, streams)
        server.fail['/Items/'] = 401
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:600,expect:5,state,press:B,wait:300,expect:6', env=tv, name='tv-401-tune')
        assert 'sign-in' in s[0] and not server.streams, s[0]
        # 1.6.4: Live TV chosen but no tuner or no channels on the server yet: the weather row, and where to add them
        server = TvServer(work, streams)
        server.channel_count = 0
        s = run(exe, server, 'wait:1500,expect:8,state,shot:tv-no-channels,press:A,wait:300,expect:6,state', env=tv, name='tv-empty')
        assert field(run.tv[0], 'err') == '0' and field(run.tv[0], 'number') == 'WX' and field(s[-1], 'exit_to') == '4', (run.tv, s)
        checks.append('TV with Live TV chosen but no channels on the server yet (1.6.4): the Local Weather row and "No Live TV channels yet. '
                      'Add them in Chromatic Manager: Settings > Live TV channels."; A on the weather row opens it')
        checks.append('TV with the server gone, Live TV failing or the sign-in expired (review M4): the list stays, with the Local Weather row, '
                      '"Try again" and the reason (Select: find the server / sign in again); a tune says "The server did not answer"; B always leaves')

        # 8. The built-in Local Weather row: choosing it leaves the app for the launcher's weather screen (after the
        #    closes of what was on); the app comes back to the list, or by Up/Down to the next / last channel.
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:A,wait:300,expect:6,state', env=tv, name='tv-weather')
        assert field(s[0], 'exit_to') == '4' and len(server.requests) == 1, s[0]
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:DOWN,press:A,wait:3000,state,press:DOWN,wait:2500,expect:6,state', env=tv, name='tv-weather-surf')
        assert field(s[0], 'playing') == '1' and field(s[1], 'exit_to') == '4' and tv_lifecycle(server) == 1, ('Down from the first channel: the weather channel, after the closes', s)
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1000,state,wait:3000,state,press:B,wait:500,press:B,wait:600,expect:6', env=dict(tv, TV_RESUME='next'), name='tv-weather-up')
        assert field(run.tv[0], 'state') == '1' and field(run.tv[0], 'number') == '2' and field(s[1], 'playing') == '1' and field(run.tv[1], 'number') == '2', (run.tv, s)
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1000,state,wait:3500,state,press:B,wait:500,press:B,wait:600,expect:6', env=dict(tv, TV_RESUME='prev'), name='tv-weather-down')
        assert field(s[1], 'playing') == '1' and field(run.tv[1], 'number') == '36' and field(run.tv[1], 'start') == '28', (run.tv, s)
        assert tv_lifecycle(server) == 1
        checks.append('TV Local Weather row: A (or stepping onto it) leaves the app for the launcher\'s weather screen once the closes are out, with no '
                      'request for it; coming back by Up tunes the first channel, by Down the last, by B shows the list')

        # 9. ChromaTube: the library found by name, the ordinary browser under it, PlaybackInfo before the stream,
        #    500 kbit/s, one stream from the beginning: no seek, no reopen (review H3).
        tube = {'MODE': 'tube'}
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,expect:2,state,shot:tube-root,press:A,wait:800,expect:2,press:A,wait:800,expect:2,state,shot:tube-videos,'
                             'press:A,wait:800,expect:3,state,shot:tube-details,press:A,wait:6000,expect:4,state,shot:tube-playing,'
                             'press:RIGHT,wait:1500,press:LEFT,wait:1500,press:MENU,wait:500,expect:4,state,press:B,wait:500,expect:3,'
                             'press:B,press:B,press:B,press:B,wait:500,expect:6',
                env=dict(tube, AUDIO='1'), name='tube')
        assert server.requests[0] == ('GET', f'/UserViews?userId={USER}'), server.requests[0]
        assert server.requests[1][1].startswith(f'/Items?userId={USER}&parentId={TUBE}&startIndex=0'), server.requests[1]
        assert not any(p.startswith(('/UserItems/Resume', '/Shows/NextUp', '/Items/Latest')) for _, p in server.requests), 'no Home rows'
        video = TUBE_VIDEOS[0]
        order = [(e[0], (e[2] if e[0] == 'REQ' else e[1]).split('?')[0]) for e in server.log
                 if e[0] == 'STREAM' or 'PlaybackInfo' in e[2] or e[2].startswith('/Sessions')]
        assert order[:3] == [('REQ', f'/Items/{video}/PlaybackInfo'), ('STREAM', f'/Videos/{video}/stream.avi'), ('REQ', '/Sessions/Playing')], order
        assert server.bodies[0][1]['AutoOpenLiveStream'] is True and server.bodies[0][1]['UserId'] == USER and server.bodies[0][1]['MaxStreamingBitrate'] == 500000 \
            and field(run.tv[3], 'thinned') == '0'
        assert re.fullmatch(rf'/Videos/{video}/stream\.avi\?mediaSourceId=src{video[-6:]}&playSessionId=[0-9a-f]{{16}}&deviceId=chromatic-test'
                            r'&videoCodec=mjpeg&audioCodec=adpcm_ima_wav&audioChannels=1&audioSampleRate=16000&maxWidth=160&maxHeight=120'
                            r'&maxFramerate=15&videoBitRate=500000', server.streams[0]), server.streams[0]
        assert server.served == ['good'], ('with PlaybackInfo first the server sends the small picture with sound; one stream only', server.served)
        assert field(s[3], 'playing') == '1' and int(field(s[3], 'shown')) > 50 and field(run.sound[3], 'on') == '1' \
            and int(field(run.sound[3], 'played')) > 16000 * 4, (s[3], run.sound[3])
        assert int(field(s[3], 'pos')) in range(4, 8), ('from the beginning although the server holds a resume point', s[3])
        assert len(server.streams) == 1 and field(s[4], 'playing') == '1' and int(field(s[4], 'pos')) >= int(field(s[3], 'pos')), ('Left, Right and Menu change nothing', s[4])
        assert sum(1 for e in server.log if e[0] == 'REQ' and 'PlaybackInfo' in e[2]) == 1
        assert not server.stopped and not server.closed, 'no live stream for a video: nothing to close'
        assert server.bodies[-1][0] == '/Sessions/Playing/Stopped' and server.bodies[-1][1]['ItemId'] == video, server.bodies[-1]
        wrong = TvServer(work, streams)
        assert wrong.stream_file(server.streams[0]) == streams.wrong, 'the fake reproduces the fault: no PlaybackInfo, full-size pictures, no sound'
        checks.append('ChromaTube: library found by name among the views, channel -> season -> videos in the ordinary browser, Play asks '
                      'PlaybackInfo first at 500,000 bit/s and gets the 160x90 picture with sound (the fake serves 1920x1080 without sound otherwise), '
                      'starts at the beginning, one stream: Left, Right and the Menu button over the video do nothing (review H3); reports as for any video; '
                      'B at the root leaves')
        results = []
        for label, measured_shape, more in (('as measured (15/s, about 21 KB/s, offered 8 times faster than real time)', True,
                                             {'SERVE_BPS': '21000', 'SERVE_BURST_S': '600', 'SERVE_X100': '800'}),
                                            ('at 500 kbit/s honoured', False, {})):
            server = TvServer(work, streams)
            server.measured = measured_shape
            s = run(exe, server, 'wait:1500,press:A,wait:800,press:A,wait:800,press:A,wait:800,press:A,wait:1000,state,' + 'wait:1500,state,' * 8,
                    env=dict(tube, AUDIO='1', **CONSOLE, **more), name='tube-console')
            span = (int(field(s[8], ' t')) - int(field(s[0], ' t'))) / 1000
            share, under = sound_share(run.sound, 0, 8)
            pos = int(field(s[8], 'pos')) - int(field(s[0], 'pos'))
            shown = (int(field(s[8], 'shown')) - int(field(s[0], 'shown'))) / span
            assert len(server.streams) == 1 and share >= 0.99 and abs(pos - span) <= 1.5 and field(s[8], 'softened') == '0' and field(s[8], 'slowed') == '0' \
                and 14.5 <= shown <= 15.2 and field(run.tv[8], 'thinned') == '0', (label, share, pos, span, shown, s[8])
            results.append(f'{label}: {shown:.1f} shown a second, {100 * share:.2f}% sound, {pos} s of video in {span:.1f} s')
        checks.append('ChromaTube at the console\'s figures (100 KB/s link) with sound, one stream, no reopen, nothing thinned: ' + '; '.join(results))
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:A,wait:800,press:A,wait:800,press:A,wait:800,press:A,wait:2500,state,' + 'wait:3000,state,' * 8,
                env=dict(tube, AUDIO='1', **dict(CONSOLE, RATE='40000')), name='tube-slow-link')
        assert len(server.streams) == 1 and field(s[-1], 'playing') == '1' and field(s[-1], 'softened') == '0', ('a slow link plays on in the one stream (slow, no reopen)', s[-1])
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:A,wait:800,press:A,wait:800,press:A,wait:800,press:A,wait:5500,state,wait:1000,expect:5,state',
                env=dict(tube, FAIL_AT_MS='5000'), name='tube-lost')
        assert len(server.streams) == 1 and 'msg=Lost' in s[-1], ('a lost connection stops the video and says so: no reopen at a position', s[-1])
        server = TvServer(work, streams)
        server.tracks = 28
        s = run(exe, server, 'wait:1500,press:A,wait:800,press:A,wait:800,press:A,wait:800,press:A,wait:4000,state', env=tube, name='tube-big-reply')
        assert server.playinfo_bytes > 20000 and field(s[0], 'playing') == '1' and server.served == ['good'], (server.playinfo_bytes, s[0])
        assert server.bodies[0][1]['DeviceProfile']['TranscodingProfiles'][0]['AudioCodec'] == 'mp3' and 'audioCodec=mp3' in server.streams[0], 'review M6'
        checks.append(f'ChromaTube: a slow link plays on in its one stream; a lost connection stops with "Lost the connection" (no reopen); a video '
                      f'whose PlaybackInfo reply is {server.playinfo_bytes} B (28 media streams) plays; without the FPGA\'s sound both requests name mp3')
        server = TvServer(work, streams)
        server.libraries = [base.item(base.ident(0x900 + i), f'Library {i}', 'CollectionFolder') for i in range(7)] + [
            base.item(TUBE, 'ChromaTube', 'CollectionFolder')]
        run(exe, server, 'wait:1500,expect:2,state', env=tube, name='tube-eighth')
        server = TvServer(work, streams)
        server.libraries = [base.item(base.MOVIES, 'Movies', 'CollectionFolder'), base.item(base.ident(0x901), 'chromatube', 'CollectionFolder'),
                            base.item(base.ident(0x902), 'ChromaTube 2', 'CollectionFolder')]
        s = run(exe, server, 'wait:1500,expect:5,state,shot:tube-not-set-up,press:A,wait:800,expect:5,press:B,wait:300,expect:6,state', env=tube,
                name='tube-missing')
        assert 'msg=ChromaTube' in s[0] and len(server.requests) == 2 and field(s[-1], 'exit_to') == '0', (s, server.requests)
        server = TvServer(work, streams)
        server.tube_empty = True
        s = run(exe, server, 'wait:1500,expect:2,state,shot:tube-empty,press:B,wait:300,expect:6,state', env=tube, name='tube-empty')
        assert field(s[-1], 'exit_to') == '0' and not server.streams, s
        checks.append('ChromaTube root lookup: found as the eighth library; without a library of exactly that name: "ChromaTube isn\'t '
                      'set up on your server" with where to set it up (1.6.4), A asks again, B leaves; the library there but empty: "No '
                      'channels in ChromaTube yet" and where to add them (1.6.4), B leaves')
        server = TvServer(work, streams)
        server.fail['/Items/' + video + '/PlaybackInfo'] = 500
        s = run(exe, server, 'wait:1500,press:A,wait:800,press:A,wait:800,press:A,wait:800,press:A,wait:800,expect:5,state,shot:tube-server-down',
                env=tube, name='tube-refused')
        assert 'msg=Your ChromaTube server' in s[0] and not server.streams and not re.search(r'msg=[^ ]*\d', s[0]), s[0]
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:6000,press:A,wait:1500,press:A,wait:1500,press:A,wait:1500,press:A,wait:300,press:B,wait:2500,expect:3,state',
                env=dict(tube, DELAY_MS='1000'), name='tube-cancel')
        assert not server.streams and field(s[0], 'playing') == '0', (server.streams, s[0])
        server = TvServer(work, streams)
        s = run(exe, server, 'wait:1500,press:A,wait:800,press:A,wait:800,press:A,wait:800,press:A,wait:3000,state,quiesce,state', env=tube, name='tube-handover')
        assert field(s[0], 'playing') == '1' and field(s[1], 'playing') == '0' and server.bodies[-1][0] == '/Sessions/Playing/Stopped', (s, server.bodies[-1])
        checks.append('ChromaTube: PlaybackInfo refused (the server could not reach the ChromaTube tool) says "Your ChromaTube server isn\'t '
                      'answering" with "Check that it is running" (1.6.4); B while it is out never starts the stream; the hand-over '
                      'to Games stops the video and reports it')

        # 10. Jellyfin mode is as before: no PlaybackInfo, the same stream path byte for byte, 1 Mbit/s, seek as before.
        stream = work / 'synthetic.avi'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x120:r=24000/1001', '-f', 'lavfi', '-i',
                        'sine=f=440:r=48000', '-t', '40', '-c:v', 'mjpeg', '-q:v', '5', '-pix_fmt', 'yuvj420p', '-c:a', 'libmp3lame',
                        '-ac', '1', str(stream)], check=True)
        server = base.FakeServer(work, stream)
        server.log = []
        s = run(exe, server, 'wait:3000,expect:1,press:DOWN,wait:300,press:A,wait:1500,expect:3,press:A,wait:4000,expect:4,state,press:RIGHT,wait:2000,'
                             'state,quiesce,state,press:B,wait:500,expect:3',
                name='jellyfin-unchanged')
        ident = base.EPISODES[3]
        assert re.fullmatch(rf'/Videos/{ident}/stream\.avi\?mediaSourceId={ident}&playSessionId=[0-9a-f]{{16}}&deviceId=chromatic-test&videoCodec=mjpeg'
                            r'&audioCodec=mp3&audioChannels=1&audioBitRate=32000&maxWidth=160&maxHeight=120&maxFramerate=15&videoBitRate=1000000'
                            r'&startTimeTicks=3000000000', server.streams[0]), server.streams[0]
        assert len(server.streams) == 2 and re.search(r'&startTimeTicks=31[0-9]0000000$', server.streams[1]), ('Jellyfin seeks as before', server.streams)
        assert field(s[2], 'playing') == '1', ('the hand-over hook does nothing in Jellyfin mode', s[2])
        assert not any('PlaybackInfo' in p or 'LiveTv' in p or 'ActiveEncodings' in p or 'LiveStreams' in p for _, p in server.requests)
        assert all(m in ('GET', 'POST') for m, _ in server.requests)
        checks.append('Jellyfin mode unchanged: no PlaybackInfo, no new request kinds, the 1.5.1 stream path byte for byte at 1,000,000 bit/s, '
                      'seek as before, the hand-over hook does nothing')

        # 11. Memory.
        growth = run.size - APP_BYTES_1_5_1
        assert 0 <= growth <= 512, ('the app struct may not grow by more than 0.5 KB', run.size)
        src = (ROOT / 'chromatic/launcher/main/jellyfin_esp32.c').read_text(encoding='utf-8')
        sizes = {name: int(n) for name, n in re.findall(r'(req_path|req_body|stream_path|stream_url|http_url)\[(\d+)\]', src)}
        assert sizes['req_body'] >= 448 and sizes['stream_path'] >= 600 and sizes['stream_url'] >= sizes['stream_path'] + 129, sizes
        checks.append(f'memory: the app is {run.size} bytes, {growth} more than 1.5.1 ({APP_BYTES_1_5_1}); the platform buffers hold a '
                      f'{sizes["req_body"]}-byte request body and a {sizes["stream_path"]}-byte stream path')

        # previews for docs and review
        menu = work / 'preview_menu.exe'
        subprocess.run([GCC, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', 'chromatic/launcher/preview_menu.c', '-o', str(menu)],
                       cwd=ROOT, check=True)
        menus = OUT / 'menu'
        menus.mkdir(exist_ok=True)
        checks.append(subprocess.run([str(menu), str(menus)], check=True, capture_output=True, text=True).stdout.strip()[6:])
        for ppm in menus.glob('*.ppm'):
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(ppm), '-vf', 'scale=640:576:flags=neighbor',
                            str(ppm.with_suffix('.png'))], check=True)
            ppm.unlink()
        if previews:
            PREVIEWS.mkdir(parents=True, exist_ok=True)
            for old in ('tv-getting-weather', 'tv-local-weather-banner'):
                (PREVIEWS / f'{old}.png').unlink(missing_ok=True)
            for png in menus.glob('*.png'):
                shutil.copyfile(png, PREVIEWS / png.name)
            for folder, name, target in (('tv-list', 'tv-list', 'tv-channel-list'), ('tv-list', 'tv-list-page2', 'tv-channel-list-page-2'),
                                         ('tv-names', 'tv-list-names', 'tv-channel-list-names'),
                                         ('tv-tune', 'tv-banner', 'tv-banner-over-picture'), ('tv-tune', 'tv-playing', 'tv-playing'),
                                         ('tv-surf', 'tv-surf', 'tv-stepping-channels'), ('tv-reopen', 'tv-tuning', 'tv-tuning'),
                                         ('tv-dead', 'tv-no-signal', 'tv-no-signal'), ('tv-lost-info', 'tv-no-answer', 'tv-server-did-not-answer'),
                                         ('tv-gone', 'tv-list-retrying', 'tv-list-server-not-answering'),
                                         ('tv-gone', 'tv-server-gone', 'tv-server-gone'), ('tv-401', 'tv-sign-in', 'tv-sign-in-expired'),
                                         ('tube', 'tube-root', 'chromatube-root'), ('tube', 'tube-videos', 'chromatube-videos'),
                                         ('tube', 'tube-details', 'chromatube-details'), ('tube', 'tube-playing', 'chromatube-playing'),
                                         ('tube-missing', 'tube-not-set-up', 'chromatube-not-set-up')):
                shutil.copyfile(OUT / folder / f'{name}.png', PREVIEWS / f'{target}.png')
        # 1.6.4: what an owner sees before (or half way through) setting up Live TV or ChromaTube
        if previews or '--not-set-up-previews' in sys.argv:
            NEW.mkdir(parents=True, exist_ok=True)
            for folder, name, target in (('tv-empty', 'tv-no-channels', 'no-live-tv-channels-yet'),
                                         ('tv-gone', 'tv-list-retrying', 'tv-server-not-answering-retrying'),
                                         ('tv-gone', 'tv-server-gone', 'tv-server-not-answering'),
                                         ('tube-missing', 'tube-not-set-up', 'chromatube-no-library-on-server'),
                                         ('tube-empty', 'tube-empty', 'chromatube-no-channels-yet'),
                                         ('tube-refused', 'tube-server-down', 'chromatube-server-not-answering')):
                shutil.copyfile(OUT / folder / f'{name}.png', NEW / f'not-set-up-{target}.png')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for c in checks:
        print('PASS:', c)
    print(f'Previews in {OUT}' + (f' and {PREVIEWS}' if previews else ' (run with --previews to refresh chromatic/launcher/previews-1.6.0)'))


if __name__ == '__main__':
    os.environ['PATH'] = r'C:\msys64\ucrt64\bin;' + os.environ['PATH']
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
