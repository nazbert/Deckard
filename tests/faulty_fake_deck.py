"""
FaultyFakeDeck: a FakeDeck subclass for the single-writer migration harness
(docs/presenter-migration-plan.md, M0).

Records every device write into an ordered journal -- [(t, seq, op, slot,
bytes_hash, thread_name), ...] -- and can scriptably inject TransportErrors,
per-write latency, and input events (fired the way the real reader thread
would call into DeckController's key/dial/touchscreen callbacks).

Import order matters: this module imports FakeDeck, which imports `globals`
at module scope and reads `gl.settings_manager` during __init__. Always
import `fixtures` first in a scenario/test script so the isolated temp data
dir + stub/real settings manager are in place before a FaultyFakeDeck (or any
FakeDeck) is constructed.
"""
import hashlib
import itertools
import os
import sys
import threading
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from StreamDeck.Transport.Transport import TransportError

from src.backend.DeckManagement.Subclasses.FakeDeck import FakeDeck


def _hash_bytes(data) -> str:
    if data is None:
        return "none"
    if isinstance(data, (bytes, bytearray, memoryview)):
        return hashlib.sha1(bytes(data)).hexdigest()[:12]
    # Non-bytes payloads (brightness percent, key-color tuple, ...): hash a
    # stable repr so the journal still has a comparable fingerprint.
    return hashlib.sha1(repr(data).encode()).hexdigest()[:12]


