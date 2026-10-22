"""
Remote control for Fire programs.

``fire.Fire(component, remote=True)`` gives a program a ``server``
command, and a ``--remote [PASSWORD@]HOST[:PORT]`` flag that runs the
rest of the command line on such a server. The server runs every
command line in a fresh process of the program, whose standard input,
output and error are terminals or pipes as the client's are, so that
the program behaves as it does locally. A server on Windows, which has
no pseudo-terminals, gives every command pipes.

Imported by fireaid when a program asks for it.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import itertools
import json
import logging
import os
import queue
import secrets
import select
import selectors
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

try:
    import fcntl
    import termios
    import tty
except ImportError:  # Windows
    fcntl = termios = tty = None

# Whether this system has pseudo-terminals for the commands a server runs.
HAS_PTY = termios is not None and hasattr(os, "openpty")

logger = logging.getLogger("fireaid.remote")

# 2: input as the server asks for it, and a server that proves it knows the password.
PROTOCOL = 2
FLAG = "--remote"

# Set in the environment of every command the server runs.
CHILD_VARIABLE = "FIREAID_REMOTE_CHILD"

# Makes server and client behave as on Windows: the server gives commands
# pipes, the client leaves its terminal alone and reads it in a thread.
_TEST_NO_PTY_VARIABLE = "FIREAID_REMOTE_TEST_NO_PTY"

# What decides how a program presents its output, taken from the client.
TERMINAL_VARIABLES = (
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "FORCE_COLOR",
    "CLICOLOR",
    "CLICOLOR_FORCE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "COLUMNS",
    "LINES",
)
# Not PAGER: Fire runs it in a shell, so a client's would run on the server.

# Messages. A frame is the length of what follows, the type, and the
# payload: JSON for control messages, bytes for data. The server sends
# CREDIT for the input it is ready to take, and takes no more.
HELLO, AUTH, RUN, STDIN, STDIN_EOF, WINCH, SIGNAL = 1, 2, 3, 4, 5, 6, 7
CHALLENGE, AUTH_OK, REFUSED, STDOUT, STDERR, EXIT, TTY, CREDIT = 129, 130, 131, 132, 133, 134, 135, 136

_HEADER = struct.Struct(">IB")
_MAX_FRAME = 1 << 20
_CHUNK = 1 << 16
_BACKLOG = 1 << 20  # buffered output beyond which reading pauses, and the input a server takes ahead
_SIGNALS = {name: getattr(signal, "SIG" + name) for name in ("INT", "TERM", "HUP", "QUIT")
            if hasattr(signal, "SIG" + name)}
_CONTROL_C_EXIT = 0xC000013A  # how Windows reports a process ended by Ctrl-Break

_CONNECT_TIMEOUT = 5.0
# Errors a fresh LAN neighbour gives for a moment, while its address is being resolved.
_TRANSIENT = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.EADDRNOTAVAIL}
_HANDSHAKE_TIMEOUT = 10.0
_MAX_HANDSHAKES = 8  # clients logging in at once; more are turned away
# A peer that vanished without a word is given up after a minute or so.
_KEEPALIVE = (("TCP_KEEPIDLE", 30), ("TCP_KEEPALIVE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3))
_TICK = 0.2
_POLL = 0.05  # the wait of a loop that also takes from a queue


def _like_windows() -> bool:
    return os.name == "nt" or bool(os.environ.get(_TEST_NO_PTY_VARIABLE))


def _pty_available() -> bool:
    return HAS_PTY and not _like_windows()


class RemoteError(Exception):
    """A failure to be reported to the user, with the exit code to use."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class _ProtocolError(Exception):
    pass


# -- The command line


def _stem(prog: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in os.path.splitext(prog)[0].upper())


def environment_variable(prog: str, configured: str | None = None) -> str:
    """The variable a client takes --remote from: PROG_REMOTE."""
    return configured or _stem(prog) + "_REMOTE"


def password_variable(prog: str) -> str:
    """The variable a server takes its password from: PROG_SERVER_PASSWORD."""
    return _stem(prog) + "_SERVER_PASSWORD"


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    password: str | None

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def parse_target(text: str, default_port: int) -> Target:
    """
    Parse ``[PASSWORD@]HOST[:PORT]``.

    The password ends at the last ``@``, so it may contain ``@``
    itself. An IPv6 address goes in brackets: ``[::1]:4247``.
    """
    password = None
    if "@" in text:
        password, _, text = text.rpartition("@")
    port_text = None
    if text.startswith("["):
        host, closed, rest = text[1:].partition("]")
        if not closed or (rest and not rest.startswith(":")):
            raise ValueError("an IPv6 address goes in brackets, as in [::1]:4247")
        port_text = rest[1:] if rest else None
    elif text.count(":") > 1:
        raise ValueError("an IPv6 address goes in brackets, as in [::1]:4247")
    else:
        host, colon, port_text = text.partition(":")
        port_text = port_text if colon else None
    if not host:
        raise ValueError("no host")
    if port_text is None:
        return Target(host, default_port, password)
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError(f"{port_text!r} is not a port")
    return Target(host, int(port_text), password)


