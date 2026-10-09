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

import hashlib
import hmac
import json
import os
import queue
import secrets
import select
import selectors
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

PROTOCOL = 1
FLAG = "--remote"

# Set in the environment of every command the server runs.
CHILD_VARIABLE = "FIREAID_REMOTE_CHILD"

# Binds the server to this address instead of the one the password
# selects. For the tests, which must not trigger a firewall dialog.
_TEST_BIND_VARIABLE = "FIREAID_REMOTE_TEST_BIND"

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
    "PAGER",
)

# Messages. A frame is the length of what follows, the type, and the
# payload: JSON for control messages, bytes for data.
HELLO, AUTH, RUN, STDIN, STDIN_EOF, WINCH, SIGNAL = 1, 2, 3, 4, 5, 6, 7
CHALLENGE, AUTH_OK, REFUSED, STDOUT, STDERR, EXIT, TTY = 129, 130, 131, 132, 133, 134, 135

_HEADER = struct.Struct(">IB")
_MAX_FRAME = 1 << 20
_CHUNK = 1 << 16
_BACKLOG = 1 << 20  # buffered output beyond which reading pauses
_SIGNALS = {name: getattr(signal, "SIG" + name) for name in ("INT", "TERM", "HUP", "QUIT")
            if hasattr(signal, "SIG" + name)}
_CONTROL_C_EXIT = 0xC000013A  # how Windows reports a process ended by Ctrl-Break

_CONNECT_TIMEOUT = 5.0
_HANDSHAKE_TIMEOUT = 10.0
_TICK = 0.2


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