class FaultyFakeDeck(FakeDeck):
    """FakeDeck + write journal + scriptable fault injection.

    Journal entries are 6-tuples: (t, seq, op, slot, bytes_hash, thread_name).
    `seq` comes from a per-instance monotonic counter (one counter per deck
    instance, so two-deck scenarios get independent sequences).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._journal_lock = threading.Lock()
        self._journal: list[tuple] = []
        self._seq_counter = itertools.count(1)

        # op-name-substring -> remaining-failures-to-inject. Matched inside
        # _maybe_fail() so `fail_next("set_key_image", 3)` fails only key
        # writes, not touchscreen/brightness writes.
        self._fail_lock = threading.Lock()
        self._fail_schedule: dict[str, int] = {}

        self._write_latency: float = 0.0

        # ---- Lifecycle state (issue #59) --------------------------------- #
        # The stock FakeDeck's is_open()/connected() are hard-wired True, so a
        # post-close write silently succeeds and an unplug is inexpressible.
        # Model it explicitly:
        #   _open       -- False after close() (device handle released).
        #   _connected  -- False after simulate_unplug() (USB gone). Unplug
        #                  implies not-open too, matching a yanked cable.
        # These are only *enforced* (writes made to raise) when strict
        # lifecycle is on. Strict is the default so new scenarios get the real
        # semantics; a scenario that legitimately needs the old lenient
        # behaviour opts out with set_strict_lifecycle(False).
        self._lifecycle_lock = threading.Lock()
        self._open = True
        self._connected = True
        self._strict_lifecycle = True

        # Registered by DeckController.__init__ via BetterDeck.set_*_callback;
        # stored here so fire_*_event() can invoke them like the real reader
        # thread would.
        self._key_callback = None
        self._dial_callback = None
        self._touchscreen_callback = None

    # ---------------------------------------------------------------- #
    # Callback registration (BetterDeck.set_key_callback wraps this with a
    # physical->logical remapper; that's exercised for free by going through
    # BetterDeck like a real controller does).
    # ---------------------------------------------------------------- #
    def set_key_callback(self, callback):
        self._key_callback = callback

    def set_dial_callback(self, callback):
        self._dial_callback = callback

    def set_touchscreen_callback(self, callback):
        self._touchscreen_callback = callback

    # ---------------------------------------------------------------- #
    # Fault injection
    # ---------------------------------------------------------------- #
    def fail_next(self, op_pattern: str, count: int = 1) -> None:
        """Schedule the next `count` writes whose op name contains
        `op_pattern` to raise StreamDeck.Transport.Transport.TransportError
        instead of writing/journaling."""
        with self._fail_lock:
            self._fail_schedule[op_pattern] = self._fail_schedule.get(op_pattern, 0) + count

    def clear_failures(self) -> None:
        """Cancels every scheduled injected failure (fail_next is additive;
        this is the only way to reset the schedule)."""
        with self._fail_lock:
            self._fail_schedule.clear()

    def set_write_latency(self, seconds: float) -> None:
        """Every subsequent write sleeps `seconds` before it lands (and before
        it's journaled), simulating a slow USB transfer."""
        self._write_latency = seconds

    def _maybe_fail(self, op: str) -> None:
        with self._fail_lock:
            for pattern, remaining in list(self._fail_schedule.items()):
                if remaining > 0 and pattern in op:
                    self._fail_schedule[pattern] = remaining - 1
                    raise TransportError(f"FaultyFakeDeck: injected failure for {op}")

    # ---------------------------------------------------------------- #
    # Lifecycle (issue #59): closed/unplugged states
    # ---------------------------------------------------------------- #
    def set_strict_lifecycle(self, strict: bool) -> None:
        """When True (the default), writes made after close()/simulate_unplug()
        raise TransportError -- the real transport's behaviour once the handle
        is gone. When False, the old lenient behaviour (post-close writes
        silently journal) is restored for scenarios that legitimately drive
        writes past a close()."""
        with self._lifecycle_lock:
            self._strict_lifecycle = strict

    def simulate_unplug(self) -> None:
        """Model a yanked USB cable: connected() flips False and every
        subsequent write fails (strict mode). Unplug implies the handle is
        no longer usable, so is_open() reads False too."""
        with self._lifecycle_lock:
            self._connected = False
            self._open = False

    def _lifecycle_reject(self, op: str) -> None:
        """Raise TransportError if a write is attempted after the device was
        closed or unplugged (strict mode only). `close` itself is exempt so a
        double-close / fallback-close is a safe no-op, matching the real
        Transport.close() being idempotent."""
        if op == "close":
            return
        with self._lifecycle_lock:
            if not self._strict_lifecycle:
                return
            if not self._connected:
                raise TransportError(
                    f"FaultyFakeDeck: {op} on an unplugged device")
            if not self._open:
                raise TransportError(
                    f"FaultyFakeDeck: {op} on a closed device")

    # ---------------------------------------------------------------- #
    # Journal
    # ---------------------------------------------------------------- #
    def _record(self, op: str, slot, data) -> None:
        entry = (
            time.time(),
            next(self._seq_counter),
            op,
            slot,
            _hash_bytes(data),
            threading.current_thread().name,
        )
        with self._journal_lock:
            self._journal.append(entry)

    def _do_write(self, op: str, slot, data) -> None:
        # Fail (or sleep) BEFORE journaling: a failed write must not appear as
        # a landed entry. Lifecycle rejection (closed/unplugged) is checked
        # first: it's the most fundamental "this write cannot land" reason.
        self._lifecycle_reject(op)
        self._maybe_fail(op)
        if self._write_latency:
            time.sleep(self._write_latency)
        self._record(op, slot, data)

    def journal(self) -> list[tuple]:
        """Snapshot of the journal so far, oldest first."""
        with self._journal_lock:
            return list(self._journal)

    def ops_after(self, seq: int) -> list[tuple]:
        return [e for e in self.journal() if e[1] > seq]

    def ops_by_name(self, op: str) -> list[tuple]:
        return [e for e in self.journal() if e[2] == op]

    def last_op_for(self, slot) -> tuple | None:
        matches = [e for e in self.journal() if e[3] == slot]
        return matches[-1] if matches else None

    def current_seq(self) -> int:
        with self._journal_lock:
            return self._journal[-1][1] if self._journal else 0

    def clear_journal(self) -> None:
        with self._journal_lock:
            self._journal.clear()

    # ---------------------------------------------------------------- #
    # Overridden writes -- every one funnels through _do_write so fault
    # injection/latency/journaling apply uniformly.
    # ---------------------------------------------------------------- #
    def set_key_image(self, key, image):
        self._do_write("set_key_image", f"key:{key}", image)

    def set_touchscreen_image(self, image, x_pos=0, y_pos=0, width=0, height=0):
        self._do_write("set_touchscreen_image", "touchscreen", image)

    def set_brightness(self, percent):
        self._do_write("set_brightness", "brightness", percent)

    def set_key_color(self, key, r, g, b):
        self._do_write("set_key_color", f"key_color:{key}", (r, g, b))

    def set_screen_image(self, image):
        self._do_write("set_screen_image", "screen", image)

    def reset(self):
        self._do_write("reset", "device", None)

    def close(self):
        # Journal the close first (the op is lifecycle-exempt, so a double- or
        # post-unplug close is a safe no-op that still records), THEN release
        # the handle so any LATER write raises. Idempotent.
        self._do_write("close", "device", None)
        with self._lifecycle_lock:
            self._open = False

    # ---------------------------------------------------------------- #
    # Lifecycle queries -- FakeDeck hard-wired both to True; reflect state.
    # ---------------------------------------------------------------- #
    def is_open(self) -> bool:
        with self._lifecycle_lock:
            return self._open

    def connected(self) -> bool:
        with self._lifecycle_lock:
            return self._connected

    def open(self, *args, **kwargs):
        # Re-opening a device (replug) clears the closed state. Unplug leaves
        # _connected False -- only a real reconnection would flip that, which
        # a scenario models by constructing a fresh deck.
        with self._lifecycle_lock:
            if self._connected:
                self._open = True

    # ---------------------------------------------------------------- #
    # Input event injection -- fires callbacks the way the real reader
    # thread would (see StreamDeck.Devices.StreamDeck._read and
    # BetterDeck.set_key_callback's physical->logical remapping).
    # ---------------------------------------------------------------- #
    def fire_key_event(self, physical_key: int, state: bool) -> None:
        if self._key_callback is not None:
            self._key_callback(self, physical_key, state)

    def fire_dial_event(self, dial: int, event_type, value) -> None:
        if self._dial_callback is not None:
            self._dial_callback(self, dial, event_type, value)

    def fire_touchscreen_event(self, event_type, value) -> None:
        if self._touchscreen_callback is not None:
            self._touchscreen_callback(self, event_type, value)