def take_flag(command: Sequence[str], prog: str, default_port: int, variable: str) -> tuple[Target | None, list[str]]:
    """
    Take ``--remote`` out of a command line.

    Returns the target, or None to run the command here, and the
    command line without the flag. The flag may be anywhere before a
    ``--``, as ``--remote VALUE`` or ``--remote=VALUE``. Without it, the
    target comes from the environment variable, except for the server
    command, which never runs remotely. An empty value runs the command
    here.
    """
    words = list(command)
    rest: list[str] = []
    value = None
    seen = False
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--":
            rest.extend(words[i:])
            break
        if word == FLAG or word.startswith(FLAG + "="):
            if seen:
                raise RemoteError(f"{FLAG} is given twice", 2)
            seen = True
            if word == FLAG:
                if i + 1 == len(words) or words[i + 1] == "--":
                    raise RemoteError(f"{FLAG} needs a value: [PASSWORD@]HOST[:PORT]", 2)
                value = words[i + 1]
                i += 2
            else:
                value = word[len(FLAG) + 1 :]
                i += 1
            continue
        rest.append(word)
        i += 1

    source = FLAG
    if not seen and rest[:1] != ["server"]:
        value = os.environ.get(variable)
        source = variable
    if not value:
        return None, rest
    try:
        return parse_target(value, default_port), rest
    except ValueError as e:
        raise RemoteError(f"{source}: {e}", 2) from None


def refusal(argv: Sequence[str], deny: Sequence[str]) -> str | None:
    """
    Tell why the server will not run a command line, or None if it will.

    Nothing may follow the last ``--``. Fire's own flags go there, its
    interactive mode among them, which is a Python shell; none is needed
    remotely, as ``--help`` works without a separator. A deny entry
    names a command path, as words. It covers every command line in
    which those words appear in that order, help for them included:
    Fire chains commands, so a denied command may be reached after
    another. The server command itself is denied unless help for it is
    asked for, in its plain forms.
    """
    argv = list(argv)
    if "--" in argv:
        last = len(argv) - 1 - argv[::-1].index("--")
        if argv[last + 1 :]:
            return "flags after -- are not available remotely; --help works without the separator"
    words = argv[: argv.index("--")] if "--" in argv else argv
    if any(w == FLAG or w.startswith(FLAG + "=") for w in words):
        return f"{FLAG} cannot be passed on to the server"

    def fold(text: str) -> list[str]:
        return [w.replace("-", "_") for w in text.split()]

    path = fold(" ".join(w for w in words if not w.startswith("-")))
    for entry in deny:
        steps = fold(entry)
        if steps and any(path[i : i + len(steps)] == steps for i in range(len(path))):
            return f"'{entry}' is not available remotely"
    if "server" in path and argv not in (["help", "server"], ["server", "--help"], ["server", "-h"]):
        return "'server' is not available remotely"
    return None


# -- How the server starts the program


@dataclass(frozen=True)
class Launch:
    """How to start the running program again, with other arguments."""

    prefix: tuple[str, ...]
    cwd: str
    error: str | None = None

    @classmethod
    def capture(cls) -> Launch:
        cwd = os.getcwd()
        main = sys.modules.get("__main__")
        spec = getattr(main, "__spec__", None)
        if spec is not None and spec.name:
            # python -m package: run the package again, not its file.
            name = spec.name
            if name.endswith(".__main__"):
                name = name[: -len(".__main__")]
            return cls((sys.executable, "-m", name), cwd)
        script = sys.argv[0] if sys.argv else ""
        if not script or not os.path.isfile(script):
            return cls((), cwd, f"cannot start this program again: {script or 'no script'} is not a file")
        if script.lower().endswith(".exe"):
            return cls((script,), cwd)  # a console script's launcher on Windows
        return cls((sys.executable, script), cwd)


# -- Framing


def _frame(kind: int, payload: bytes | dict = b"") -> bytes:
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    if len(payload) >= _MAX_FRAME:
        raise ValueError("frame too large")
    return _HEADER.pack(len(payload) + 1, kind) + payload


def _parse(buffer: bytearray) -> Iterator[tuple[int, bytes]]:
    """Take the complete frames out of the start of buffer."""
    while len(buffer) >= _HEADER.size:
        length, kind = _HEADER.unpack_from(buffer)
        if not 0 < length <= _MAX_FRAME:
            raise _ProtocolError(f"bad frame length {length}")
        end = _HEADER.size - 1 + length  # the length counts the type
        if len(buffer) < end:
            return
        payload = bytes(buffer[_HEADER.size : end])
        del buffer[:end]
        yield kind, payload


def _json(payload: bytes) -> dict:
    try:
        value = json.loads(payload.decode())
    except (UnicodeDecodeError, ValueError):
        raise _ProtocolError("bad message") from None
    if not isinstance(value, dict):
        raise _ProtocolError("bad message")
    return value


