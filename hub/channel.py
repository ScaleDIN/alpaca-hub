"""The control channel between the hub and a driver process (REFACTOR.md step 5).

One multiprocessing.connection per driver process, over a named pipe with a random key. Messages are JSON (never
pickle: unpickling what a driver process sends would run its code in the hub), each at most MAX_MSG bytes. bytes
travel as {"$b": base64}; tuples arrive as lists.

Message kinds:
  ["call", id, method, [args]]     either side asks; answered by
  ["ret", id, ok, value]           ok false: value is the error text
  anything else                    a frame, handed to on_frame (the relay's ["r", chan, msg], simulator data)

Calls are answered on their own threads, so a slow one (a probe waiting for the device) never holds up the frames it
needs. Sending goes through a queue and its own thread, so a peer that stops reading never blocks the sender.
"""
import base64
import itertools
import json
import logging
import queue
import threading

log = logging.getLogger("hub.channel")

MAX_MSG = 1 << 20          # bytes per message
MAX_QUEUE = 20000          # messages waiting to be sent before the peer counts as stuck


class ChannelClosed(OSError):
    pass


class CallFailed(Exception):
    """The peer answered a call with an error."""


class CallTimeout(TimeoutError):
    pass


class BadMessage(ValueError):
    """A message that isn't what the protocol allows: counts as a failure of the side that sent it."""


def _default(o):
    if isinstance(o, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(o)).decode("ascii")}
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"cannot send {type(o).__name__}")


def _hook(d):
    if len(d) == 1 and "$b" in d:
        v = d["$b"]
        if not isinstance(v, str):
            raise BadMessage("bad bytes")
        return base64.b64decode(v.encode("ascii"), validate=True)
    return d


def encode(msg):
    data = json.dumps(msg, default=_default, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_MSG:
        raise ValueError(f"message of {len(data)} bytes is over {MAX_MSG}")
    return data


def decode(data):
    try:
        msg = json.loads(data.decode("utf-8"), object_hook=_hook)
    except BadMessage:
        raise
    except Exception as e:
        raise BadMessage(f"not a message: {e}") from None
    if not isinstance(msg, list) or not msg or not isinstance(msg[0], str):
        raise BadMessage("not a message")
    return msg


class Channel:
    """on_call(method, args) -> value (raise to answer with an error); on_frame(msg); on_close(reason, bad).
    on_close runs once: bad is True when the peer sent something the protocol doesn't allow."""

    def __init__(self, conn, name, on_call=None, on_frame=None, on_close=None, methods=None):
        self.name = name
        self._conn = conn
        self._on_call = on_call
        self._on_frame = on_frame
        self._on_close = on_close
        self._methods = methods            # the methods the peer may call (None: any)
        self._ids = itertools.count(1)
        self._pending = {}                 # id -> [event, ok, value]
        self._lock = threading.Lock()
        self._q = queue.Queue()
        self._closed = threading.Event()
        self.close_reason = None
        self._reader = threading.Thread(target=self._read, name=f"chan-r-{name}", daemon=True)
        self._writer = threading.Thread(target=self._write, name=f"chan-w-{name}", daemon=True)
        self._reader.start()
        self._writer.start()

    @property
    def closed(self):
        return self._closed.is_set()

    def send(self, msg):
        if self._closed.is_set():
            raise ChannelClosed(self.close_reason or f"{self.name}: closed")
        data = encode(msg)
        if self._q.qsize() > MAX_QUEUE:
            self._close(f"{self.name}: the other side stopped reading", False)
            raise ChannelClosed(self.close_reason)
        self._q.put(data)

    def call(self, method, *args, timeout=10.0):
        i = next(self._ids)
        slot = [threading.Event(), None, None]
        with self._lock:
            self._pending[i] = slot
        try:
            self.send(["call", i, method, list(args)])
            if not slot[0].wait(timeout):
                if self._closed.is_set():
                    raise ChannelClosed(self.close_reason or f"{self.name}: closed")
                raise CallTimeout(f"{self.name}: {method} took more than {timeout:g} s")
        finally:
            with self._lock:
                self._pending.pop(i, None)
        if slot[1] is None:
            raise ChannelClosed(self.close_reason or f"{self.name}: closed")
        if not slot[1]:
            raise CallFailed(slot[2])
        return slot[2]

    def close(self, reason="closed"):
        self._close(reason, False)

    # ---------------------------------------------------------------- threads
    def _write(self):
        while True:
            data = self._q.get()
            if data is None:
                return
            try:
                self._conn.send_bytes(data)
            except Exception as e:
                self._close(f"{self.name}: sending failed ({e})", False)
                return

    def _read(self):
        while not self._closed.is_set():
            try:
                data = self._conn.recv_bytes(MAX_MSG)
            except EOFError:
                self._close(f"{self.name}: the other side closed", False)
                return
            except OSError as e:
                bad = "too long" in str(e) or "message length" in str(e).lower()
                self._close(f"{self.name}: {'message over the size limit' if bad else e}", bad)
                return
            except Exception as e:      # the pipe closed under a read (multiprocessing asserts then)
                self._close(f"{self.name}: reading failed ({type(e).__name__})", False)
                return
            try:
                self._dispatch(decode(data))
            except BadMessage as e:
                self._close(f"{self.name}: bad message ({e})", True)
                return
            except Exception:
                log.exception("%s: handling a message failed", self.name)

    def _dispatch(self, msg):
        kind = msg[0]
        if kind == "ret":
            if len(msg) != 4 or not isinstance(msg[1], int) or not isinstance(msg[2], bool):
                raise BadMessage("bad answer")
            with self._lock:
                slot = self._pending.get(msg[1])
            if slot is not None:
                slot[1], slot[2] = msg[2], msg[3]
                slot[0].set()
            return
        if kind == "call":
            if len(msg) != 4 or not isinstance(msg[1], int) or not isinstance(msg[2], str) \
                    or not isinstance(msg[3], list):
                raise BadMessage("bad call")
            if self._on_call is None or (self._methods is not None and msg[2] not in self._methods):
                raise BadMessage(f"call {msg[2]!r} not allowed")
            threading.Thread(target=self._answer, args=(msg[1], msg[2], msg[3]), name=f"call-{msg[2]}",
                             daemon=True).start()
            return
        if self._on_frame is None:
            raise BadMessage(f"unexpected {kind!r}")
        self._on_frame(msg)

    def _answer(self, i, method, args):
        try:
            value = self._on_call(method, args)
            reply = ["ret", i, True, value]
        except BadMessage as e:
            self._close(f"{self.name}: bad call {method} ({e})", True)
            return
        except Exception as e:
            reply = ["ret", i, False, f"{type(e).__name__}: {e}"]
        try:
            self.send(reply)
        except ChannelClosed:
            pass
        except ValueError as e:            # the answer itself couldn't be sent (too big, not JSON)
            try:
                self.send(["ret", i, False, f"answer not sendable: {e}"])
            except Exception:
                pass

    def _close(self, reason, bad):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self.close_reason = reason
            pending = list(self._pending.values())
        for slot in pending:
            slot[0].set()
        self._q.put(None)
        try:
            self._conn.close()
        except Exception:
            pass
        if self._on_close is not None:
            try:
                self._on_close(reason, bad)
            except Exception:
                log.exception("%s: close handler failed", self.name)
