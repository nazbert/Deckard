"""FakeDeck subclass that journals every device write.

Entries are (t, seq, op, slot, bytes_hash, thread_name). The deck also injects
TransportErrors, latency and input events. Import fixtures first.
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
    # Non-bytes payloads hash their repr, so the journal stays comparable.
    return hashlib.sha1(repr(data).encode()).hexdigest()[:12]


class FaultyFakeDeck(FakeDeck):
    """FakeDeck plus a write journal and scriptable fault injection.

    Journal entries are (t, seq, op, slot, bytes_hash, thread_name). Each deck
    counts seq on its own, so two-deck scenarios keep independent sequences.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._journal_lock = threading.Lock()
        self._journal: list[tuple] = []
        self._seq_counter = itertools.count(1)

        # Maps an op-name substring to the failures left to inject. _maybe_fail
        # matches the substring, so a pattern fails only the ops that contain it.
        self._fail_lock = threading.Lock()
        self._fail_schedule: dict[str, int] = {}

        self._write_latency: float = 0.0

        # FakeDeck hard-wires is_open() and connected() to True, which hides a
        # post-close write and makes an unplug inexpressible. Model both here.
        # close() clears _open; simulate_unplug() clears _connected and _open.
        # Strict mode makes later writes raise. set_strict_lifecycle(False) opts out.
        self._lifecycle_lock = threading.Lock()
        self._open = True
        self._connected = True
        self._strict_lifecycle = True

        # DeckController.__init__ registers these via BetterDeck.set_*_callback.
        # fire_*_event() calls them the way the reader thread does.
        self._key_callback = None
        self._dial_callback = None
        self._touchscreen_callback = None

    # BetterDeck.set_key_callback wraps this with a physical-to-logical
    # remapper, which a real controller exercises through BetterDeck.
    def set_key_callback(self, callback):
        self._key_callback = callback

    def set_dial_callback(self, callback):
        self._dial_callback = callback

    def set_touchscreen_callback(self, callback):
        self._touchscreen_callback = callback

    # Fault injection
    def fail_next(self, op_pattern: str, count: int = 1) -> None:
        """Fail the next count writes whose op name contains op_pattern.

        A failing write raises TransportError and never reaches the journal.
        """
        with self._fail_lock:
            self._fail_schedule[op_pattern] = self._fail_schedule.get(op_pattern, 0) + count

    def clear_failures(self) -> None:
        """Cancel every scheduled failure. fail_next adds to the schedule."""
        with self._fail_lock:
            self._fail_schedule.clear()

    def set_write_latency(self, seconds: float) -> None:
        """Sleep seconds before each write lands and before it is journaled."""
        self._write_latency = seconds

    def _maybe_fail(self, op: str) -> None:
        with self._fail_lock:
            for pattern, remaining in list(self._fail_schedule.items()):
                if remaining > 0 and pattern in op:
                    self._fail_schedule[pattern] = remaining - 1
                    raise TransportError(f"FaultyFakeDeck: injected failure for {op}")

    # Closed and unplugged lifecycle states
    def set_strict_lifecycle(self, strict: bool) -> None:
        """Make writes after close() or simulate_unplug() raise TransportError.

        Strict is the default. Set False to let post-close writes journal.
        """
        with self._lifecycle_lock:
            self._strict_lifecycle = strict

    def simulate_unplug(self) -> None:
        """Model a yanked USB cable, so connected() and is_open() read False.

        Every later write fails in strict mode.
        """
        with self._lifecycle_lock:
            self._connected = False
            self._open = False

    def _lifecycle_reject(self, op: str) -> None:
        """Raise TransportError for a write after close or unplug, in strict mode.

        close is exempt, so a double close stays a safe no-op.
        """
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

    # Journal
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
        # Fail or sleep before journaling; a failed write must not land in the
        # journal. Lifecycle rejection comes first and outranks fault injection.
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

    # Every overridden write funnels through _do_write, so fault injection,
    # latency and journaling apply uniformly.
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
        # The close op is lifecycle-exempt, so a double or post-unplug close
        # still records. Journal it first, then release the handle, so a later
        # write raises. close() is idempotent.
        self._do_write("close", "device", None)
        with self._lifecycle_lock:
            self._open = False

    # Lifecycle queries. FakeDeck hard-wires both to True; report state instead.
    def is_open(self) -> bool:
        with self._lifecycle_lock:
            return self._open

    def connected(self) -> bool:
        with self._lifecycle_lock:
            return self._connected

    def open(self, *args, **kwargs):
        # A replug clears the closed state. Unplug leaves _connected False; a
        # scenario models a real reconnection with a fresh deck.
        with self._lifecycle_lock:
            if self._connected:
                self._open = True

    # Input event injection fires the callbacks the way the reader thread does
    # (StreamDeck.Devices.StreamDeck._read, BetterDeck.set_key_callback).
    def fire_key_event(self, physical_key: int, state: bool) -> None:
        if self._key_callback is not None:
            self._key_callback(self, physical_key, state)

    def fire_dial_event(self, dial: int, event_type, value) -> None:
        if self._dial_callback is not None:
            self._dial_callback(self, dial, event_type, value)

    def fire_touchscreen_event(self, event_type, value) -> None:
        if self._touchscreen_callback is not None:
            self._touchscreen_callback(self, event_type, value)