class _Channel:
    """A socket that sends and receives frames, blocking or not."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.incoming = bytearray()
        self.outgoing = bytearray()

    # Blocking, for the handshake.

    def write(self, kind: int, payload: bytes | dict = b"") -> None:
        self.sock.sendall(_frame(kind, payload))

    def read(self) -> tuple[int, bytes]:
        while True:
            for message in _parse(self.incoming):
                return message
            data = self.sock.recv(_CHUNK)
            if not data:
                raise EOFError("connection closed")
            self.incoming += data

    def expect(self, kind: int) -> bytes:
        got, payload = self.read()
        if got != kind:
            raise _ProtocolError(f"unexpected message {got}")
        return payload

    # Non-blocking, once a command runs.

    def queue(self, kind: int, payload: bytes | dict = b"") -> None:
        if isinstance(payload, (bytes, bytearray)) and len(payload) > _CHUNK:
            for i in range(0, len(payload), _CHUNK):
                self.outgoing += _frame(kind, bytes(payload[i : i + _CHUNK]))
        else:
            self.outgoing += _frame(kind, payload)

    def flush(self) -> None:
        """Send what the socket takes now. Raises EOFError if the peer is gone."""
        while self.outgoing:
            try:
                sent = self.sock.send(self.outgoing)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                raise EOFError(e.strerror or "connection lost") from None
            del self.outgoing[:sent]

    def receive(self) -> list[tuple[int, bytes]]:
        """
        The frames that have arrived, those read along with an earlier
        one included. Raises EOFError if the peer is gone.
        """
        try:
            data = self.sock.recv(_CHUNK)
        except (BlockingIOError, InterruptedError):
            data = None
        except OSError as e:
            raise EOFError(e.strerror or "connection lost") from None
        if data is not None:
            if not data:
                raise EOFError
            self.incoming += data
        return list(_parse(self.incoming))


def _mac(password: str, side: str, server_nonce: bytes, client_nonce: bytes) -> str:
    """The proof that a side, client or server, knows the password: each proves it on a text of its own."""
    text = side.encode() + b"\0" + server_nonce + client_nonce
    return hmac.new(password.encode(), text, hashlib.sha256).hexdigest()


def _proves(mac: Any, expected: str) -> bool:
    return isinstance(mac, str) and hmac.compare_digest(mac.encode(), expected.encode())


def _nonce(text: Any) -> bytes:
    try:
        nonce = bytes.fromhex(text)
    except (TypeError, ValueError):
        raise _ProtocolError("bad nonce") from None
    if len(nonce) < 16:
        raise _ProtocolError("bad nonce")
    return nonce


def _integer(message: dict, key: str, default: int) -> int:
    value = message.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ProtocolError(f"bad {key}")
    return value


def _keep_alive(sock: socket.socket) -> None:
    """Have the system notice a peer that vanished without a word, such as a computer gone to sleep."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for name, value in _KEEPALIVE:
        option = getattr(socket, name, None)
        if option is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                pass


def _set_size(fd: int, size: Any) -> None:
    try:
        rows, cols = int(size["rows"]), int(size["cols"])
    except (KeyError, TypeError, ValueError):
        return
    if not (0 <= rows < 65536 and 0 <= cols < 65536):
        return
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _watch(selector: selectors.BaseSelector, fileobj: Any, events: int) -> None:
    """Make selector watch fileobj for events, or not at all for none."""
    try:
        key = selector.get_key(fileobj)
    except KeyError:
        key = None
    if not events:
        if key is not None:
            selector.unregister(fileobj)
    elif key is None:
        selector.register(fileobj, events)
    elif key.events != events:
        selector.modify(fileobj, events)


# -- The server


class _PlainFormatter(logging.Formatter):
    """Log lines as a command-line tool prints them: ``prog: warning: ...``."""

    def __init__(self, prog: str) -> None:
        super().__init__("%(message)s")
        self.prog = prog

    def format(self, record: logging.LogRecord) -> str:
        level = f"{record.levelname.lower()}: " if record.levelno >= logging.WARNING else ""
        return f"{self.prog}: {level}{super().format(record)}"


def _plain_logging(prog: str, debug: bool) -> None:
    """Have the server's logger print plain lines to stderr, unless the program has set up logging."""
    if logging.getLogger().handlers or logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_PlainFormatter(prog))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False