def environment_variable(prog: str, configured: str | None = None) -> str:
    """The variable a client takes --remote from: PROG_REMOTE."""
    if configured:
        return configured
    stem = os.path.splitext(prog)[0]
    return "".join(c if c.isalnum() else "_" for c in stem.upper()) + "_REMOTE"


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

    A deny entry names a command path, as words. It covers the command
    lines that start with that path, help for them included. The server
    command itself is denied unless help for it is asked for.
    """
    words = list(argv[: argv.index("--")]) if "--" in argv else list(argv)
    if any(w == FLAG or w.startswith(FLAG + "=") for w in words):
        return f"{FLAG} cannot be passed on to the server"
    if "--" in argv:
        # Fire's own flags follow the last separator. Its interactive
        # mode is a Python shell, which no deny list could contain.
        last = len(argv) - 1 - argv[::-1].index("--")
        for flag in argv[last + 1 :]:
            if flag == "-i" or (len(flag) > 2 and "--interactive".startswith(flag)):
                return "Fire's interactive mode is not available remotely"

    def fold(text: str) -> list[str]:
        return [w.replace("-", "_") for w in text.split()]

    path = [w for w in words if not w.startswith("-")]
    help_asked = path[:1] == ["help"] or ("--" in argv and "--help" in argv) or argv[-1:] in (["--help"], ["-h"])
    if path[:1] == ["help"]:
        path = path[1:]
    path = fold(" ".join(path))

    entries = list(deny) if help_asked else ["server", *deny]
    for entry in entries:
        steps = fold(entry)
        if steps and path[: len(steps)] == steps:
            return f"'{entry}' is not available remotely"
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


def bind_address(password: str | None) -> str:
    """Without a password, only this computer may connect."""
    return "0.0.0.0" if password is not None else "127.0.0.1"


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
        end = 4 + length
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
                raise _ProtocolError("connection closed")
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
        """Send what the socket takes now. Raises OSError if the peer is gone."""
        while self.outgoing:
            try:
                sent = self.sock.send(self.outgoing)
            except (BlockingIOError, InterruptedError):
                return
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
        except ConnectionError:
            raise EOFError from None
        if data is not None:
            if not data:
                raise EOFError
            self.incoming += data
        return list(_parse(self.incoming))


def _mac(password: str, nonce: bytes) -> str:
    return hmac.new(password.encode(), nonce, hashlib.sha256).hexdigest()


def _set_size(fd: int, size: Any) -> None:
    try:
        rows, cols = int(size["rows"]), int(size["cols"])
    except (KeyError, TypeError, ValueError):
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


def serve(launch: Launch, deny: Sequence[str], prog: str, variable: str, port: Any, password: Any) -> None:
    """Run the server until interrupted."""
    if launch.error:
        raise RemoteError(launch.error)
    if isinstance(password, bool) or not isinstance(password, (str, int, type(None))):
        raise RemoteError("give the password as a string, as in --password='\"1e3\"'", 2)
    if password is not None:
        password = str(password)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise RemoteError(f"{port!r} is not a port", 2)

    host = os.environ.get(_TEST_BIND_VARIABLE) or bind_address(password)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
        listener.listen(5)
    except OSError as e:
        listener.close()
        raise RemoteError(f"cannot listen on {host}:{port}: {e.strerror or e}") from None

    def stop(sig, frame):
        raise SystemExit(0)

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, _SIGNALS.get("HUP")) if sig}
    terminals = "" if _pty_available() else ", commands get pipes"
    print(f"{prog}: serving on {host}:{listener.getsockname()[1]}{terminals}", file=sys.stderr, flush=True)
    listener.settimeout(1.0)  # so that Ctrl-C gets through on Windows
    try:
        while True:
            try:
                sock, peer = listener.accept()
            except socket.timeout:
                continue
            try:
                _Session(sock, launch, deny, prog, variable, password).run()
            except Exception as e:  # one client must not stop the server
                print(f"{prog}: {peer[0]}: {e}", file=sys.stderr, flush=True)
            finally:
                sock.close()
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


class _Session:
    """One client, one command."""

    def __init__(self, sock: socket.socket, launch: Launch, deny: Sequence[str], prog: str, variable: str,
                 password: str | None) -> None:
        self.channel = _Channel(sock)
        self.peer = sock.getpeername()[0]
        self.launch = launch
        self.deny = deny
        self.prog = prog
        self.variable = variable
        self.password = password

    def run(self) -> None:
        sock = self.channel.sock
        sock.settimeout(_HANDSHAKE_TIMEOUT)
        try:
            request = self.handshake()
        except _ProtocolError as e:
            self.refuse(str(e), 2)
            return
        except (socket.timeout, ConnectionError):
            return
        if request is None:
            return
        sock.setblocking(False)
        runner = _Command if _pty_available() else _PipeCommand
        runner(self.channel, self.launch, request, self.variable).run()

    def refuse(self, message: str, code: int) -> None:
        print(f"{self.prog}: {self.peer}: {message}", file=sys.stderr, flush=True)
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
        if self.password is not None:
            mac = _json(channel.expect(AUTH)).get("mac")
            if not isinstance(mac, str) or not hmac.compare_digest(mac, _mac(self.password, nonce)):
                self.refuse("authentication failed", 1)
                return None
        channel.write(AUTH_OK)

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


def _environment(request: dict, variable: str) -> dict:
    # The client's terminal variables replace ours, and the program's
    # own --remote variable would send the command on again.
    env = {k: v for k, v in os.environ.items() if k not in TERMINAL_VARIABLES and k != variable}
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
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill(process: subprocess.Popen | None) -> None:
    """End the command, and what it started, as its client has gone."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        steps = ((process.terminate, 2.0), (process.kill, None))
    else:
        steps = ((lambda: os.killpg(process.pid, signal.SIGHUP), 2.0),
                 (lambda: os.killpg(process.pid, signal.SIGKILL), None))
    for step, wait in steps:
        try:
            step()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            process.wait(wait)
            return
        except subprocess.TimeoutExpired:
            pass


