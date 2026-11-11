# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""One bounded, cancellable PC AV worker per gateway. No socket or hardware IO."""
import math
import queue
import secrets
import struct
import threading
import time

from adaptive_stream import Adaptation, ladder
from av_stream import Reader, Receiver, AUDIO
from stream_transport_probe import PROFILES

READ_HEADER = struct.Struct('<IQBBH')
DATA, WAIT, EOF, FAILED = range(1, 5)
CHUNK = 4096
MAX_PACKET = 32800
QUEUE_PACKETS = 8
STREAM_PROFILES = tuple(p for p in PROFILES if p.video == 'mjpeg' and p.audio == 'adpcm_ima_wav')


class StreamWorker:
    def __init__(self, session, source, *, idle_seconds=30, stall_seconds=60, adapt=None):
        self.session = session
        self.adapt = adapt
        self.queue = queue.Queue(QUEUE_PACKETS)
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.process_lock = threading.Lock()
        self.read_lock = threading.Lock()
        self.proc = None
        self.failed = False
        self.pending = b''
        self.offset = 0
        self.last_reply = None
        self.last_activity = self.last_progress = time.monotonic()
        self.idle_seconds, self.stall_seconds = idle_seconds, stall_seconds
        self.thread = threading.Thread(target=self._run, args=(source,), daemon=True)
        self.watchdog = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()
        self.watchdog.start()

    def _process(self, proc):
        with self.process_lock:
            self.proc = proc
            if proc is not None and self.cancelled.is_set() and proc.poll() is None:
                proc.terminate()

    def _cancel(self):
        self.cancelled.set()
        with self.process_lock:
            proc = self.proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        self.failed = True

    def _watch(self):
        while not self.cancelled.wait(.1) and not self.done.is_set():
            now = time.monotonic()
            if (now - self.last_activity > self.idle_seconds or
                    (self.queue.empty() and now - self.last_progress > self.stall_seconds)):
                self.failed = True
                self._cancel()
                return

    def _run(self, source):
        iterator = None
        try:
            reader, receiver = Reader(), Receiver()
            sequence = 0
            iterator = source(self.cancelled, self._process)
            for data in iterator:
                if self.cancelled.is_set():
                    break
                if not isinstance(data, bytes) or not 0 < len(data) <= MAX_PACKET:
                    raise ValueError('Invalid producer packet')
                decoded = reader.feed(data)
                if (len(decoded) != 1 or decoded[0].session != self.session
                        or decoded[0].sequence != sequence
                        or receiver.accept(decoded[0]) != decoded[0]):
                    raise ValueError('Producer lost a session packet')
                sequence += 1
                self.last_progress = time.monotonic()
                while not self.cancelled.is_set():
                    try:
                        # Audio timestamps measure delivery speed for adaptive quality.
                        self.queue.put((data, decoded[0].pts if decoded[0].kind == AUDIO else None), timeout=.1)
                        break
                    except queue.Full:
                        pass
            if not self.cancelled.is_set():
                reader.finish()
                if not receiver.closed:
                    raise ValueError('Producer omitted END')
        except Exception:
            if not self.cancelled.is_set():
                self.failed = True
        finally:
            if iterator is not None and hasattr(iterator, 'close'):
                try:
                    iterator.close()
                except Exception:
                    self.failed = True
            self.done.set()

    def read(self, offset):
        if type(offset) is not int or not 0 <= offset < 2**64:
            raise ValueError('Invalid stream offset')
        with self.read_lock:
            self.last_activity = time.monotonic()
            # The previous DATA response may be retried after a lost RPC reply.
            if self.last_reply is not None and offset == self.last_reply[0]:
                return self.last_reply[1]
            if offset != self.offset:
                raise ValueError('Read must continue or retry the previous chunk')
            if self.failed or self.cancelled.is_set():
                return READ_HEADER.pack(self.session, offset, FAILED, 0, 0)
            if not self.pending:
                try:
                    self.pending, pts = self.queue.get_nowait()
                except queue.Empty:
                    return READ_HEADER.pack(self.session, offset, EOF if self.done.is_set() else WAIT, 0, 0)
                if pts is not None and self.adapt is not None:
                    self.adapt.delivered(pts)
            data = self.pending[:CHUNK]
            self.pending = self.pending[len(data):]
            response = READ_HEADER.pack(self.session, offset, DATA, 0, len(data)) + data
            self.last_reply = offset, response
            self.offset += len(data)
            return response

    def close(self):
        self._cancel()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=6)
        if self.thread.is_alive():
            raise ValueError('Worker did not stop within its cleanup bound')
        if threading.current_thread() is not self.watchdog:
            self.watchdog.join(timeout=1)


class StreamManager:
    def __init__(self, client, factory=None):
        self.client, self.factory = client, factory
        self.worker = None
        self.level = 0  # adaptive quality carried across seeks and titles

    def start(self, item, request):
        if self.worker is not None:
            raise ValueError('Stop the previous session first')
        profile_key = request.get('profile', 'pocket-link-jpeg-low-30')
        if profile_key == 'chromatic-auto':
            # The console cannot know the title's frame rate; keep its own cadence.
            from stream_transport_probe import chromatic_profile
            rate = next((s.get('RealFrameRate') or s.get('AverageFrameRate')
                         for s in item.get('MediaStreams') or [] if s.get('Type') == 'Video'), None)
            profile_key = chromatic_profile(rate if isinstance(rate, (int, float)) else None).key
        profile = next((p for p in STREAM_PROFILES if p.key == profile_key), None)
        seek = request.get('start_seconds', 0)
        if (profile is None or type(seek) not in (int, float) or not math.isfinite(seek)
                or not 0 <= seek < 172800):
            raise ValueError('Invalid stream settings')
        if item.get('IsFolder') or item.get('Type') not in {'Movie', 'Episode', 'Video', 'MusicVideo'}:
            raise ValueError('Choose a video')
        ticks = item.get('RunTimeTicks') or 0
        if type(ticks) is not int or ticks < 0:
            raise ValueError('Invalid title duration')
        duration = ticks / 10000000 - seek if ticks else None
        if duration is not None and not 0 < duration <= 172800:
            raise ValueError('Seek outside the title')
        session = secrets.randbits(32) or 1
        adapt = None
        if self.factory:
            source = self.factory(profile, session, seek, duration)
        else:
            from live_av import encoded_stream
            # Network streams soften pictures instead of stalling; resume one level
            # better than the last session ended, so a seek does not start at the worst.
            adapt = Adaptation(ladder(profile.quality), start=max(0, self.level - 1))
            source = lambda stop, process: encoded_stream(self.client, item['Id'], profile,
                session, seek, duration, stop, process, adapt=adapt)
        self.worker = StreamWorker(session, source, adapt=adapt)
        return {'adaptive': adapt is not None,'session': session, 'encoding': 'PJAV', 'profile': profile.key,
                'fps': profile.fps, 'width': profile.width, 'height': profile.height,
                'chunk_bytes': CHUNK, 'idle_timeout_seconds': 30}

    def resolve(self, request):
        session = request.get('session')
        if type(session) is not int or self.worker is None or session != self.worker.session:
            raise ValueError('Unknown stream session')
        return self.worker

    def read(self, request):
        return self.resolve(request).read(request.get('offset', 0))

    def stop(self, request):
        worker = self.resolve(request)
        worker.close()
        if worker.adapt is not None:
            self.level = worker.adapt.level
        self.worker = None
        return {'stopped': True}

    def close(self):
        if self.worker is not None:
            self.worker.close()
            self.worker = None
