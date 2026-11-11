# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""The gateway's chromatic-auto request picks the Chromatic profile that keeps each
title's own cadence (24 fps or more). Synthetic items; no server, no hardware."""
from stream_transport_probe import PROFILES, chromatic_profile
from stream_worker import StreamManager


def check(condition, message):
    if not condition:
        raise SystemExit(f'FAIL: {message}')


def main():
    for profile in PROFILES:
        if profile.key.startswith('chromatic-wifi-'):
            check(profile.fps >= 24 and (profile.width, profile.height) == (160, 144), f'{profile.key} shape')
    expected = {24000 / 1001: 'chromatic-wifi-film-23976', 24.0: 'chromatic-wifi-film-24', 25.0: 'chromatic-wifi-pal-25',
                30000 / 1001: 'chromatic-wifi-video-2997', 30.0: 'chromatic-wifi-video-30',
                50.0: 'chromatic-wifi-pal-25', 59.94: 'chromatic-wifi-video-2997', None: 'chromatic-wifi-film-23976'}
    for rate, key in expected.items():
        check(chromatic_profile(rate).key == key, f'{rate} -> {key}')
    for rate, key in ((23.976, 'chromatic-wifi-film-23976'), (29.97, 'chromatic-wifi-video-2997'), (None, 'chromatic-wifi-film-23976')):
        manager = StreamManager(client=None, factory=lambda profile, session, seek, duration: (lambda stop, process: iter(())))
        streams = [{'Type': 'Audio'}, {'Type': 'Video', 'RealFrameRate': rate}] if rate else []
        reply = manager.start({'Id': 'x', 'Type': 'Episode', 'RunTimeTicks': 600000000, 'MediaStreams': streams},
                              {'profile': 'chromatic-auto'})
        check(reply['profile'] == key and reply['width'] == 160 and reply['fps'] >= 24, f'auto {rate}')
        manager.close()
    print('PASS: Chromatic profiles are 160x144 at 24 fps or more, and chromatic-auto keeps each title\'s cadence.')


if __name__ == '__main__':
    main()
