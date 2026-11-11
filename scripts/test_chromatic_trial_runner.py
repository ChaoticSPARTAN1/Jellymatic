# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
"""Trial runner against a simulated console. Opens no port and needs no hardware.

These tests prove the runner's decisions. They do not prove real flash writes.
"""
import hashlib
import io
import json
import struct
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import chromatic_trial_runner as ctr
from chromatic_trial_runner import (Deadlines, Journal, Refused, Runner, SIZE, StockBackup, check_candidate,
                                    changed_regions, load_stock, main, same_outside_settings, sha, with_settings_from)

IDENTITY = {'chip': 'ESP32-U4WDH', 'revision': 3, 'mac': 'aa:bb:cc:dd:ee:ff', 'flash_id': '204016'}
MARKER = 'PJ-LAUNCHER-READY'
FAST = Deadlines(preflight=5, connect=5, read=5, write=5, boot=1, device_return=1, poll=0, restore_attempts=3)


class PowerLoss(BaseException):
    """Not an Exception: the process is simply gone, so no handler may run."""


def table(entries):
    body = b''.join(b'\xaa\x50' + bytes([kind, sub]) + struct.pack('<II', offset, size) + label.ljust(16, b'\0') + bytes(4)
                    for kind, sub, offset, size, label in entries)
    return body + b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(body).digest()


def app(seed):
    data = bytearray(0xe9 for _ in range(1))
    data += bytes(31)
    desc = bytearray(256)
    struct.pack_into('<I', desc, 0, 0xabcd5432)
    desc[16:22] = b'0.13.8'
    desc[48:54] = b'mcu_fw'
    data += desc
    data += bytes((seed + i * 7) % 251 for i in range(20000))
    return bytes(data)


def stock_image():
    image = bytearray(b'\xff' * SIZE)
    image[0x1000] = 0xe9
    entries = table([(1, 2, 0x9000, 0x6000, b'nvs'), (1, 1, 0xf000, 0x1000, b'phy_init'), (0, 0, 0x10000, 0x100000, b'factory')])
    image[0x8000:0x8000 + len(entries)] = entries
    image[0x9000:0x9100] = b'S' * 256
    code = app(3)
    image[0x10000:0x10000 + len(code)] = code
    return bytes(image)


def candidate_image(stock):
    image = bytearray(b'\xff' * SIZE)
    image[0x1000] = 0xe9
    entries = table([(1, 2, 0x9000, 0x6000, b'nvs'), (1, 1, 0xf000, 0x1000, b'phy_init'), (0, 0, 0x10000, 0x100000, b'launcher'),
                     (0, 0x10, 0x110000, 0x100000, b'stock')])
    image[0x8000:0x8000 + len(entries)] = entries
    code = app(9)
    image[0x10000:0x10000 + len(code)] = code
    original = stock[0x10000:0x110000].rstrip(b'\xff')
    image[0x110000:0x110000 + len(original)] = original
    return bytes(image)


class Console:
    """State that survives a crash of the tool, as a real console's flash would."""

    def __init__(self, image):
        self.flash = bytearray(image)
        self.identity = dict(IDENTITY)
        self.present = True
        self.booted_marker = True
        self.writes = []
        self.fail = {}      # operation -> Exception, raised once per remaining count
        self.count = {}
        self.hang = set()
        self.die_mid_write = False
        self.released = threading.Event()