def serve(launch: Launch, deny: Sequence[str], prog: str, variable: str, port: Any, password: Any, bind: Any,
          debug: bool) -> None:
    """Run the server until interrupted.

    It logs through the ``fireaid.remote`` logger. A program that has not
    set up logging gets plain lines on stderr.

    Commands run one at a time, in this thread. Clients log in in threads
    of their own, so that one that is slow to log in keeps no other out,
    and one that logs in while a command runs is told the server is busy.
    """
    _plain_logging(prog, debug)
    if launch.error:
        raise RemoteError(launch.error)
    if password == "True":
        # Fire gives a flag without a value the text True, as for a boolean.
        raise RemoteError("--password needs a value", 2)
    secret = password_variable(prog)
    if password is None:
        password = os.environ.get(secret) or None
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise RemoteError(f"{port!r} is not a port", 2)

    if not isinstance(bind, str) or not bind:
        raise RemoteError(f"{bind!r} is not an address to bind to", 2)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((bind, port))
        listener.listen(5)
    except OSError as e:
        listener.close()
        raise RemoteError(f"cannot listen on {bind}:{port}: {e.strerror or e}") from None

    def stop(sig, frame):
        raise SystemExit(0)

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, _SIGNALS.get("HUP")) if sig}
    notes = "" if password is not None else ", no password"
    notes += "" if _pty_available() else ", commands get pipes"
    logger.info(f"serving on {bind}:{listener.getsockname()[1]}{notes}")
    if password is None:
        logger.warning(f"anyone who can reach the port may run commands; --password or {secret} adds HMAC "
                       "challenge-response authentication (mild security)")
    if not bind.startswith("127."):
        logger.warning("the command server is unencrypted, do not use over untrusted networks; "
                       "--bind 127.0.0.1 restricts access to localhost")
    logger.debug(f"starting {' '.join(launch.prefix)} in {launch.cwd} for each command")

    hidden = (variable, secret)  # what the commands do not get from the server's environment
    ready: queue.Queue = queue.Queue()  # sessions logged in, whose command is to run
    busy = threading.Lock()  # held from a login until its command has run
    logins = threading.BoundedSemaphore(_MAX_HANDSHAKES)
    stopping = threading.Event()

    def admit(sock: socket.socket, peer: Any) -> None:
        session = _Session(sock, peer, launch, deny, hidden, password)
        try:
            if session.admit(busy):
                ready.put(session)
                return
        except Exception as e:  # one client must not stop the server
            logger.error(f"{peer[0]}: {e}")
        finally:
            logins.release()
        session.close()

    def accept() -> None:
        listener.settimeout(_TICK)
        while not stopping.is_set():
            try:
                sock, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return  # the listener is closed
            logger.debug(f"{peer[0]}:{peer[1]} connected")
            if not logins.acquire(blocking=False):
                logger.info(f"{peer[0]}: turned away, {_MAX_HANDSHAKES} clients are logging in")
                sock.close()
                continue
            threading.Thread(target=admit, args=(sock, peer), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        while True:
            try:
                session = ready.get(timeout=1.0)  # so that Ctrl-C gets through on Windows
            except queue.Empty:
                continue
            try:
                session.run()
            except Exception as e:  # one client must not stop the server
                logger.error(f"{session.peer}: {e}")
            finally:
                busy.release()
                session.close()
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        listener.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


class _Session:
    """One client, one command."""

    def __init__(self, sock: socket.socket, peer: Any, launch: Launch, deny: Sequence[str], hidden: Sequence[str],
                 password: str | None) -> None:
        self.channel = _Channel(sock)
        self.address = peer
        self.peer = peer[0]
        self.launch = launch
        self.deny = deny
        self.hidden = hidden
        self.password = password
        self.request: dict = {}

    def admit(self, busy: threading.Lock) -> bool:
        """
        Log the client in and take its command line, in a thread of its
        own. Returns whether the command is to run, then holding busy.
        """
        _keep_alive(self.channel.sock)
        self.channel.sock.settimeout(_HANDSHAKE_TIMEOUT)
        try:
            request = self.handshake()
        except _ProtocolError as e:
            self.refuse(str(e), 2)
            return False
        except (OSError, EOFError) as e:
            logger.debug(f"{self.peer}: gone during the handshake: {e}")
            return False
        if request is None:
            return False
        if not busy.acquire(blocking=False):
            self.refuse("busy with another client's command; try again later", 1)
            return False
        self.request = request
        return True

    def run(self) -> None:
        request = self.request
        argv = request["argv"]
        terminals = "".join("1" if x else "0" for x in request["tty"])
        # A flag's value may be a secret, such as a password: the whole line is for debugging only.
        words = list(itertools.takewhile(lambda w: not w.startswith("-"), argv))
        more = " ..." if len(words) < len(argv) else ""
        logger.info(f"{self.peer} runs: {shlex.join(words) or '(flags)'}{more}")
        logger.debug(f"as: {shlex.join([*self.launch.prefix, *argv])} in {self.launch.cwd}; "
                     f"tty {terminals}, size {request.get('size')}, env {request['env']}")
        self.channel.sock.setblocking(False)
        runner = _PtyCommand if _pty_available() else _PipeCommand
        started = time.monotonic()
        code = runner(self.channel, self.launch, request, self.hidden).run()
        seconds = time.monotonic() - started
        if code is None:
            logger.info(f"{self.peer} went away after {seconds:.1f} s; the command was ended")
        else:
            logger.info(f"{self.peer} done: exit {code} after {seconds:.1f} s")

    def close(self) -> None:
        self.channel.sock.close()
        logger.debug(f"{self.address[0]}:{self.address[1]} done")

    def refuse(self, message: str, code: int) -> None:
        logger.info(f"{self.peer}: {message}")
        try:
            self.channel.write(REFUSED, {"message": message, "code": code})
        except OSError:
            pass

    def handshake(self) -> dict | None:
        channel = self.channel
        nonce = secrets.token_bytes(32)
        channel.write(CHALLENGE, {"proto": PROTOCOL, "nonce": nonce.hex(), "auth": self.password is not None,
                                  "pty": _pty_available()})

        hello = _json(channel.expect(HELLO))
        if hello.get("proto") != PROTOCOL:
            self.refuse(f"this server speaks fireaid remote protocol {PROTOCOL}, "
                        f"the client {hello.get('proto')}", 1)
            return None
        if self.password is None:
            channel.write(AUTH_OK)
        else:
            auth = _json(channel.expect(AUTH))
            theirs = _nonce(auth.get("nonce"))
            if not _proves(auth.get("mac"), _mac(self.password, "client", nonce, theirs)):
                self.refuse("authentication failed", 1)
                return None
            channel.write(AUTH_OK, {"mac": _mac(self.password, "server", nonce, theirs)})

        request = _json(channel.expect(RUN))
        argv = request.get("argv")
        if not isinstance(argv, list) or not all(isinstance(w, str) for w in argv):
            raise _ProtocolError("bad command line")
        flags = request.get("tty")
        if not isinstance(flags, list) or len(flags) != 3:
            raise _ProtocolError("bad terminal description")
        env = request.get("env") or {}
        if not isinstance(env, dict):
            raise _ProtocolError("bad environment")
        request["env"] = {k: v for k, v in env.items() if k in TERMINAL_VARIABLES and isinstance(v, str)}

        reason = refusal(argv, self.deny)
        if reason:
            self.refuse(reason, 2)
            return None
        return request


def _environment(request: dict, hidden: Sequence[str]) -> dict:
    # The client's terminal variables replace ours. The program's own
    # --remote variable would send the command on again, and the server's
    # password is the server's.
    env = {k: v for k, v in os.environ.items() if k not in TERMINAL_VARIABLES and k not in hidden}
    env.update(request["env"])
    env[CHILD_VARIABLE] = "1"
    return env


def _exit_code(returncode: int) -> int:
    """A child's return code as the client's exit code."""
    if returncode < 0:
        return 128 - returncode  # ended by a signal
    if returncode == _CONTROL_C_EXIT:
        return 128 + signal.SIGINT
    return returncode if returncode < 256 else 1


def _interrupt(process: subprocess.Popen, name: str) -> None:
    """Send the signal of that name to the command, and what it started."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            # The nearest thing: the child is its own process group.
            process.send_signal(signal.CTRL_BREAK_EVENT)
        elif name in _SIGNALS:
            os.killpg(process.pid, _SIGNALS[name])
    except OSError:
        pass


def _kill(process: subprocess.Popen | None) -> None:
    """End the command, and what it started, as its client has gone."""
    if process is None or process.poll() is not None:
        return
    logger.debug("client gone, ending the command")
    if os.name == "nt":
        steps = ((process.terminate, 2.0), (process.kill, None))
    else:
        steps = ((lambda: os.killpg(process.pid, signal.SIGHUP), 2.0),
                 (lambda: os.killpg(process.pid, signal.SIGKILL), None))
    for step, wait in steps:
        try:
            step()
        except OSError:
            pass
        try:
            process.wait(wait)
            return
        except subprocess.TimeoutExpired:
            pass


def _send_exit(channel: _Channel, returncode: int) -> int:
    """Send the command's exit code to the client, and return it."""
    code = _exit_code(returncode)
    if code != returncode:
        logger.debug(f"command returned {returncode}, client gets {code}")
    channel.queue(EXIT, {"code": code})
    sock = channel.sock
    sock.setblocking(True)
    sock.settimeout(_HANDSHAKE_TIMEOUT)
    try:
        sock.sendall(channel.outgoing)
    except OSError:
        pass
    return code


def _take_terminal() -> None:
    """In the child: make the terminal among its stdio the controlling one."""
    for fd in (0, 1, 2):
        if os.isatty(fd):
            try:
                fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass
            return


class _Command:
    """
    A command line running in a child process, relayed to the client.

    Each kind of command spawns its process, relays until it is done,
    releases its stdio however the run ended, feeds it the client's
    input and resizes its terminal in its own way.

    The client sends input only as the server asks for it, so that what
    the command has yet to read stays within _BACKLOG: the server asks
    for more as the command takes it, and counts it in consumed.
    """

    def __init__(self, channel: _Channel, launch: Launch, request: dict, hidden: Sequence[str]) -> None:
        self.channel = channel
        self.launch = launch
        self.request = request
        self.hidden = hidden
        self.process: subprocess.Popen | None = None
        self.allowed = 0  # input the client may still send
        self.consumed = 0  # input taken by the command, or dropped, and not asked for again yet

    def run(self) -> int | None:
        """Run the command. Returns the client's exit code, or None if the client went away."""
        try:
            self.spawn()
            self.ask_for_input(_BACKLOG)
            try:
                self.relay()
            except (EOFError, ConnectionError, _ProtocolError):
                _kill(self.process)
                return None
        except BaseException:
            _kill(self.process)
            raise
        finally:
            self.release()
        return _send_exit(self.channel, self.process.wait())

    def popen(self, **options: Any) -> None:
        self.process = subprocess.Popen(
            [*self.launch.prefix, *self.request["argv"]],
            cwd=self.launch.cwd,
            env=_environment(self.request, self.hidden),
            **options,
        )

    def ask_for_input(self, size: int) -> None:
        self.allowed += size
        self.channel.queue(CREDIT, {"bytes": size})

    def ask_for_more_input(self) -> None:
        if self.consumed >= _CHUNK:
            self.ask_for_input(self.consumed)
            self.consumed = 0

    def on_message(self, kind: int, payload: bytes) -> None:
        if kind == STDIN:
            if len(payload) > self.allowed:
                raise _ProtocolError("more input than the server asked for")
            self.allowed -= len(payload)
            self.feed(payload)
        elif kind == STDIN_EOF:
            self.feed(None)
        elif kind == WINCH:
            self.resize(payload)
        elif kind == SIGNAL:
            _interrupt(self.process, str(_json(payload).get("name")))
        else:
            raise _ProtocolError(f"unexpected message {kind}")

    def resize(self, payload: bytes) -> None:
        pass  # no terminal to resize


class _PtyCommand(_Command):
    """A command with a pseudo-terminal for what is a terminal at the client."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.outputs: dict[int, int] = {}  # fd -> message type
        self.master: int | None = None
        self.stdin: int | None = None  # where the client's input goes
        self.stdin_is_pipe = False
        self.pending = bytearray()
        self.stdin_eof = False

    def spawn(self) -> None:
        stdin_tty, stdout_tty, stderr_tty = (bool(x) for x in self.request["tty"])
        child: list[int] = [0, 0, 0]
        close: list[int] = []
        slave = None
        if stdin_tty or stdout_tty or stderr_tty:
            self.master, slave = os.openpty()
            close.append(slave)
            if self.request.get("size"):
                _set_size(self.master, self.request["size"])
            if not self.request.get("raw"):
                # The client's terminal turns newlines into CR LF itself.
                attrs = termios.tcgetattr(slave)
                attrs[1] &= ~termios.OPOST
                termios.tcsetattr(slave, termios.TCSANOW, attrs)
            self.outputs[self.master] = TTY

        if stdin_tty:
            child[0] = slave
            self.stdin = self.master
        else:
            r, w = os.pipe()
            child[0] = r
            close.append(r)
            self.stdin, self.stdin_is_pipe = w, True

        if stdout_tty:
            child[1] = slave
        else:
            r, w = os.pipe()
            child[1] = w
            close.append(w)
            self.outputs[r] = STDOUT

        if stderr_tty:
            child[2] = slave
        elif self.request.get("same"):
            child[2] = child[1]  # 2>&1 on the client: one stream, in order
        else:
            r, w = os.pipe()
            child[2] = w
            close.append(w)
            self.outputs[r] = STDERR

        try:
            self.popen(
                stdin=child[0],
                stdout=child[1],
                stderr=child[2],
                start_new_session=True,
                preexec_fn=_take_terminal if slave is not None else None,
            )
        finally:
            for fd in close:
                os.close(fd)
        for fd in {*self.outputs, self.stdin}:
            os.set_blocking(fd, False)

    def release(self) -> None:
        for fd in {*self.outputs, self.stdin} - {None}:
            os.close(fd)

    def relay(self) -> None:
        channel = self.channel
        selector = selectors.SelectSelector()
        try:
            while self.outputs or self.process.poll() is None:
                _watch(selector, channel.sock,
                       selectors.EVENT_READ | (selectors.EVENT_WRITE if channel.outgoing else 0))
                for fd in {*self.outputs, self.stdin} - {None}:
                    events = 0
                    if fd in self.outputs and len(channel.outgoing) < _BACKLOG:
                        events |= selectors.EVENT_READ
                    if fd == self.stdin and self.pending:
                        events |= selectors.EVENT_WRITE
                    _watch(selector, fd, events)

                for key, events in selector.select(_TICK):
                    if key.fileobj is channel.sock:
                        if events & selectors.EVENT_WRITE:
                            channel.flush()
                        continue
                    fd = key.fd
                    if events & selectors.EVENT_READ and fd in self.outputs:
                        self.read_output(selector, fd)
                    if events & selectors.EVENT_WRITE and fd == self.stdin:
                        self.write_input()
                for kind, payload in channel.receive():
                    self.on_message(kind, payload)
                self.close_input_if_done(selector)
                self.ask_for_more_input()
        finally:
            selector.close()

    def read_output(self, selector: selectors.BaseSelector, fd: int) -> None:
        try:
            data = os.read(fd, _CHUNK)
        except BlockingIOError:
            return
        except OSError:  # EIO: the terminal's last user has closed it
            data = b""
        if data:
            self.channel.queue(self.outputs[fd], data)
            return
        _watch(selector, fd, 0)
        kind = self.outputs.pop(fd)
        if kind == TTY:
            self.master = None
            if self.stdin == fd:
                self.stdin = None
                self.drop_input()
        os.close(fd)

    def write_input(self) -> None:
        try:
            written = os.write(self.stdin, self.pending)
        except BlockingIOError:
            return
        except OSError:  # nobody reads it any more
            self.drop_input()
            self.stdin_eof = True
            return
        del self.pending[:written]
        self.consumed += written

    def drop_input(self) -> None:
        self.consumed += len(self.pending)
        self.pending.clear()

    def close_input_if_done(self, selector: selectors.BaseSelector) -> None:
        if self.stdin_is_pipe and self.stdin is not None and self.stdin_eof and not self.pending:
            _watch(selector, self.stdin, 0)
            os.close(self.stdin)
            self.stdin = None

    def feed(self, data: bytes | None) -> None:
        if data is None:
            self.stdin_eof = True
        elif self.stdin is not None and not self.stdin_eof:
            self.pending += data
        else:
            self.consumed += len(data)

    def resize(self, payload: bytes) -> None:
        if self.master is not None:
            _set_size(self.master, _json(payload))


class _PipeCommand(_Command):
    """
    A command with pipes for its stdio.

    For systems without pseudo-terminals, Windows above all, where pipes
    cannot be watched together with the socket either: threads read the
    command's output and write its input, the main loop minds the socket.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.output: queue.Queue = queue.Queue(maxsize=64)  # (kind, data), data None at the end
        self.input: queue.Queue = queue.Queue()  # data, None at the end
        self.written: queue.Queue = queue.Queue()  # the sizes of the input taken
        self.readers = 0
        self.stopped = threading.Event()  # nobody relays the output any more

    def spawn(self) -> None:
        if os.name == "nt":
            options: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            options = {"start_new_session": True}
        self.popen(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if self.request.get("same") else subprocess.PIPE,
            **options,
        )
        for pipe, kind in ((self.process.stdout, STDOUT), (self.process.stderr, STDERR)):
            if pipe is not None:
                self.readers += 1
                threading.Thread(target=self.read, args=(pipe, kind), daemon=True).start()
        threading.Thread(target=self.write, daemon=True).start()

    def read(self, pipe: Any, kind: int) -> None:
        try:
            while True:
                data = pipe.read1(_CHUNK)
                if not data or not self.hand_over((kind, data)):
                    break
        except (OSError, ValueError):
            pass
        finally:
            self.hand_over((kind, None))
            pipe.close()

    def hand_over(self, item: tuple[int, bytes | None]) -> bool:
        """Put item in the output queue, unless nobody relays any more. Returns whether it went."""
        while not self.stopped.is_set():
            try:
                self.output.put(item, timeout=_TICK)
                return True
            except queue.Full:
                pass
        return False

    def write(self) -> None:
        stdin = self.process.stdin
        broken = False
        while True:
            data = self.input.get()
            if data is None:
                break
            if not broken:
                try:
                    stdin.write(data)
                    stdin.flush()
                except (OSError, ValueError):
                    broken = True  # nobody reads it any more: what comes is dropped
            self.written.put(len(data))
        try:
            stdin.close()
        except OSError:
            pass

    def release(self) -> None:
        self.stopped.set()
        self.input.put(None)

    def relay(self) -> None:
        channel = self.channel
        sock = channel.sock
        while self.readers or self.process.poll() is None:
            while len(channel.outgoing) < _BACKLOG:
                try:
                    kind, data = self.output.get_nowait()
                except queue.Empty:
                    break
                if data is None:
                    self.readers -= 1
                else:
                    channel.queue(kind, data)
            _, writable, _ = select.select([sock], [sock] if channel.outgoing else [], [], _POLL)
            if writable:
                channel.flush()
            for kind, payload in channel.receive():
                self.on_message(kind, payload)
            while True:
                try:
                    self.consumed += self.written.get_nowait()
                except queue.Empty:
                    break
            self.ask_for_more_input()

    def feed(self, data: bytes | None) -> None:
        self.input.put(data)


# -- The client


def _same_file(a: int, b: int) -> bool:
    if os.name == "nt":
        return False  # pipes have no telling identity there
    try:
        sa, sb = os.fstat(a), os.fstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            if os.name == "nt":
                time.sleep(0.01)
            else:
                select.select([], [fd], [])
            continue
        view = view[written:]


def connect(target: Target, timeout: float = _CONNECT_TIMEOUT) -> socket.socket:
    """
    A connection to the target, trying each of its addresses.

    "No route to host" and its kin come and go while the network is still
    finding a neighbour's hardware address, so those are retried within the
    timeout. The error names every address and what went wrong with it.
    """
    try:
        found = socket.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise RemoteError(f"cannot reach {target}: {e.strerror or e}") from None
    deadline = time.monotonic() + timeout
    errors: dict[str, str] = {}
    instant_unreachable = True  # every attempt failed with "no route" at once
    while True:
        transient = False
        for family, kind, proto, _, address in found:
            shown = f"[{address[0]}]" if family == socket.AF_INET6 else address[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock = socket.socket(family, kind, proto)
            started = time.monotonic()
            try:
                sock.settimeout(remaining)
                sock.connect(address)
                # The handshake gets its own time, not what is left of the connect's.
                sock.settimeout(_HANDSHAKE_TIMEOUT)
                _keep_alive(sock)
                return sock
            except OSError as e:
                sock.close()
                errors[shown] = e.strerror or str(e)
                number = getattr(e, "errno", None)
                transient |= number in _TRANSIENT
                instant_unreachable &= number == errno.EHOSTUNREACH and time.monotonic() - started < 0.05
        if not transient or time.monotonic() + 0.5 > deadline:
            break
        time.sleep(0.5)
    tried = "; ".join(f"{address}: {error}" for address, error in errors.items()) or "no address"
    hint = ""
    if errors and instant_unreachable and sys.platform == "darwin":
        # macOS answers at once with "No route to host" for a program that
        # may not use the local network, while ping still gets through.
        hint = ("\nthis is what macOS says when the app running this has no Local Network permission: "
                "System Settings > Privacy & Security > Local Network")
    raise RemoteError(f"cannot reach {target}: {tried}{hint}")


def run_client(target: Target, argv: Sequence[str]) -> int:
    """Run a command line on a server. Returns its exit code."""
    sock = connect(target)
    try:
        return _Client(sock, target).run(list(argv))
    finally:
        sock.close()


def _read_stdin(chunks: queue.Queue) -> None:
    """Read stdin until its end, into a queue: b"" is the end."""
    while True:
        try:
            data = os.read(0, _CHUNK)
        except OSError:
            data = b""
        chunks.put(data)
        if not data:
            return


class _Client:
    def __init__(self, sock: socket.socket, target: Target) -> None:
        self.channel = _Channel(sock)
        self.target = target
        self.code: int | None = None
        self.signals: list[int] = []
        self.last_interrupt: float | None = None
        self.pty = True  # whether the server gives commands a terminal
        self.terminal = 2  # the client's terminal, or stderr: where TTY output goes
        self.raw = False  # whether the client's terminal passes keys on as they come
        self.credit = 0  # the input the server has asked for
        self.unsent = bytearray()  # input read, waiting for the server to ask for it
        self.send_eof = False
        self.escape = 1  # how far the keys typed are into Enter ~ .

    def run(self, argv: list[str]) -> int:
        try:
            self.handshake(argv)
        except socket.timeout:
            raise RemoteError(f"{self.target} does not answer") from None
        except (_ProtocolError, OSError, EOFError) as e:
            raise RemoteError(f"{self.target}: {e}") from None

        flags = [os.isatty(fd) for fd in (0, 1, 2)]
        self.terminal = next((fd for fd in (1, 2, 0) if flags[fd]), 2)
        saved = None
        if flags[0] and self.pty and termios is not None and not _like_windows():
            # The command's terminal on the server does the echoing and
            # the line editing; this one passes the keys on as they come.
            try:
                saved = termios.tcgetattr(0)
                tty.setraw(0, termios.TCSADRAIN)
            except termios.error:
                saved = None
        self.raw = saved is not None
        request = {
            "argv": argv,
            "tty": flags,
            "raw": self.raw,
            "same": not flags[1] and not flags[2] and _same_file(1, 2),
            "env": {k: os.environ[k] for k in TERMINAL_VARIABLES if k in os.environ},
        }
        size = self.size()
        if size:
            request["size"] = size

        handlers = {}

        def note(sig, frame):
            self.signals.append(sig)

        def leave(sig, frame):
            raise SystemExit(128 + sig)

        wanted = [(signal.SIGINT, note), (signal.SIGTERM, leave)]
        for name, handler in (("SIGWINCH", note), ("SIGHUP", leave)):
            if hasattr(signal, name):
                wanted.append((getattr(signal, name), handler))
        for sig, handler in wanted:
            try:
                handlers[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):  # not the main thread
                pass
        try:
            self.channel.write(RUN, request)
            self.channel.sock.setblocking(False)
            return self.relay(stdin_is_tty=flags[0])
        finally:
            if saved is not None:
                termios.tcsetattr(0, termios.TCSADRAIN, saved)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    def size(self) -> dict | None:
        try:
            columns, lines = os.get_terminal_size(self.terminal)
        except OSError:
            return None
        return {"cols": columns, "rows": lines}

    def handshake(self, argv: list[str]) -> None:
        channel = self.channel
        challenge = _json(channel.expect(CHALLENGE))
        if challenge.get("proto") != PROTOCOL:
            raise RemoteError(f"{self.target} speaks fireaid remote protocol {challenge.get('proto')}, "
                              f"this client speaks {PROTOCOL}")
        self.pty = bool(challenge.get("pty", True))
        auth = bool(challenge.get("auth"))
        if not auth and self.target.password is not None:
            # A server that does not know the password may not be the one meant.
            raise RemoteError(f"{self.target} asks for no password, so the command is not sent to it")
        channel.write(HELLO, {"proto": PROTOCOL})
        if auth:
            password = self.target.password or ""
            theirs, ours = _nonce(challenge.get("nonce")), secrets.token_bytes(32)
            channel.write(AUTH, {"mac": _mac(password, "client", theirs, ours), "nonce": ours.hex()})
        kind, payload = channel.read()
        if kind == REFUSED:
            self.refused(payload)
        if kind != AUTH_OK:
            raise _ProtocolError(f"unexpected message {kind}")
        if auth and not _proves(_json(payload).get("mac"), _mac(password, "server", theirs, ours)):
            raise RemoteError(f"{self.target} does not know the password, so the command is not sent to it")

    def refused(self, payload: bytes) -> None:
        message = _json(payload)
        raise RemoteError(f"{self.target}: {message.get('message')}", _integer(message, "code", 1))

    def relay(self, stdin_is_tty: bool) -> int:
        channel = self.channel
        sock = channel.sock
        threaded = _like_windows()  # stdin cannot be selected on Windows
        chunks: queue.Queue = queue.Queue()
        if threaded:
            threading.Thread(target=_read_stdin, args=(chunks,), daemon=True).start()
        reading_stdin = True

        def wanted() -> bool:
            """Whether to read stdin: the server has asked for input, and what was read has gone."""
            return reading_stdin and self.credit > 0 and not self.unsent and len(channel.outgoing) < _BACKLOG

        try:
            while self.code is None:
                self.handle_signals()
                stdin = [0] if wanted() and not threaded else []
                readable, writable, _ = select.select(
                    [sock, *stdin], [sock] if channel.outgoing else [], [], _POLL if threaded else _TICK)
                if sock in writable:
                    channel.flush()
                for kind, payload in channel.receive():
                    self.on_message(kind, payload)
                if 0 in readable:
                    try:
                        data = os.read(0, _CHUNK)
                    except OSError:
                        data = b""
                    reading_stdin = self.take_input(data, stdin_is_tty)
                self.send_input()
                while threaded and wanted():
                    try:
                        data = chunks.get_nowait()
                    except queue.Empty:
                        break
                    reading_stdin = self.take_input(data, stdin_is_tty)
                    self.send_input()
                channel.flush()
        except EOFError as e:
            raise RemoteError(f"{self.target}: {str(e) or 'connection closed'}") from None
        except BrokenPipeError:
            # Our own output has gone away. The server stops the command.
            return 128 + getattr(signal, "SIGPIPE", 13)
        return self.code

    def take_input(self, data: bytes, stdin_is_tty: bool) -> bool:
        """Take stdin's data, to send as the server asks for it. Returns whether there is more to read."""
        if data:
            if self.raw and self.escaped(data):
                raise RemoteError(f"disconnected from {self.target}", 255)
            self.unsent += data
            return True
        self.send_eof = not stdin_is_tty
        return False

    def escaped(self, keys: bytes) -> bool:
        """
        Whether the keys typed include Enter ~ . , the way out where Ctrl-C
        is a key for the server's terminal, as in ssh.
        """
        for key in keys:
            if self.escape == 2 and key == ord("."):
                return True
            self.escape = 2 if self.escape == 1 and key == ord("~") else int(key in b"\r\n")
        return False

    def send_input(self) -> None:
        if self.unsent and self.credit > 0:
            size = min(len(self.unsent), self.credit)
            self.channel.queue(STDIN, bytes(self.unsent[:size]))
            del self.unsent[:size]
            self.credit -= size
        if self.send_eof and not self.unsent:
            self.channel.queue(STDIN_EOF)
            self.send_eof = False

    def handle_signals(self) -> None:
        while self.signals:
            sig = self.signals.pop(0)
            if sig == getattr(signal, "SIGWINCH", None):
                size = self.size()
                if size:
                    self.channel.queue(WINCH, size)
            elif sig == signal.SIGINT:
                now = time.monotonic()
                if self.last_interrupt is not None and now - self.last_interrupt < 1.0:
                    raise SystemExit(128 + signal.SIGINT)  # the second one is for us
                self.last_interrupt = now
                self.channel.queue(SIGNAL, {"name": "INT"})

    def on_message(self, kind: int, payload: bytes) -> None:
        if kind == STDOUT:
            _write_all(1, payload)
        elif kind == STDERR:
            _write_all(2, payload)
        elif kind == TTY:
            _write_all(self.terminal, payload)
        elif kind == EXIT:
            self.code = _integer(_json(payload), "code", 1)
        elif kind == CREDIT:
            self.credit += max(0, _integer(_json(payload), "bytes", 0))
        elif kind == REFUSED:
            self.refused(payload)
        else:
            raise _ProtocolError(f"unexpected message {kind}")