def _send_exit(channel: _Channel, returncode: int) -> None:
    channel.queue(EXIT, {"code": _exit_code(returncode)})
    sock = channel.sock
    sock.setblocking(True)
    sock.settimeout(_HANDSHAKE_TIMEOUT)
    try:
        sock.sendall(channel.outgoing)
    except OSError:
        pass


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
    """A command line running in a child process, relayed to the client."""

    def __init__(self, channel: _Channel, launch: Launch, request: dict, variable: str) -> None:
        self.channel = channel
        self.variable = variable
        self.launch = launch
        self.request = request
        self.outputs: dict[int, int] = {}  # fd -> message type
        self.master: int | None = None
        self.stdin: int | None = None  # where the client's input goes
        self.stdin_is_pipe = False
        self.pending = bytearray()
        self.stdin_eof = False
        self.process: subprocess.Popen | None = None

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
            self.process = subprocess.Popen(
                [*self.launch.prefix, *self.request["argv"]],
                stdin=child[0],
                stdout=child[1],
                stderr=child[2],
                cwd=self.launch.cwd,
                env=_environment(self.request, self.variable),
                start_new_session=True,
                preexec_fn=_take_terminal if slave is not None else None,
            )
        finally:
            for fd in close:
                os.close(fd)
        for fd in {*self.outputs, self.stdin}:
            os.set_blocking(fd, False)

    def run(self) -> None:
        try:
            self.spawn()
            gone = self.relay()
        except BaseException:
            self.kill()
            raise
        finally:
            for fd in {*self.outputs, self.stdin} - {None}:
                os.close(fd)
        if gone:
            self.kill()
            return
        _send_exit(self.channel, self.process.wait())

    def kill(self) -> None:
        _kill(self.process)

    def relay(self) -> bool:
        """Relay until the command is done. Returns whether the client went away."""
        channel = self.channel
        selector = selectors.SelectSelector()
        try:
            while self.outputs or self.process.poll() is None:
                _watch(selector, channel.sock,
                       selectors.EVENT_READ | (selectors.EVENT_WRITE if channel.outgoing else 0))
                for fd in set(self.outputs) | {self.stdin} - {None}:
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
                        self.write_input(selector)
                for kind, payload in channel.receive():
                    self.on_message(kind, payload)
                self.close_input_if_done(selector)
        except (EOFError, ConnectionError, BrokenPipeError):
            return True
        except _ProtocolError:
            return True
        finally:
            selector.close()
        return False

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
                self.pending.clear()
        if fd != self.stdin:
            os.close(fd)

    def write_input(self, selector: selectors.BaseSelector) -> None:
        try:
            written = os.write(self.stdin, self.pending)
        except BlockingIOError:
            return
        except OSError:  # nobody reads it any more
            self.pending.clear()
            self.stdin_eof = True
            return
        del self.pending[:written]

    def close_input_if_done(self, selector: selectors.BaseSelector) -> None:
        if self.stdin_is_pipe and self.stdin is not None and self.stdin_eof and not self.pending:
            _watch(selector, self.stdin, 0)
            os.close(self.stdin)
            self.stdin = None

    def on_message(self, kind: int, payload: bytes) -> None:
        if kind == STDIN:
            if self.stdin is not None and not self.stdin_eof:
                self.pending += payload
        elif kind == STDIN_EOF:
            self.stdin_eof = True
        elif kind == WINCH:
            if self.master is not None:
                _set_size(self.master, _json(payload))
        elif kind == SIGNAL:
            _interrupt(self.process, str(_json(payload).get("name")))
        else:
            raise _ProtocolError(f"unexpected message {kind}")