class FakeProgrammer:
    def __init__(self, console):
        self.console = console
        self.open = False

    def trip(self, operation):
        console = self.console
        if operation in console.hang:
            console.hang.discard(operation)
            console.released.wait(10)
            raise OSError('port closed')
        if console.count.get(operation, 0) > 0:
            console.count[operation] -= 1
            raise console.fail[operation]

    def device_present(self):
        return self.console.present

    def connect(self):
        self.trip('connect')
        if not self.console.present:
            raise OSError('no device')
        self.open = True

    def identity(self):
        return dict(self.console.identity)

    def read_all(self):
        self.trip('read')
        if not self.open or not self.console.present:
            raise OSError('lost')
        return bytes(self.console.flash)

    def write(self, offset, data):
        self.trip('write')
        if not self.open or not self.console.present:
            raise OSError('lost')
        self.console.writes.append((offset, len(data)))
        if self.console.die_mid_write:
            self.console.die_mid_write = False
            half = len(data) // 2
            self.console.flash[offset:offset + half] = data[:half]
            raise PowerLoss()
        self.console.flash[offset:offset + len(data)] = data

    def boot(self, marker, seconds):
        self.trip('boot')
        self.open = False
        return bool(marker) and self.console.booted_marker

    def abort(self):
        self.open = False
        self.console.released.set()


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name) / 'work'
        self.stock_bytes = stock_image()
        self.stock = StockBackup(self.stock_bytes, dict(IDENTITY), sha(self.stock_bytes))
        self.candidate = candidate_image(self.stock_bytes)
        self.digest = sha(self.candidate)
        self.console = Console(self.stock_bytes)
        self.factories = 0
        self.output = []

    def factory(self):
        self.factories += 1
        return FakeProgrammer(self.console)

    def runner(self, gate=lambda: None, journal_class=Journal, stock=None):
        load = stock or (lambda: self.stock)
        return Runner(self.work, load, self.factory, gate, FAST, journal_class, sleep=lambda s: None,
                      say=self.output.append)

    def trial(self, **kwargs):
        return self.runner(**kwargs).trial(self.candidate, self.digest, MARKER)

    def assert_stock(self):
        self.assertEqual(bytes(self.console.flash), self.stock_bytes)

    def assert_closed(self):
        self.assertFalse((self.work / 'journal.jsonl').exists())
        self.assertTrue(list((self.work / 'closed').glob('*/journal.jsonl')))