class _PipeCommand:
    """
    A command line running in a child process with pipes for its stdio.

    For systems without pseudo-terminals, Windows above all, where pipes
    cannot be watched together with the socket either: threads read the
    command's output and write its input, the main loop minds the socket.
    """

    def __init__(self, channel: _Channel, launch: Launch, request: dict, variable: str) -> None:
        self.channel = channel
        self.launch = launch
        self.request = request
        self.variable = variable
        self.process: subprocess.Popen | None = None
        self.output: queue.Queue = queue.Queue(maxsize=64)  # (kind, data), data None at the end
        self.input: queue.Queue = queue.Queue()  # data, None at the end
        self.readers = 0

    def spawn(self) -> None:
        options: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
            else {"start_new_session": True}
        self.process = subprocess.Popen(
            [*self.launch.prefix, *self.request["argv"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if self.request.get("same") else subprocess.PIPE,
            cwd=self.launch.cwd,
            env=_environment(self.request, self.variable),
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
                if not data:
                    break
                self.output.put((kind, data))
        except (OSError, ValueError):
            pass
        finally:
            self.output.put((kind, None))

    def write(self) -> None:
        stdin = self.process.stdin
        try:
            while True:
                data = self.input.get()
                if data is None:
                    break
                stdin.write(data)
                stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def run(self) -> None:
        try:
            self.spawn()
            gone = self.relay()
        except BaseException:
            _kill(self.process)
            raise
        finally:
            self.input.put(None)
        if gone:
            _kill(self.process)
            return
        _send_exit(self.channel, self.process.wait())

    def relay(self) -> bool:
        """Relay until the command is done. Returns whether the client went away."""
        channel = self.channel
        sock = channel.sock
        try:
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
                _, writable, _ = select.select([sock], [sock] if channel.outgoing else [], [], 0.05)
                if writable:
                    channel.flush()
                for kind, payload in channel.receive():
                    self.on_message(kind, payload)
        except (EOFError, ConnectionError, _ProtocolError):
            return True
        return False

    def on_message(self, kind: int, payload: bytes) -> None:
        if kind == STDIN:
            self.input.put(payload)
        elif kind == STDIN_EOF:
            self.input.put(None)
        elif kind == WINCH:
            pass  # no terminal to resize
        elif kind == SIGNAL:
            _interrupt(self.process, str(_json(payload).get("name")))
        else:
            raise _ProtocolError(f"unexpected message {kind}")


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


def run_client(target: Target, argv: Sequence[str]) -> int:
    """Run a command line on a server. Returns its exit code."""
    try:
        sock = socket.create_connection((target.host, target.port), timeout=_CONNECT_TIMEOUT)
    except OSError as e:
        raise RemoteError(f"cannot reach {target}: {e.strerror or e}") from None
    try:
        return _Client(sock, target).run(list(argv))
    finally:
        sock.close()


def _read_stdin(lines: queue.Queue) -> None:
    """Read stdin until its end, into a queue: b"" is the end."""
    while True:
        try:
            data = os.read(0, _CHUNK)
        except OSError:
            data = b""
        lines.put(data)
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

    def run(self, argv: list[str]) -> int:
        try:
            self.handshake(argv)
        except socket.timeout:
            raise RemoteError(f"{self.target} does not answer") from None
        except (_ProtocolError, ConnectionError) as e:
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
        request = {
            "argv": argv,
            "tty": flags,
            "raw": saved is not None,
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
        channel.write(HELLO, {"proto": PROTOCOL})
        if challenge.get("auth"):
            try:
                nonce = bytes.fromhex(challenge.get("nonce", ""))
            except (TypeError, ValueError):
                raise _ProtocolError("bad challenge") from None
            channel.write(AUTH, {"mac": _mac(self.target.password or "", nonce)})
        kind, payload = channel.read()
        if kind == REFUSED:
            self.refused(payload)
        if kind != AUTH_OK:
            raise _ProtocolError(f"unexpected message {kind}")

    def refused(self, payload: bytes) -> None:
        refusal_ = _json(payload)
        raise RemoteError(f"{self.target}: {refusal_.get('message')}", int(refusal_.get("code", 1)))

    def relay(self, stdin_is_tty: bool) -> int:
        channel = self.channel
        sock = channel.sock
        threaded = _like_windows()  # stdin cannot be selected on Windows
        lines: queue.Queue = queue.Queue()
        if threaded:
            threading.Thread(target=_read_stdin, args=(lines,), daemon=True).start()
        reading_stdin = True
        try:
            while self.code is None:
                self.handle_signals()
                stdin = [0] if reading_stdin and not threaded and len(channel.outgoing) < _BACKLOG else []
                readable, writable, _ = select.select(
                    [sock, *stdin], [sock] if channel.outgoing else [], [], 0.05 if threaded else _TICK)
                if sock in writable:
                    channel.flush()
                for kind, payload in channel.receive():
                    self.on_message(kind, payload)
                if 0 in readable:
                    try:
                        data = os.read(0, _CHUNK)
                    except OSError:
                        data = b""
                    reading_stdin = self.relay_input(data, stdin_is_tty)
                while threaded and reading_stdin and len(channel.outgoing) < _BACKLOG:
                    try:
                        data = lines.get_nowait()
                    except queue.Empty:
                        break
                    reading_stdin = self.relay_input(data, stdin_is_tty)
                channel.flush()
        except EOFError:
            raise RemoteError(f"{self.target} closed the connection") from None
        except BrokenPipeError:
            # Our own output has gone away. The server stops the command.
            return 128 + getattr(signal, "SIGPIPE", 13)
        except ConnectionError as e:
            raise RemoteError(f"{self.target}: {e}") from None
        return self.code

    def relay_input(self, data: bytes, stdin_is_tty: bool) -> bool:
        """Pass stdin's data on. Returns whether there is more to read."""
        if data:
            self.channel.queue(STDIN, data)
            return True
        if not stdin_is_tty:
            self.channel.queue(STDIN_EOF)
        return False

    def handle_signals(self) -> None:
        while self.signals:
            sig = self.signals.pop(0)
            if sig == signal.SIGWINCH:
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
            self.code = int(_json(payload).get("code", 1))
        elif kind == REFUSED:
            self.refused(payload)
        else:
            raise _ProtocolError(f"unexpected message {kind}")