class HappyPath(Base):
    def test_trial_writes_candidate_then_ends_on_stock(self):
        self.assertEqual(self.trial(), ctr.EXIT_OK)
        self.assertTrue(self.console.writes)
        self.assert_stock()
        self.assert_closed()

    def test_candidate_was_really_on_flash_during_the_boot(self):
        seen = {}
        real_boot = FakeProgrammer.boot

        def spy(programmer, marker, seconds):
            if marker:
                seen['flash'] = bytes(programmer.console.flash)
            return real_boot(programmer, marker, seconds)

        FakeProgrammer.boot = spy
        self.addCleanup(setattr, FakeProgrammer, 'boot', real_boot)
        self.trial()
        self.assertEqual(seen['flash'], with_settings_from(self.candidate, self.stock_bytes))

    def test_settings_the_owner_changed_since_the_backup_are_kept(self):
        self.console.flash[0x9000:0x9004] = b'MINE'
        self.assertEqual(self.trial(), ctr.EXIT_OK)
        self.assertEqual(bytes(self.console.flash[0x9000:0x9004]), b'MINE')
        self.assertTrue(same_outside_settings(bytes(self.console.flash), self.stock_bytes))

    def test_restore_rewrites_only_changed_sectors(self):
        self.trial()
        total = sum(size for _, size in self.console.writes)
        self.assertLess(total, SIZE // 2)
        current = bytearray(self.stock_bytes)
        current[0x20000] ^= 1
        self.assertEqual([(o, len(d)) for o, d in changed_regions(bytes(current), self.stock_bytes)], [(0x20000, 4096)])


class Refusals(Base):
    def assert_untouched(self):
        self.assertEqual(self.console.writes, [])
        self.assert_stock()

    def test_mismatched_candidate_hash(self):
        result = self.runner().trial(self.candidate, '0' * 64, MARKER)
        self.assertEqual(result, ctr.EXIT_REFUSED)
        self.assert_untouched()

    def test_wrong_unit(self):
        self.console.identity['mac'] = '11:22:33:44:55:66'
        self.assertEqual(self.trial(), ctr.EXIT_REFUSED)
        self.assert_untouched()

    def test_bad_backup(self):
        def broken():
            raise Refused('bad')
        self.assertEqual(self.trial(stock=broken), ctr.EXIT_REFUSED)
        self.assert_untouched()
        self.assertEqual(self.factories, 0)

    def test_failed_preflight_opens_no_device(self):
        def gate():
            raise ValueError('usage')
        self.assertEqual(self.trial(gate=gate), ctr.EXIT_REFUSED)
        self.assertEqual(self.factories, 0)

    def test_console_not_on_stock(self):
        self.console.flash[0x20000] ^= 0xff
        self.assertEqual(self.trial(), ctr.EXIT_REFUSED)
        self.assertEqual(self.console.writes, [])

    def test_candidate_without_stock_firmware(self):
        image = bytearray(self.candidate)
        image[0x110000:0x120000] = b'\xff' * 0x10000
        with self.assertRaises(Refused):
            check_candidate(bytes(image), sha(bytes(image)), self.stock)

    def test_candidate_that_moves_settings(self):
        image = bytearray(self.candidate)
        entries = table([(1, 2, 0xa000, 0x5000, b'nvs'), (1, 1, 0xf000, 0x1000, b'phy_init'), (0, 0, 0x110000, 0x100000, b'stock')])
        image[0x8000:0x8c00] = b'\xff' * 0xc00
        image[0x8000:0x8000 + len(entries)] = entries
        with self.assertRaises(Refused):
            check_candidate(bytes(image), sha(bytes(image)), self.stock)

    def test_fpga_image_refused(self):
        image = bytearray(self.candidate)
        image[0:4] = b'\xa5\xc3\x06\x00'
        image[6:10] = (0x1281b).to_bytes(4, 'big')
        with self.assertRaisesRegex(Refused, 'FPGA'):
            check_candidate(bytes(image), sha(bytes(image)), self.stock)

    def test_fpga_target_refused(self):
        runner = self.runner()
        self.assertEqual(main(['--target', 'fpga', '--execute'], runner), ctr.EXIT_REFUSED)
        self.assertEqual(self.factories, 0)

    def test_no_fpga_write_path_exists(self):
        self.assertFalse([name for name in dir(ctr.RealProgrammer) if 'fpga' in name.lower()])

    def test_marker_is_required(self):
        with self.assertRaises(Refused):
            self.runner().trial(self.candidate, self.digest, None)

    def test_execute_needs_exact_hash(self):
        path = self.work.parent / 'candidate.bin'
        path.write_bytes(self.candidate)
        self.assertEqual(main(['--execute', '--candidate', str(path), '--boot-marker', MARKER], self.runner()), ctr.EXIT_REFUSED)
        self.assertEqual(self.factories, 0)

    def test_unfinished_trial_blocks_a_new_one(self):
        self.work.mkdir()
        Journal(self.work).append('write', 'begin')
        with self.assertRaises(Refused):
            self.runner().trial(self.candidate, self.digest, MARKER)


class DryRun(Base):
    def test_default_touches_nothing(self):
        path = self.work.parent / 'candidate.bin'
        path.write_bytes(self.candidate)
        result = main(['--candidate', str(path), '--candidate-sha256', self.digest, '--boot-marker', MARKER], self.runner())
        self.assertEqual(result, ctr.EXIT_OK)
        self.assertEqual(self.factories, 0)
        self.assertEqual(self.console.writes, [])
        self.assertFalse((self.work / 'journal.jsonl').exists())

    def test_no_arguments_touches_nothing(self):
        self.assertEqual(main([], self.runner()), ctr.EXIT_OK)
        self.assertEqual(self.factories, 0)

    def test_dry_run_reports_a_bad_candidate(self):
        self.assertEqual(self.runner().dry_run(self.candidate, '1' * 64), ctr.EXIT_REFUSED)


class Failures(Base):
    def test_failure_at_each_operation_ends_on_stock(self):
        for operation, skip in (('write', 0), ('read', 2), ('boot', 0)):
            with self.subTest(operation=operation):
                self.setUp()
                programmer = FakeProgrammer
                original = programmer.trip
                state = {'seen': 0}

                def trip(this, name, operation=operation, skip=skip, state=state, original=original):
                    if name == operation:
                        state['seen'] += 1
                        if state['seen'] == skip + 1:
                            raise OSError('injected')
                    return original(this, name)

                programmer.trip = trip
                try:
                    self.assertEqual(self.trial(), ctr.EXIT_RECOVERED)
                finally:
                    programmer.trip = original
                self.assert_stock()
                self.assert_closed()

    def test_connect_failure_before_writes_changes_nothing(self):
        self.console.fail['connect'] = OSError('x')
        self.console.count['connect'] = 1
        self.assertEqual(self.trial(), ctr.EXIT_REFUSED)
        self.assertEqual(self.console.writes, [])
        self.assert_closed()

    def test_verify_mismatch_goes_to_restore(self):
        real = FakeProgrammer.write

        def corrupt(this, offset, data):
            real(this, offset, data)
            if len(this.console.writes) == 1:
                this.console.flash[offset] ^= 1

        FakeProgrammer.write = corrupt
        self.addCleanup(setattr, FakeProgrammer, 'write', real)
        self.assertEqual(self.trial(), ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_candidate_that_never_boots_is_removed(self):
        self.console.booted_marker = False
        self.assertEqual(self.trial(), ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_a_hung_port_hits_its_deadline(self):
        self.console.hang.add('write')
        result = Runner(self.work, lambda: self.stock, self.factory, lambda: None,
                        Deadlines(preflight=5, connect=5, read=5, write=0.2, boot=1, device_return=1, poll=0),
                        sleep=lambda s: None, say=self.output.append).trial(self.candidate, self.digest, MARKER)
        self.assertEqual(result, ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_device_disappears_then_returns(self):
        real = FakeProgrammer.boot

        def vanish(this, marker, seconds):
            if marker:
                this.console.present = False
                this.console.count['boot'] = 0
                threading.Timer(0.05, lambda: setattr(this.console, 'present', True)).start()
                raise OSError('gone')
            return real(this, marker, seconds)

        FakeProgrammer.boot = vanish
        self.addCleanup(setattr, FakeProgrammer, 'boot', real)
        self.assertEqual(self.trial(), ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_device_gone_for_good_gives_manual_steps(self):
        real = FakeProgrammer.boot

        def vanish(this, marker, seconds):
            this.console.present = False
            raise OSError('gone')

        FakeProgrammer.boot = vanish
        self.addCleanup(setattr, FakeProgrammer, 'boot', real)
        self.assertEqual(self.trial(), ctr.EXIT_MANUAL)
        self.assertTrue(any('STOP' in line for line in self.output))
        self.assertTrue((self.work / 'journal.jsonl').exists())
        self.console.present = True
        FakeProgrammer.boot = real
        self.assertEqual(main([], self.runner()), ctr.EXIT_RECOVERED)
        self.assert_stock()
        self.assert_closed()

    def test_restore_write_failures_stop_after_bounded_attempts(self):
        real = FakeProgrammer.write
        calls = {'n': 0}

        def fail_after_first(this, offset, data):
            calls['n'] += 1
            if len(this.console.writes) >= 1 and calls['n'] > len(changed_regions(self.stock_bytes, with_settings_from(self.candidate, self.stock_bytes))):
                raise OSError('flash error')
            real(this, offset, data)

        FakeProgrammer.write = fail_after_first
        self.addCleanup(setattr, FakeProgrammer, 'write', real)
        real_boot = FakeProgrammer.boot
        self.assertEqual(self.trial(), ctr.EXIT_MANUAL)
        self.assertIs(FakeProgrammer.boot, real_boot)
        journal = Journal(self.work).load()
        self.assertEqual(sum(r['event'] == 'attempt' for r in journal.records), FAST.restore_attempts)
        self.assertEqual(journal.records[-1]['event'], 'manual-required')

    def test_wrong_console_at_resume_gets_no_write(self):
        self.work.mkdir()
        journal = Journal(self.work)
        journal.append('write', 'begin')
        self.console.flash[0x20000] ^= 1
        self.console.identity['mac'] = '11:22:33:44:55:66'
        self.assertEqual(main([], self.runner()), ctr.EXIT_MANUAL)
        self.assertEqual(self.console.writes, [])

    def test_ctrl_c_still_restores(self):
        self.console.fail['boot'] = KeyboardInterrupt()
        self.console.count['boot'] = 1
        self.assertEqual(self.trial(), ctr.EXIT_RECOVERED)
        self.assert_stock()


class CountingJournal(Journal):
    kill_at = None
    mode = 'before'
    total = 0

    def append(self, phase, event, **data):
        cls = CountingJournal
        cls.total += 1
        if cls.total == cls.kill_at and cls.mode == 'before':
            raise PowerLoss()
        if cls.total == cls.kill_at and cls.mode == 'torn':
            self.folder.mkdir(parents=True, exist_ok=True)
            with self.path.open('ab') as file:
                file.write(b'{"phase":"wri')
            raise PowerLoss()
        super().append(phase, event, **data)
        if cls.total == cls.kill_at and cls.mode == 'after':
            raise PowerLoss()


class PowerCuts(Base):
    def run_until_cut(self, kill_at, mode):
        CountingJournal.kill_at, CountingJournal.mode, CountingJournal.total = kill_at, mode, 0
        try:
            self.trial(journal_class=CountingJournal)
            return False
        except PowerLoss:
            return True
        finally:
            CountingJournal.kill_at = None

    def test_every_journal_step_recovers(self):
        CountingJournal.total = 0
        self.trial(journal_class=CountingJournal)
        steps = CountingJournal.total
        self.assertGreater(steps, 15)
        for mode in ('before', 'after', 'torn'):
            for kill_at in range(1, steps + 1):
                with self.subTest(mode=mode, kill_at=kill_at):
                    self.setUp()
                    self.assertTrue(self.run_until_cut(kill_at, mode))
                    if not (self.work / 'journal.jsonl').exists():
                        self.assert_stock()  # died after closing, or before starting
                        continue
                    result = main([], self.runner())
                    self.assertIn(result, (ctr.EXIT_OK, ctr.EXIT_REFUSED, ctr.EXIT_RECOVERED), result)
                    self.assert_stock()
                    self.assertFalse((self.work / 'journal.jsonl').exists())

    def test_power_cut_halfway_through_a_flash_write(self):
        self.console.die_mid_write = True
        with self.assertRaises(PowerLoss):
            self.trial()
        self.assertNotEqual(bytes(self.console.flash), self.stock_bytes)
        self.assertEqual(main([], self.runner()), ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_power_cut_twice(self):
        self.console.die_mid_write = True
        with self.assertRaises(PowerLoss):
            self.trial()
        self.console.die_mid_write = True
        with self.assertRaises(PowerLoss):
            main([], self.runner())
        self.assertEqual(main([], self.runner()), ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_settings_baseline_survives_a_crash(self):
        self.console.flash[0x9000:0x9004] = b'MINE'
        self.console.die_mid_write = True
        with self.assertRaises(PowerLoss):
            self.trial()
        self.assertEqual(main([], self.runner()), ctr.EXIT_RECOVERED)
        self.assertEqual(bytes(self.console.flash[0x9000:0x9004]), b'MINE')

    def test_damaged_journal_assumes_writes_happened(self):
        self.console.die_mid_write = True
        with self.assertRaises(PowerLoss):
            self.trial()
        path = self.work / 'journal.jsonl'
        lines = path.read_bytes().split(b'\n')
        lines[1] = b'garbage'
        path.write_bytes(b'\n'.join(lines))
        self.assertTrue(Journal(self.work).load().writes_started)
        self.assertEqual(main([], self.runner()), ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_damaged_baseline_falls_back_to_the_backup(self):
        self.console.die_mid_write = True
        with self.assertRaises(PowerLoss):
            self.trial()
        baseline = next(self.work.glob('baseline-*.bin'))
        baseline.write_bytes(b'x' * SIZE)
        self.assertEqual(main([], self.runner()), ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_resume_does_not_need_the_usage_gate(self):
        self.console.die_mid_write = True
        with self.assertRaises(PowerLoss):
            self.trial()

        def closed_gate():
            raise ValueError('usage reserve reached')
        self.assertEqual(main([], self.runner(gate=closed_gate)), ctr.EXIT_RECOVERED)
        self.assert_stock()

    def test_status_reports_without_a_device(self):
        self.work.mkdir()
        Journal(self.work).append('write', 'begin')
        runner = self.runner()
        self.assertEqual(main(['--status'], runner), ctr.EXIT_OK)
        self.assertEqual(self.factories, 0)
        self.assertIn('unfinished', self.output[-1])


class RealBackupLoading(Base):
    def bundle(self):
        primary = self.work.parent / 'primary'
        secondary = self.work.parent / 'secondary'
        for folder in (primary, secondary):
            folder.mkdir()
            (folder / 'chromatic-x-esp32_flash_4MB.bin').write_bytes(self.stock_bytes)
        (primary / 'chromatic-x-backup.json').write_text(json.dumps({'esp': dict(IDENTITY, sha256=self.stock.sha256)}))
        return primary, secondary

    def test_good_copies_load(self):
        self.assertEqual(load_stock(*self.bundle()).sha256, self.stock.sha256)

    def test_damaged_primary_refused(self):
        primary, secondary = self.bundle()
        (primary / 'chromatic-x-esp32_flash_4MB.bin').write_bytes(b'x' + self.stock_bytes[1:])
        with self.assertRaises(Refused):
            load_stock(primary, secondary)

    def test_differing_copies_refused(self):
        primary, secondary = self.bundle()
        (secondary / 'chromatic-x-esp32_flash_4MB.bin').write_bytes(b'x' + self.stock_bytes[1:])
        with self.assertRaises(Refused):
            load_stock(primary, secondary)


class MockEsp:
    """Records the esptool calls the real class makes. No port is involved."""
    FLASH_WRITE_SIZE = 0x4000

    def __init__(self, flash):
        self.flash, self.calls, self.begin = flash, [], None
        self.corrupt = False

    def flash_begin(self, size, offset):
        self.calls.append('begin')
        self.begin = offset
        return (size + self.FLASH_WRITE_SIZE - 1) // self.FLASH_WRITE_SIZE

    def flash_block(self, block, number, timeout):
        self.calls.append('block')
        assert len(block) == self.FLASH_WRITE_SIZE
        at = self.begin + number * self.FLASH_WRITE_SIZE
        self.flash[at:at + len(block)] = block

    def read_reg(self, address, timeout):
        self.calls.append('settle')
        return 0

    def flash_md5sum(self, offset, size):
        data = bytes(self.flash[offset:offset + size])
        return hashlib.md5(b'x' + data if self.corrupt else data).hexdigest()

    def read_flash(self, offset, size):
        return bytes(self.flash[offset:offset + size])


class RealProgrammerCalls(unittest.TestCase):
    def setUp(self):
        import sys
        import types
        loader = types.ModuleType('esptool.loader')
        loader.ESPLoader = types.SimpleNamespace(CHIP_DETECT_MAGIC_REG_ADDR=0x40001000)
        package = types.ModuleType('esptool')
        package.loader = loader
        saved = {name: sys.modules.get(name) for name in ('esptool', 'esptool.loader')}
        sys.modules.update({'esptool': package, 'esptool.loader': loader})

        def restore():
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.addCleanup(restore)
        self.programmer = ctr.RealProgrammer()
        self.programmer.esp = self.esp = MockEsp(bytearray(b'\xff' * SIZE))

    def test_write_waits_for_the_last_block_and_checks_it(self):
        data = bytes(range(256)) * 16 * 5  # 20 KiB, so the last block is padded
        self.programmer.write(0x20000, data)
        self.assertEqual(self.esp.calls, ['begin', 'block', 'block', 'settle'])
        self.assertEqual(bytes(self.esp.flash[0x20000:0x20000 + len(data)]), data)

    def test_write_with_a_bad_on_chip_checksum_fails(self):
        self.esp.corrupt = True
        with self.assertRaises(ctr.StepFailed):
            self.programmer.write(0x20000, bytes(4096))

    def test_read_checks_every_chunk(self):
        self.assertEqual(len(self.programmer.read_all()), SIZE)
        self.esp.corrupt = True
        with self.assertRaises(ctr.StepFailed):
            self.programmer.read_all()


class Privacy(Base):
    def test_output_and_journal_never_hold_identity_or_settings(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(self.trial(), ctr.EXIT_OK)
        text = ' '.join(self.output) + stream.getvalue()
        for journal in self.work.glob('closed/*/journal.jsonl'):
            text += journal.read_text(encoding='ascii')
        self.assertNotIn(IDENTITY['mac'], text)
        self.assertNotIn('SSSS', text)


if __name__ == '__main__':
    unittest.main(verbosity=1)
