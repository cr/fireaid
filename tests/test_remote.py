"""
Remote control: a command line run through a server behaves as it does
locally, and the server runs only what it may.

On Windows, which has no pseudo-terminals, a server gives commands pipes.
FIREAID_REMOTE_TEST_NO_PTY makes a server or client anywhere behave like
one on Windows, so that the two kinds are tested against each other.
"""

import errno
import importlib.util
import inspect
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

import fireaid
from fireaid import remote
from support import TESTS, unix
from support import environment as program_environment
from support import on_terminal as run_on_terminal

try:
    import fcntl
    import pty
    import struct
    import termios
except ImportError:  # Windows
    pass

PASSWORD = "p@ss word"


def environment(component, **variables):
    """The test program's, remote-controlled, without a CLI_REMOTE of our own."""
    env = program_environment("fireaid", component, FIREAID_TEST_REMOTE="1")
    env.pop("CLI_REMOTE", None)
    env.update(variables)
    return env


def run(argv, component="CLI", input=None, **variables):
    """Run the test program, remote-controlled, without a terminal."""
    variables.setdefault("FIREAID_REMOTE_TEST_NO_PTY", "")
    completed = subprocess.run(
        [sys.executable, "cli.py", *argv],
        input=input,
        stdin=None if input is not None else subprocess.DEVNULL,
        capture_output=True,
        env=environment(component, **variables),
        cwd=TESTS,
        timeout=30,
    )
    # Newlines as text mode would give them, since the program prints in text mode.
    return completed.returncode, completed.stdout.decode().replace("\r\n", "\n"), \
        completed.stderr.decode().replace("\r\n", "\n")


class Log:
    """
    The server's log, read as events.

    The server's wording is pinned here and nowhere else, so that a
    rewording changes one place. Tests ask whether something happened.
    """

    SERVING = re.compile(r"serving on (?P<host>\S+?):(?P<port>\d+)(?P<notes>[^\n]*)")

    def __init__(self, path):
        self.path = path

    @property
    def text(self):
        return self.path.read_text()

    def wait(self, happened, timeout=10):
        """Wait until happened(text) holds; return the text then."""
        deadline = time.monotonic() + timeout
        while not happened(self.text) and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.text

    # -- what the program's setup hook saw (tests/cli.py prints it)

    def setup_debug(self):
        m = re.search(r"^SETUP debug=(True|False)$", self.text, re.M)
        return None if m is None else m.group(1) == "True"

    # -- the server's start

    def serving(self):
        return self.SERVING.search(self.text)

    def port(self):
        return int(self.serving().group("port"))

    def host(self):
        return self.serving().group("host")

    def without_password(self):
        return "no password" in self.serving().group("notes")

    def pipes_only(self):
        return "pipes" in self.serving().group("notes")

    # The two warnings name the flag that silences them; the prose may change.

    def warned_no_password(self):
        return "--password" in self.text

    def warned_open_network(self):
        return "--bind 127.0.0.1" in self.text

    # -- clients and commands

    @staticmethod
    def connected(text):
        return "connected" in text

    @staticmethod
    def finished(text):
        return "done" in text

    @staticmethod
    def gone_in_handshake(text):
        return "gone during the handshake" in text

    @staticmethod
    def ran(text, words):
        return f"runs: {words}" in text

    @staticmethod
    def ran_as(text, shown):
        """The info line names the command as shown, and nothing more."""
        return f"runs: {shown}\n" in text

    @staticmethod
    def exited(text, code):
        return f"exit {code} after" in text

    @staticmethod
    def launched(text, words):
        return "as: " in text and f" {words} in " in text

    @staticmethod
    def terminal(text, flags):
        return f"tty {flags}" in text

    @staticmethod
    def waiting(text):
        """How many clients have waited for a running command, as --debug logs it."""
        return text.count("waits for the command running now")

    @staticmethod
    def refused(text, path):
        return f"'{path}' is not available remotely" in text


class Said:
    """
    What the client says on stderr, and what help shows.

    Their wording is pinned here and nowhere else, as the server's is in
    Log, so that a rewording changes one place. Tests ask whether
    something was said.
    """

    # -- the client's stderr

    @staticmethod
    def refused(err):
        """The server refused the command line, and the client named the server."""
        return re.fullmatch(r"cli\.py: 127\.0\.0\.1:\d+: [^\n]*(is|are) not available remotely[^\n]*\n", err) is not None

    @staticmethod
    def refused_after_separator(err):
        return Said.refused(err) and "after --" in err

    @staticmethod
    def authentication_failed(err, port):
        return err == f"cli.py: 127.0.0.1:{port}: authentication failed\n"

    @staticmethod
    def not_sent_without_password(err, target):
        """The client has a password, the server asks for none: the command does not go."""
        return err == f"cli.py: {target} asks for no password, so the command is not sent to it\n"

    @staticmethod
    def not_sent_to_impostor(err, target):
        return err == f"cli.py: {target} does not know the password, so the command is not sent to it\n"

    @staticmethod
    def busy(err):
        return re.fullmatch(r"cli\.py: 127\.0\.0\.1:\d+: busy with another client's command; try again later\n",
                            err) is not None

    @staticmethod
    def disconnected(output, target):
        return f"cli.py: disconnected from {target}\n" in output

    @staticmethod
    def unreachable(err, target):
        return err.startswith(f"cli.py: cannot reach {target}: ")

    @staticmethod
    def needs_value(err, flag):
        return err.endswith(f"cli.py: {flag} needs a value\n")

    # -- help

    @staticmethod
    def lists_command(out, name):
        """The command is listed, with a summary line under it."""
        return re.search(rf"^ +{re.escape(name)}\n +\S", out, re.M) is not None

    @staticmethod
    def lists_flag(out, name):
        return f"--{name}=" in out

    @staticmethod
    def local_only(out):
        """The commands the help says only run locally."""
        m = re.search(r"only run locally: ([^\n]*)\.", out)
        return [] if m is None else [name.strip() for name in m.group(1).split(",")]


class Server:
    def __init__(self, component, password, log, pipes=False, debug=False, **variables):
        # Loopback, so that no firewall asks about the tests.
        # The password is True for the usual one, or a string of its own.
        secret = password if isinstance(password, str) else PASSWORD
        argv = ["server", "--port", "0", "--bind", "127.0.0.1"] + ([f"--password={secret}"] if password else [])
        argv += ["--debug"] if debug else []
        self.log = Log(log)
        self.log_file = log.open("w")
        self.process = subprocess.Popen(
            [sys.executable, "cli.py", *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self.log_file,
            env=environment(component, FIREAID_REMOTE_TEST_NO_PTY="1" if pipes else "", **variables),
            cwd=TESTS,
        )
        deadline = time.monotonic() + 10
        while not self.log.serving():
            assert self.process.poll() is None, self.log.text
            assert time.monotonic() < deadline, "the server does not start"
            time.sleep(0.05)
        self.pipes = self.log.pipes_only()
        self.port = self.log.port()
        self.target = f"{secret}@127.0.0.1:{self.port}" if password else f"127.0.0.1:{self.port}"

    def stop(self):
        self.process.terminate()
        self.process.wait(10)
        self.log_file.close()


def ended(process):
    """Kill a process if it still runs, wait for it, and close its pipes."""
    if process.poll() is None:
        process.kill()
    process.wait(10)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """A server for a component, with or without a password, started once."""
    servers = {}

    def server(component="CLI", password=False, pipes=False, debug=False):
        key = (component, password, pipes, debug)
        if key not in servers:
            servers[key] = Server(component, password, tmp_path_factory.mktemp("server") / "log", pipes, debug)
        return servers[key]

    yield server
    for s in servers.values():
        s.stop()


# -- What the client takes from the command line


def test_exit_code_mapping():
    """How a command's return code reaches the client."""
    assert remote._exit_code(0) == 0
    assert remote._exit_code(7) == 7
    assert remote._exit_code(-signal.SIGINT) == 128 + signal.SIGINT  # ended by a signal
    assert remote._exit_code(remote._CONTROL_C_EXIT) == 128 + signal.SIGINT  # Windows Ctrl-Break
    assert remote._exit_code(256) == 1 and remote._exit_code(1000) == 1  # out of a byte's range


@pytest.mark.parametrize("hex_, ok", [("ab" * 16, True), ("ab" * 15, False), ("", False), ("xy", False), (None, False)])
def test_nonce_must_be_long_enough(hex_, ok):
    if ok:
        assert remote._nonce(hex_) == bytes.fromhex(hex_)
    else:
        with pytest.raises(remote._ProtocolError):
            remote._nonce(hex_)


def test_frame_length_bounds():
    """A frame's length must be within 1.._MAX_FRAME; the parser rejects the rest."""
    import struct
    for length in (0, remote._MAX_FRAME + 1):
        with pytest.raises(remote._ProtocolError):
            list(remote._parse(bytearray(struct.pack(">IB", length, remote.STDOUT))))
    biggest = remote._frame(remote.STDOUT, b"x" * (remote._MAX_FRAME - 1))
    assert list(remote._parse(bytearray(biggest))) == [(remote.STDOUT, b"x" * (remote._MAX_FRAME - 1))]
    with pytest.raises(ValueError):
        remote._frame(remote.STDOUT, b"x" * remote._MAX_FRAME)  # one too large to frame


@pytest.mark.parametrize(
    "text, host, port, password",
    [
        ("boat", "boat", 4247, None),
        ("boat:1", "boat", 1, None),
        ("pw@boat", "boat", 4247, "pw"),
        ("p@w@boat:2", "boat", 2, "p@w"),
        ("@boat", "boat", 4247, ""),
        ("[::1]:3", "::1", 3, None),
        ("[::1]", "::1", 4247, None),
    ],
)
def test_target(text, host, port, password):
    assert remote.parse_target(text, 4247) == remote.Target(host, port, password)


@pytest.mark.parametrize("text", ["", ":1", "boat:x", "boat:0", "boat:65536", "::1", "[::1", "[::1]x"])
def test_bad_target(text):
    with pytest.raises(ValueError):
        remote.parse_target(text, 4247)


@pytest.mark.parametrize(
    "command, rest",
    [
        ("--remote h get x", "get x"),
        ("get x --remote h", "get x"),
        ("get --remote=h x", "get x"),
        ("get x -- --remote h", "get x -- --remote h"),
    ],
)
def test_flag_anywhere_before_separator(command, rest, monkeypatch):
    monkeypatch.delenv("T_REMOTE", raising=False)
    target, words = remote.take_flag(command.split(), "t", 4247, "T_REMOTE")
    assert words == rest.split()
    assert (target is not None) == (command != "get x -- --remote h")


def test_flag_from_environment(monkeypatch):
    monkeypatch.setenv("T_REMOTE", "env")
    assert remote.take_flag(["get"], "t", 1, "T_REMOTE")[0].host == "env"
    assert remote.take_flag(["get", "--remote", "flag"], "t", 1, "T_REMOTE")[0].host == "flag"
    assert remote.take_flag(["get", "--remote="], "t", 1, "T_REMOTE")[0] is None
    assert remote.take_flag(["server"], "t", 1, "T_REMOTE")[0] is None


@pytest.mark.parametrize("command", ["--remote a get --remote b", "get --remote", "get --remote -- x", "get --remote=h:x"])
def test_bad_flag(command):
    with pytest.raises(remote.RemoteError) as e:
        remote.take_flag(command.split(), "t", 1, "T_REMOTE")
    assert e.value.code == 2


def test_variable_name():
    assert remote.environment_variable("mvconnect") == "MVCONNECT_REMOTE"
    assert remote.environment_variable("cli.py") == "CLI_REMOTE"
    assert remote.environment_variable("my-tool") == "MY_TOOL_REMOTE"
    assert remote.environment_variable("x", "OWN") == "OWN"


@pytest.mark.parametrize(
    "command, refused",
    [
        ("foo x", False),
        ("help", False),
        ("help server", False),
        ("server --help", False),
        ("server", True),
        ("server --port 1", True),
        ("firmware", True),
        ("firmware flash x", True),
        ("firmware --help", True),
        ("help firmware", True),
        ("firmware -- --help", True),
        ("db drop", True),
        ("db-drop", False),
        ("db list", False),
        ("foo --remote x", True),
        ("foo -- --remote x", True),  # nothing may follow the last separator
        ("foo -- --help", True),
        ("foo -- --interactive", True),
        ("foo -- -i", True),
        ("foo -- -vi", True),
        ("foo -- --trace", True),
        ("foo -- x", True),
        ("foo -- -i -- x", True),
        ("foo --", False),  # a trailing separator with nothing after it
        ("foo -- --", False),
        ("foo -- -i --", False),  # ... whatever came before it is the command's
        ("foo x server", True),  # Fire chains commands: a method may return the tool
        ("foo x server --help", True),
        ("foo x help server", True),
        ("help server --port 1", True),
        ("server --help --", True),
        ("server -h", False),
        ("foo x firmware flash", True),
        ("help foo firmware", True),
        ("foo x db drop", True),
        ("foo db-drop", False),
    ],
)
def test_refusal(command, refused):
    assert (remote.refusal(command.split(), ["firmware", "db drop"]) is not None) == refused


@pytest.mark.parametrize(
    "command, refused",
    [
        ("status", False),
        ("status x", False),
        ("status --help", False),
        ("help status", False),
        ("db list --all", False),
        ("db-list", True),  # one word, not the path db list
        ("db", True),  # the group, where only db list is allowed
        ("db drop", True),
        ("help db", True),
        ("brightness 70", True),
        ("Status", True),
        # The program's help and usage, and help for help and the server.
        ("", False),
        ("help", False),
        ("--help", False),
        ("help help", False),
        ("help server", False),
        ("server --help", False),
        ("server", True),
        # What is denied stays denied, allowed or not, anywhere.
        ("status x firmware", True),
        # A flag's value before the command cannot be told from a command.
        ("--level 1 status", True),
        ("--verbose status", False),
    ],
)
def test_refusal_with_an_allow_list(command, refused):
    assert (remote.refusal(command.split(), ["firmware"], ["status", "db list"]) is not None) == refused


def test_refusal_names_what_is_not_allowed():
    assert remote.refusal(["brightness", "70"], [], ["status"]) == "'brightness' is not available remotely"
    assert remote.refusal(["help", "brightness"], [], ["status"]) == "'brightness' is not available remotely"
    assert remote.refusal(["--level", "1", "status"], [], ["status"]) == (
        "'1' is not available remotely; a flag with a value goes after the command")


def test_an_empty_allow_list_allows_no_command():
    assert remote.refusal(["status"], [], []) is not None
    assert remote.refusal(["help"], [], []) is None


@pytest.mark.parametrize("lists", [{"deny": "firmware"}, {"allow": "status"}, {"allow": ["status", 1]},
                                   {"deny": None}], ids=repr)
def test_lists_are_command_paths(lists):
    """A single string would be read letter by letter, and allow or deny nothing meant."""
    with pytest.raises(TypeError):
        fireaid.Remote(**lists)


@pytest.mark.parametrize(
    "settings, error",
    [
        ({"port": "4247"}, TypeError),
        ({"port": True}, TypeError),
        ({"port": 4247.0}, TypeError),
        ({"port": 0}, ValueError),  # a client needs a port to go to
        ({"port": 65536}, ValueError),
        ({"env": 5}, TypeError),
        ({"env": ""}, ValueError),
        ({"env": "has space"}, ValueError),
        ({"env": "9LIVES"}, ValueError),
        ({"setup": "log"}, TypeError),
    ],
    ids=repr,
)
def test_remote_settings_are_checked_at_once(settings, error):
    """A mistake is reported where the program makes its Remote, not when a server starts, or every command line."""
    with pytest.raises(error):
        fireaid.Remote(**settings)


def test_remote_settings_that_are_fine():
    def setup(debug):
        pass

    remote_ = fireaid.Remote(port=1, env="TOOL_HOST", setup=setup)
    assert (remote_.port, remote_.env, remote_.setup) == (1, "TOOL_HOST", setup)
    assert fireaid.Remote(port=65535, env=None, setup=None).port == 65535


def test_lists_become_tuples():
    assert fireaid.Remote().allow is None and fireaid.Remote().deny == ()
    assert fireaid.Remote(allow=["status"], deny=["firmware"]) == fireaid.Remote(allow=("status",), deny=("firmware",))


def test_help_lists_the_remote_flag_for_what_is_allowed():
    allow = {"FIREAID_TEST_ALLOW": "foo,dynamic bar"}
    assert REMOTE_FLAG in run(["help", "foo"], "INSTANCE", **allow)[1]
    assert REMOTE_FLAG in run(["help", "dynamic", "bar"], "INSTANCE", **allow)[1]
    assert REMOTE_FLAG not in run(["help", "grep"], "INSTANCE", **allow)[1]
    assert REMOTE_FLAG not in run(["help", "dynamic", "hosty"], "INSTANCE", **allow)[1]
    out = run(["help", "server"], "INSTANCE", **allow)[1]
    assert "Only these commands run remotely: foo, dynamic bar." in out
    assert Said.local_only(out) == ["static"]


def test_server_runs_only_what_is_allowed(tmp_path):
    s = Server("CLI", False, tmp_path / "log", FIREAID_TEST_ALLOW="foo,dynamic bar")
    try:
        assert run(["foo", "x", "--remote", s.target]) == run(["foo", "x"])
        assert run(["dynamic", "bar", "--remote", s.target]) == run(["dynamic", "bar"])
        code, out, err = run(["grep", "pat", "--remote", s.target])
        assert (code, out) == (2, "") and Said.refused(err)
        assert Log.refused(s.log.text, "grep")
        assert run(["help", "server", "--remote", s.target])[0] == 0
    finally:
        s.stop()


# -- allow holds through chained commands, not only at the start


@pytest.mark.parametrize(
    "allow, command, runs",
    [
        ("foo", "foo x", True),                       # an allowed command, with its argument
        ("dynamic bar", "dynamic bar --x 3", True),   # navigating into an allowed path
        ("dynamic bar", "dynamic hosty", False),      # a sibling, not allowed
        ("echo", "echo a b c", True),                 # plain arguments do not extend the path
        ("chain", "chain bar", False),                # a command chained onto chain()'s result
    ],
)
def test_allow_holds_through_chaining(tmp_path, allow, command, runs):
    s = Server("CLI", False, tmp_path / "log", FIREAID_TEST_ALLOW=allow)
    try:
        code, out, err = run([*command.split(), "--remote", s.target])
        if runs:
            assert (code, out, err) == run(command.split())
        else:
            # Refused either at the handshake (the line as text) or as it runs
            # (a chain); either way the command does not run.
            assert (code, out) == (2, "") and "not available remotely" in err
    finally:
        s.stop()


def test_chained_command_outside_allow_does_not_run(tmp_path):
    """chain() returns a group; a command chained onto its result is refused as it runs."""
    s = Server("CLI", False, tmp_path / "log", FIREAID_TEST_ALLOW="chain")
    try:
        code, out, err = run(["chain", "bar", "--x", "9", "--remote", s.target])
        # bar would return "bar x=9"; instead the command refuses and produces nothing.
        assert (code, out, err) == (2, "", "cli.py: 'bar' is not available remotely\n")
    finally:
        s.stop()


# -- what a client sends cannot forge or disturb the server's log


def test_client_text_is_printable_in_the_log(tmp_path):
    s = Server("Probe", False, tmp_path / "log")
    try:
        forged = "6.6.6.6 done: exit 0 after 0.0 s"
        run(["echo", "\x1b]0;x\x07", f"a\n{forged}", "--remote", s.target], "Probe")
        text = s.log.wait(lambda t: "echo" in t)
        assert "\x1b" not in text, "no escape sequences reach the operator's terminal"
        assert "\x1b]0;x" not in text
        for line in text.splitlines():
            assert not line.startswith(forged), "a newline does not forge a log line"
    finally:
        s.stop()


# -- many clients at once are served, or told busy, never dropped


def test_many_clients_are_served_not_dropped(server):
    """More clients than the login limit still all get served within the wait."""
    import concurrent.futures

    target = server("Probe").target

    def once(i):
        return run(["env", "TERM", "--remote", target], "Probe", TERM=f"t{i}", input=b"")

    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        results = list(pool.map(once, range(16)))
    # Served, or on a slow computer told the server is busy; never dropped.
    served = 0
    for i, (code, out, err) in enumerate(results):
        assert "connection closed" not in err
        if code == 0:
            assert out == f"TERM=t{i}\n"
            served += 1
        else:
            assert (code, out) == (1, "") and Said.busy(err), err
    assert served, "some are served at once"


# -- allow as a command runs: groups, properties, a command's own Fire call


@pytest.mark.parametrize(
    "allow, command",
    [
        ("group bar", "group bar"),  # a group given as a class: instantiating it is navigation, not the command
        ("sync", "sync"),  # what an allowed command does with Fire itself is its own business
    ],
)
def test_allow_lets_through_what_it_names(tmp_path, allow, command):
    s = Server("Device", False, tmp_path / "log", FIREAID_TEST_ALLOW=allow)
    try:
        here = run(command.split(), "Device")
        assert here[0] == 0
        assert run([*command.split(), "--remote", s.target], "Device") == here
    finally:
        s.stop()


def test_refused_member_does_not_run(tmp_path):
    """A property chained onto an allowed command's result is refused before any of its code runs."""
    s = Server("Device", False, tmp_path / "log", FIREAID_TEST_ALLOW="on")
    try:
        code, out, err = run(["on", "gadget", "--remote", s.target], "Device")
        assert (code, out) == (2, "") and "'gadget' is not available remotely" in err
        # Old Fire finds any member by reading them all, properties included,
        # as it does locally too: there the property runs on the way to on.
        if "getmembers" not in inspect.getsource(fireaid.core._GetMember):
            assert "GADGET CODE RAN" not in err
    finally:
        s.stop()


def test_refusals_are_printable_in_the_log(tmp_path):
    """A refusal quotes the client's word; the log shows it rather than acting on it."""
    s = Server("Device", False, tmp_path / "log", FIREAID_TEST_ALLOW="on")
    try:
        assert run(["\x1b]0;x\x07", "--remote", s.target], "Device")[0] == 2
        text = s.log.wait(lambda t: "not available remotely" in t)
        assert "not available remotely" in text
        assert "\x1b" not in text and "\x07" not in text
    finally:
        s.stop()


def test_printable_shows_every_control_character():
    for c in "\x00\x07\x1b\n\r\x7f\x85\x9b\u2028\u2029\u202e":
        shown = remote._printable(f"a{c}b")
        assert c not in shown and shown[0] == "a" and shown[-1] == "b", repr(shown)
    assert remote._printable("tab\tand ünïcödé") == "tab\tand ünïcödé"


def test_clients_beyond_the_login_limit_are_told_so(tmp_path):
    """With every login slot held by a client that says nothing, the next is told why, not dropped."""
    s = Server("Probe", False, tmp_path / "log")
    idle = []
    try:
        idle = [socket.create_connection(("127.0.0.1", s.port), timeout=10) for _ in range(remote._MAX_HANDSHAKES)]
        for sock in idle:
            # The server sends its challenge once it has taken a login slot for the client.
            remote._Channel(sock).expect(remote.CHALLENGE)
        code, out, err = run(["tty", "--remote", s.target], "Probe")
        assert (code, out) == (1, "")
        assert "too many clients logging in" in err and "connection closed" not in err
    finally:
        for sock in idle:
            sock.close()
        s.stop()


def test_clients_waiting_to_run_are_bounded(tmp_path):
    """Logged-in clients waiting for the running command are bounded; beyond that, told so at once."""
    s = Server("Probe", False, tmp_path / "log", debug=True)
    first = subprocess.Popen(
        [sys.executable, "cli.py", "sleep", "--remote", s.target],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=environment("Probe"), cwd=TESTS,
    )
    waiting = []
    request = {"argv": ["tty"], "tty": [False, False, False], "env": {}}
    try:
        assert first.stdout.readline().rstrip(b"\r\n") == b"started"
        for _ in range(remote._MAX_WAITING):
            channel, (kind, _) = log_in(s.port)
            assert kind == remote.AUTH_OK
            channel.write(remote.RUN, request)
            waiting.append(channel)
        s.log.wait(lambda text: Log.waiting(text) == remote._MAX_WAITING)
        started = time.monotonic()
        channel, _ = log_in(s.port)
        waiting.append(channel)
        channel.write(remote.RUN, request)
        kind, payload = channel.read()
        assert kind == remote.REFUSED and "too many clients waiting" in remote._json(payload)["message"]
        assert time.monotonic() - started < 3, "told at once, not after the wait"
    finally:
        for channel in waiting:
            channel.sock.close()
        ended(first)
        s.stop()


def test_open_server_without_a_password_warns(tmp_path):
    path = tmp_path / "log"
    log_file = path.open("w")
    process = subprocess.Popen(
        [sys.executable, "cli.py", "server", "--port", "0"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_file,
        env=environment("INSTANCE"), cwd=TESTS,
    )
    log = Log(path)
    try:
        log.wait(lambda _: log.warned_no_password() and log.warned_open_network())
    finally:
        process.terminate()
        process.wait(10)
        log_file.close()
    assert log.setup_debug() is False
    assert log.host() == "0.0.0.0" and log.without_password()
    assert log.warned_no_password() and log.warned_open_network()


def test_connection_closed_before_the_handshake_is_no_refusal(server):
    quiet, loud = server(), server(debug=True)
    before = quiet.log.text
    socket.create_connection(("127.0.0.1", quiet.port), timeout=5).close()
    # Not a refusal: at the normal level the server says nothing at all.
    time.sleep(0.5)
    assert quiet.log.text == before
    before = loud.log.text
    socket.create_connection(("127.0.0.1", loud.port), timeout=5).close()
    added = loud.log.wait(lambda text: Log.finished(text[len(before):]))[len(before):]
    assert Log.connected(added) and Log.gone_in_handshake(added)


def test_server_setup_hook_and_debug(server):
    """The program's setup gets the --debug flag; the server then logs each client."""
    quiet, loud = server(), server(debug=True)
    assert quiet.log.setup_debug() is False and loud.log.setup_debug() is True
    # With input, stdin is a pipe: the NUL device counts as a terminal on Windows.
    assert run(["foo", "x", "--remote", loud.target], input=b"")[:2] == (0, "foo name='x' count=1\n")
    log = loud.log.text
    assert Log.connected(log) and Log.ran(log, "foo x") and Log.exited(log, 0)
    assert Log.launched(log, "foo x") and Log.terminal(log, "000")
    # The quiet server says the same at info, without the debug lines.
    run(["foo", "x", "--remote", quiet.target], input=b"")
    log = quiet.log.text
    assert Log.ran(log, "foo x") and Log.exited(log, 0)
    assert not Log.connected(log) and not Log.launched(log, "foo x")


@pytest.mark.parametrize(
    "command, shown",
    [
        ("foo x", "foo x"),
        ("foo x --count s3cret", "foo x ..."),
        ("foo x --count=s3cret", "foo x ..."),
        ("dynamic bar --x s3cret", "dynamic bar ..."),
        ("help foo", "help foo"),
        ("foo --help", "foo ..."),
        # Before the command, a flag's value cannot be told from a command word.
        ("--count s3cret foo x", "(flags) ..."),
        # A plain word is logged as given: a secret belongs in a flag.
        ("foo s3cret", "foo s3cret"),
    ],
)
def test_info_logs_the_words_up_to_the_first_flag(server, command, shown):
    """A flag's value may be a secret: info names the command by its words up to the first flag."""
    quiet = server()
    before = len(quiet.log.text)
    run([*command.split(), "--remote", quiet.target], input=b"")
    added = quiet.log.wait(lambda text: Log.finished(text[before:]))[before:]
    assert Log.ran_as(added, shown)
    assert ("s3cret" in added) == ("s3cret" in shown)


def test_debug_logs_the_whole_command_line(server):
    loud = server(debug=True)
    assert run(["foo", "x", "--count", "s3cret", "--remote", loud.target], input=b"")[0] == 0
    assert Log.launched(loud.log.text, "foo x --count s3cret")


def test_server_listens_everywhere_by_default():
    """--bind narrows it; a password does not change where it listens."""
    command = fireaid._server_command(fireaid.Remote(), "t", "T", None)
    parameters = inspect.signature(command).parameters
    assert parameters["bind"].default == "0.0.0.0"
    assert parameters["password"].default is None
    out = run(["help", "server"], "INSTANCE")[1]
    assert Said.lists_flag(out, "bind")


# -- What Fire() makes of remote=


class WithServer:
    def server(self):
        pass


@pytest.mark.parametrize("component", [WithServer, len])
def test_remote_needs_a_server_command(component):
    with pytest.raises(ValueError):
        fireaid.Fire(component, command=[], remote=True)


def test_remote_off_leaves_the_flag_to_fire():
    command = ["echo", "--remote", "x"]
    result = run(command, FIREAID_TEST_REMOTE="")
    assert result == run(command, FIREAID_TEST_REMOTE="", FIREAID_TEST_MODULE="fire")


def test_server_is_a_command():
    code, out, err = run(["help"], "INSTANCE")
    assert Said.lists_command(out, "server")
    code, out, err = run(["help", "server"], "INSTANCE")
    assert "CLI_REMOTE" in out and Said.local_only(out) == ["static"]


REMOTE_FLAG = "\n    --remote=[PASSWORD@]HOST[:PORT]\n"


@pytest.mark.parametrize("component", ["CLI", "INSTANCE"])
@pytest.mark.parametrize(
    "command",
    [["help"], ["--help"], ["help", "foo"], ["foo", "--help"], ["foo", "-h"], ["help", "dynamic"],
     ["dynamic", "bar", "--help"]],
)
def test_help_lists_the_remote_flag(component, command):
    code, out, err = run(command, component)
    flags = out[out.index("\nFLAGS\n"):]
    assert code == 0 and REMOTE_FLAG in flags
    assert "CLI_REMOTE" in flags and "cli.py help server" in flags


@pytest.mark.parametrize("command", [["help", "server"], ["help", "static"], ["help", "static", "bar"], ["help", "help"]])
def test_no_remote_flag_where_it_does_not_run_remotely(command):
    code, out, err = run(command, "INSTANCE")
    assert code == 0 and REMOTE_FLAG not in out


def test_no_remote_flag_without_remote():
    assert "--remote" not in run(["help", "foo"], FIREAID_TEST_REMOTE="")[1]


# -- Off unless the program turns it on, in its own call of Fire


class Plain:
    def foo(self):
        return "foo"


@pytest.mark.parametrize("value", [1, "yes", "True", fireaid.Remote, object()], ids=repr)
def test_only_true_or_a_remote_turns_it_on(value):
    """What might pass for yes is an error, not a guess."""
    with pytest.raises(TypeError):
        fireaid.Fire(Plain, command=["foo"], remote=value)


def test_remote_is_never_given_by_position():
    with pytest.raises(TypeError):
        fireaid.Fire(Plain, ["foo"], "tool", None, True)


def test_nothing_from_outside_turns_it_on():
    """
    Without remote=, --remote is the program's flag like any other,
    CLI_REMOTE its variable, and there is no server command.
    """
    off = {"FIREAID_TEST_REMOTE": "", "CLI_REMOTE": "127.0.0.1:1"}
    assert run(["kw", "--remote", "127.0.0.1:1"], **off) == (0, "kw [('remote', '127.0.0.1:1')]\n", "")
    assert run(["foo", "x"], **off) == (0, "foo name='x' count=1\n", "")
    code, out, err = run(["server"], **off)
    assert (code, out) == (2, "") and "Could not consume arg: server" in err
    assert not Said.lists_command(run(["help"], "INSTANCE", **off)[1], "server")


def test_remote_code_is_not_even_loaded_without_remote(tmp_path):
    program = tmp_path / "prog.py"
    program.write_text(
        "import sys, fireaid\n"
        "class P:\n"
        "    def foo(self):\n"
        "        return 'foo'\n"
        "fireaid.Fire(P, command=['foo'])\n"
        "try:\n"
        "    fireaid.Fire(P, command=['--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('fireaid.remote' in sys.modules, file=sys.stderr)\n"
    )
    completed = subprocess.run([sys.executable, str(program)], capture_output=True, text=True, cwd=TESTS,
                               env=environment("CLI", PROG_REMOTE="127.0.0.1:1"), stdin=subprocess.DEVNULL)
    assert completed.stderr == "False\n"


@pytest.mark.skipif(importlib.util.find_spec("fire.__main__") is None, reason="this Fire has no python -m fire")
def test_python_m_fireaid_has_no_remote_control():
    completed = subprocess.run([sys.executable, "-m", "fireaid", "cli.py", "CLI", "kw", "--remote", "127.0.0.1:1"],
                               capture_output=True, text=True, cwd=TESTS, env=environment("CLI"),
                               stdin=subprocess.DEVNULL)
    assert (completed.returncode, completed.stdout) == (0, "kw [('remote', '127.0.0.1:1')]\n")


# -- Command lines run remotely as locally


@pytest.mark.parametrize(
    "command",
    [
        "foo x",
        "foo x --count=2",
        "dynamic bar",
        "echo say help",
        "grep pat -h",
        "help",
        "help foo",
        "foo --help",
        "help server",
        "foo x --",
        "",
        "nonesuch",
        "foo",
    ],
)
def test_runs_as_locally(server, command):
    target = server().target
    assert run([*command.split(), "--remote", target]) == run(command.split())


def test_exit_code(server):
    assert run(["fail", "--code", "7", "--remote", server("Probe").target])[0] == 7


def test_flag_from_the_variable(server):
    code, out, err = run(["foo", "x"], CLI_REMOTE=server().target)
    assert (code, out) == (0, "foo name='x' count=1\n")


def test_input(server):
    target = server("Probe").target
    assert run(["cat", "--remote", target], "Probe", input=b"hello\n")[1] == "hello\n"
    data = os.urandom(1 << 20).hex().encode()
    assert run(["cat", "--remote", target], "Probe", input=data)[1] == data.decode()


def test_environment(server):
    target = server("Probe").target
    names = ["FIREAID_REMOTE_CHILD", "TERM", "NO_COLOR", "CLI_REMOTE"]
    local = run(["env", *names], "Probe", TERM="vt100", NO_COLOR="1")[1]
    assert local == "FIREAID_REMOTE_CHILD=- TERM=vt100 NO_COLOR=1 CLI_REMOTE=-\n"
    remote_ = run(["env", *names], "Probe", TERM="vt100", NO_COLOR="1", CLI_REMOTE=target)[1]
    assert remote_ == local.replace("CHILD=-", "CHILD=1")


def test_pipes(server):
    # With input, stdin is a pipe: the NUL device counts as a terminal on Windows.
    target = server("Probe").target
    assert run(["tty", "--remote", target], "Probe", input=b"")[1] == run(["tty"], "Probe", input=b"")[1] == "000 -\n"


@unix
def test_merged_output_keeps_its_order(server):
    """2>&1: the program writes to one stream, which keeps its order."""
    target = server("Probe").target
    shell = f"{sys.executable} cli.py interleave {{}} 2>&1 </dev/null"
    env = environment("Probe")
    local = subprocess.run(shell.format(""), shell=True, capture_output=True, env=env, cwd=TESTS)
    there = subprocess.run(shell.format(f"--remote {target}"), shell=True, capture_output=True, env=env, cwd=TESTS)
    assert local.stdout == "".join(f"{i}\n" for i in range(500)).encode()
    assert (there.returncode, there.stdout) == (local.returncode, local.stdout)


# -- What the server will not do


@pytest.mark.parametrize("command", ["static bar", "help static", "server", "server --port 1"])
def test_denied(server, command):
    code, out, err = run([*command.split(), "--remote", server().target])
    assert (code, out) == (2, "")
    assert Said.refused(err)
    denied = "server" if command.startswith("server") else "static"
    assert Log.refused(server().log.text, denied)


@pytest.mark.parametrize("flags", [["-i"], ["-vi"], ["--interactive"], ["--help"], ["--trace"], ["x"]])
def test_nothing_may_follow_the_separator(server, flags):
    """
    Fire's own flags follow the last separator, its interactive mode
    among them: a Python shell on the server, outside any command. None
    of them is needed remotely, so the server refuses them all rather
    than telling them apart.
    """
    # --remote goes before the separator, so the client sends it to the server.
    code, out, err = run(["foo", "--remote", server().target, "--", *flags])
    assert (code, out) == (2, "")
    assert Said.refused_after_separator(err)


def test_password(server):
    s = server(password=True)
    assert run(["foo", "x", "--remote", s.target])[:2] == (0, "foo name='x' count=1\n")
    for target in (f"wrong@127.0.0.1:{s.port}", f"127.0.0.1:{s.port}"):
        code, out, err = run(["foo", "x", "--remote", target])
        assert (code, out) == (1, "")
        assert Said.authentication_failed(err, s.port)


def test_numeric_password_is_a_string(server):
    # Fire alone would read 1e3 as the float 1000.0; the server's hint keeps it.
    s = server(password="1e3")
    assert run(["foo", "x", "--remote", s.target])[:2] == (0, "foo name='x' count=1\n")
    code, out, err = run(["foo", "x", "--remote", f"1000.0@127.0.0.1:{s.port}"])
    assert (code, out) == (1, "")
    assert Said.authentication_failed(err, s.port)


def test_password_without_a_value_is_refused():
    # A bare --password would be the password "True".
    for argv in (["--password"], ["--password", "--port", "0"]):
        process = subprocess.run(
            [sys.executable, "cli.py", "server", "--bind", "127.0.0.1", *argv],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=environment("CLI"), cwd=TESTS, timeout=30,
        )
        assert (process.returncode, process.stdout) == (2, "")
        assert process.stderr.startswith("SETUP debug=False\n") and Said.needs_value(process.stderr, "--password")


def test_password_given_to_a_server_without_one_is_refused(server):
    """A server that asks for no password may not be the one meant: the command does not go to it."""
    s = server()
    assert s.log.without_password()
    assert s.log.warned_no_password()
    assert not s.log.warned_open_network()  # it listens on loopback
    before = s.log.text
    for password in ("pw", ""):
        code, out, err = run(["foo", "x", "--remote", f"{password}@{s.target}"])
        assert (code, out) == (1, "")
        assert Said.not_sent_without_password(err, s.target)
    assert not Log.ran(s.log.text[len(before):], "foo x")


def test_server_that_does_not_know_the_password_gets_no_command():
    """The server proves it knows the password too: an impostor learns no command line."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    got = []

    def impostor():
        sock, _ = listener.accept()
        channel = remote._Channel(sock)
        channel.write(remote.CHALLENGE, {"proto": remote.PROTOCOL, "nonce": "ab" * 32, "auth": True, "pty": False})
        got.append(channel.read())  # HELLO
        got.append(channel.read())  # AUTH
        channel.write(remote.AUTH_OK, {"mac": "0" * 64})
        sock.settimeout(10)
        try:
            got.append(channel.read())
        except EOFError:
            pass
        sock.close()

    thread = threading.Thread(target=impostor)
    thread.start()
    code, out, err = run(["foo", "x", "--count", "s3cret", "--remote", f"pw@127.0.0.1:{port}"])
    thread.join(10)
    listener.close()
    assert (code, out) == (1, "")
    assert Said.not_sent_to_impostor(err, f"127.0.0.1:{port}")
    assert [kind for kind, _ in got] == [remote.HELLO, remote.AUTH]  # and no RUN


def log_in(port, password=None, mac=None):
    """Log in to a server by hand. Returns the channel, and what the server answered."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    channel = remote._Channel(sock)
    challenge = remote._json(channel.expect(remote.CHALLENGE))
    channel.write(remote.HELLO, {"proto": remote.PROTOCOL})
    if challenge["auth"]:
        ours = os.urandom(32)
        if mac is None:
            mac = remote._mac(password, "client", bytes.fromhex(challenge["nonce"]), ours)
        channel.write(remote.AUTH, {"mac": mac, "nonce": ours.hex()})
    return channel, channel.read()


def run_raw(port, request):
    """
    Log in and send a request by hand, as a client may send whatever it
    likes. Returns ("refused", message, code) or ("ran", stdout, exit code).
    """
    channel, (kind, _) = log_in(port)
    assert kind == remote.AUTH_OK
    try:
        channel.write(remote.RUN, request)
        out = bytearray()
        while True:
            kind, payload = channel.read()
            if kind == remote.REFUSED:
                message = remote._json(payload)
                return "refused", message["message"], message["code"]
            if kind in (remote.STDOUT, remote.TTY):
                out += payload
            elif kind == remote.EXIT:
                return "ran", out.decode(), remote._json(payload)["code"]
    finally:
        channel.sock.close()


# -- what a client sends by hand: only what the protocol allows reaches the command


def test_client_environment_is_filtered(server):
    """
    Only terminal variables, and only as text, go from a client to the
    command: not PYTHONPATH, LD_PRELOAD or anything else that would change
    what the command runs, and with an allow list get round it.
    """
    s = server("Probe")
    injected = ["PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PATH", "FIREAID_TEST_ALLOW"]
    env = {"TERM": "sent-term", "COLUMNS": 123, **{name: "INJECTED" for name in injected}}
    kind, out, code = run_raw(s.port, {"argv": ["env", "TERM", "COLUMNS", *injected], "tty": [False] * 3, "env": env})
    assert (kind, code) == ("ran", 0)
    assert "TERM=sent-term" in out, "a terminal variable goes along"
    assert "COLUMNS=123" not in out, "a number is not text"
    assert "INJECTED" not in out, "nothing else does"


@pytest.mark.parametrize(
    "request_, message",
    [
        ({"argv": "tty", "tty": [False] * 3}, "bad command line"),
        ({"argv": ["tty", 5], "tty": [False] * 3}, "bad command line"),
        ({"argv": ["tty"], "tty": [False, False]}, "bad terminal description"),
        ({"argv": ["tty"], "tty": "000"}, "bad terminal description"),
        ({"argv": ["tty"], "tty": [False] * 3, "env": ["TERM"]}, "bad environment"),
        (b"not json", "bad message"),
        (b"[1, 2]", "bad message"),
    ],
    ids=["argv not a list", "argv not text", "two terminals", "terminals not a list", "env not a map",
         "not json", "not an object"],
)
def test_malformed_requests_are_refused(server, request_, message):
    s = server("Probe")
    assert run_raw(s.port, request_) == ("refused", message, 2)
    assert run(["tty", "--remote", s.target], "Probe", input=b"")[0] == 0, "and the server goes on"


def test_server_refuses_another_protocol(server):
    s = server("Probe")
    with socket.create_connection(("127.0.0.1", s.port), timeout=10) as sock:
        channel = remote._Channel(sock)
        channel.expect(remote.CHALLENGE)
        channel.write(remote.HELLO, {"proto": 99})
        kind, payload = channel.read()
    assert kind == remote.REFUSED
    assert remote._json(payload) == {
        "message": f"this server speaks fireaid remote protocol {remote.PROTOCOL}, the client 99", "code": 1}


def test_unexpected_message_in_the_handshake_is_refused(server):
    s = server("Probe")
    with socket.create_connection(("127.0.0.1", s.port), timeout=10) as sock:
        channel = remote._Channel(sock)
        channel.expect(remote.CHALLENGE)
        channel.write(remote.RUN, {"argv": ["tty"], "tty": [False] * 3, "env": {}})  # before HELLO
        kind, payload = channel.read()
    assert (kind, remote._json(payload)) == (remote.REFUSED, {"message": f"unexpected message {remote.RUN}", "code": 2})


def test_client_refuses_another_protocol():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def other_server():
        sock, _ = listener.accept()
        remote._Channel(sock).write(remote.CHALLENGE, {"proto": 99, "nonce": "ab" * 32, "auth": False, "pty": False})
        sock.settimeout(10)
        try:
            sock.recv(1)
        except OSError:
            pass
        sock.close()

    thread = threading.Thread(target=other_server)
    thread.start()
    result = run(["foo", "x", "--remote", f"127.0.0.1:{port}"])
    thread.join(10)
    listener.close()
    assert result == (1, "", f"cli.py: 127.0.0.1:{port} speaks fireaid remote protocol 99, "
                             f"this client speaks {remote.PROTOCOL}\n")


def test_run_time_check_holds_by_itself(monkeypatch, capsys):
    """In the command's own process the allow list holds by itself, not only behind the server's check of the text."""
    monkeypatch.setenv(remote.CHILD_VARIABLE, "1")
    monkeypatch.delenv("TOOL_REMOTE", raising=False)
    ran = []

    class Device:
        def status(self):
            return "ok"

        def firmware(self):
            ran.append("firmware")
            return "flashed"

    allow = fireaid.Remote(allow=("status",))
    with pytest.raises(SystemExit) as exit:
        fireaid.Fire(Device, command=["firmware"], name="tool", remote=allow)
    assert exit.value.code == 2 and ran == []
    assert capsys.readouterr().err == "tool: 'firmware' is not available remotely\n"
    assert fireaid.Fire(Device, command=["status"], name="tool", remote=allow) == "ok"
    # Outside a server's command, the same program runs anything.
    monkeypatch.delenv(remote.CHILD_VARIABLE)
    assert fireaid.Fire(Device, command=["firmware"], name="tool", remote=allow) == "flashed"


def test_any_mac_is_refused_as_such(server):
    """A MAC that is not even ASCII fails authentication like any other wrong one."""
    s = server(password=True)
    before = s.log.text
    channel, (kind, payload) = log_in(s.port, mac="é" * 64)
    channel.sock.close()
    assert kind == remote.REFUSED and remote._json(payload)["message"] == "authentication failed"
    assert "error" not in s.log.text[len(before):]


def test_server_takes_no_more_input_than_it_asked_for(server):
    """A client that sends input the server has not asked for is cut off."""
    s = server("Probe")
    channel, (kind, _) = log_in(s.port)
    assert kind == remote.AUTH_OK
    channel.write(remote.RUN, {"argv": ["sleep", "--seconds", "20"], "tty": [False, False, False], "env": {}})
    chunk = b"x" * (remote._MAX_FRAME // 2)
    try:
        for _ in range(4):  # twice what it asks for
            channel.write(remote.STDIN, chunk)
        channel.sock.settimeout(10)
        while channel.read():
            pass
    except (EOFError, OSError):
        pass
    channel.sock.close()
    assert s.log.wait(lambda text: "went away" in text) and "went away" in s.log.text


def test_unreachable():
    code, out, err = run(["foo", "x", "--remote", "127.0.0.1:1"])
    assert code == 1
    assert Said.unreachable(err, "127.0.0.1:1")


def test_connect_retries_transient_errors_and_names_each_address(monkeypatch):
    attempts = []

    class Sock:
        def __init__(self, family, kind, proto):
            self.family = family

        def settimeout(self, t):
            pass

        def setsockopt(self, *option):
            pass

        def connect(self, address):
            attempts.append(address[0])
            if self.family == socket.AF_INET6:
                raise OSError(errno.ENETUNREACH, "No route to host")
            if len([a for a in attempts if a == address[0]]) < 2:
                raise OSError(errno.EHOSTUNREACH, "No route to host")  # ARP still pending

        def close(self):
            pass

    found = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::5", 4247, 0, 0)),
             (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.5", 4247))]
    monkeypatch.setattr(remote.socket, "getaddrinfo", lambda *a, **k: found)
    monkeypatch.setattr(remote.socket, "socket", Sock)
    monkeypatch.setattr(remote.time, "sleep", lambda s: None)
    sock = remote.connect(remote.Target("r5", 4247, None), timeout=5)
    assert isinstance(sock, Sock) and sock.family == socket.AF_INET
    assert attempts == ["fd00::5", "192.168.1.5", "fd00::5", "192.168.1.5"]

    def refused(self, address):
        raise OSError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(Sock, "connect", refused)
    with pytest.raises(remote.RemoteError) as e:
        remote.connect(remote.Target("r5", 4247, None), timeout=5)
    assert str(e.value) == ("cannot reach r5:4247: [fd00::5]: Connection refused; 192.168.1.5: Connection refused")


def test_instant_no_route_everywhere_names_the_macos_permission(monkeypatch):
    class Sock:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect(self, address):
            raise OSError(errno.EHOSTUNREACH, "No route to host")

        def close(self):
            pass

    found = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.5", 4247))]
    monkeypatch.setattr(remote.socket, "getaddrinfo", lambda *a, **k: found)
    monkeypatch.setattr(remote.socket, "socket", Sock)
    monkeypatch.setattr(remote.time, "sleep", lambda s: None)
    monkeypatch.setattr(remote.sys, "platform", "darwin")
    with pytest.raises(remote.RemoteError) as e:
        remote.connect(remote.Target("r5", 4247, None), timeout=0.6)
    assert "192.168.1.5: No route to host" in str(e.value) and "Local Network" in str(e.value)
    monkeypatch.setattr(remote.sys, "platform", "linux")
    with pytest.raises(remote.RemoteError) as e:
        remote.connect(remote.Target("r5", 4247, None), timeout=0.6)
    assert "Local Network" not in str(e.value)


@unix  # ps tells the server's memory
@pytest.mark.parametrize("pipes", [False, True], ids=["server", "pipes-only server"])
def test_input_waits_for_the_command(server, pipes):
    """Input the command does not read waits at the client, not in the server's memory."""
    s = server("Probe", pipes=pipes)

    def memory():
        return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(s.process.pid)])) // 1024

    before = memory()
    client = subprocess.Popen(
        [sys.executable, "cli.py", "sleep", "--seconds", "20", "--remote", s.target],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment("Probe"),
        cwd=TESTS,
    )
    sent = []

    def feed():
        try:
            for _ in range(200):
                client.stdin.write(b"x" * (1 << 20))
                sent.append(1)
        except (BrokenPipeError, ValueError):
            pass

    threading.Thread(target=feed, daemon=True).start()
    time.sleep(3)
    grown = memory() - before
    ended(client)
    assert len(sent) < 50, "the client's stdin backs up"
    assert grown < 32, f"the server grew by {grown} MiB"


def test_pipes_only_command_lets_go_of_its_threads():
    """A client gone while the command's output is backed up leaves no thread behind."""
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    code = "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"
    launch = remote.Launch((sys.executable, "-c", code), str(TESTS))
    request = {"argv": [], "tty": [False, False, False], "env": {}}
    before = threading.active_count()
    command = remote._PipeCommand(remote._Channel(ours), launch, request, ())
    result = []
    runner = threading.Thread(target=lambda: result.append(command.run()))
    runner.start()
    time.sleep(2)  # nobody reads: the output backs up into the queue
    theirs.close()
    runner.join(15)
    ours.close()
    deadline = time.monotonic() + 15
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert result == [None]
    assert threading.active_count() == before


def test_interrupt_without_sigwinch(monkeypatch):
    """Windows has no SIGWINCH: Ctrl-C at a client there is passed on all the same."""
    monkeypatch.delattr(signal, "SIGWINCH", raising=False)
    ours, theirs = socket.socketpair()
    client = remote._Client(ours, remote.Target("h", 1, None))
    client.signals.append(signal.SIGINT)
    client.handle_signals()
    ours.close()
    theirs.close()
    assert list(remote._parse(bytearray(client.channel.outgoing))) == [(remote.SIGNAL, b'{"name": "INT"}')]


def test_connection_has_the_handshake_time_and_keepalive():
    """However long connecting took, the handshake gets its own time; a vanished peer is noticed."""
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        sock = remote.connect(remote.Target("127.0.0.1", listener.getsockname()[1], None), timeout=0.5)
        assert sock.gettimeout() == remote._HANDSHAKE_TIMEOUT
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        sock.close()
    finally:
        listener.close()


def test_a_lost_connection_is_a_peer_gone():
    """What keepalive reports, a timed-out connection, ends the relay as a closed one does."""

    class Lost:
        def recv(self, size):
            raise TimeoutError(errno.ETIMEDOUT, "Operation timed out")

        send = recv

    channel = remote._Channel(Lost())
    with pytest.raises(EOFError):
        channel.receive()
    channel.outgoing += b"x"
    with pytest.raises(EOFError):
        channel.flush()


def test_busy_server_says_so(server):
    """While one client's command runs, another is told so at once."""
    target = server("Probe").target
    first = subprocess.Popen(
        [sys.executable, "cli.py", "sleep", "--remote", target],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=environment("Probe"), cwd=TESTS,
    )
    try:
        assert first.stdout.readline().rstrip(b"\r\n") == b"started"
        started = time.monotonic()
        code, out, err = run(["tty", "--remote", target], "Probe")
        assert time.monotonic() - started < 20  # the wait, not the command's 30 s
        assert (code, out) == (1, "") and Said.busy(err)
    finally:
        before = len(server("Probe").log.text)
        ended(first)
        server("Probe").log.wait(lambda text: "went away" in text[before:])


@unix
def test_next_client_waits_for_a_command_ending(server):
    """A command that takes a moment to end, its client gone, delays the next client rather than turning it away."""
    target = server("Probe").target
    first = subprocess.Popen(
        [sys.executable, "cli.py", "stubborn", "--remote", target],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=environment("Probe"), cwd=TESTS,
    )
    assert first.stdout.readline().rstrip(b"\r\n") == b"started"
    ended(first)
    code, out, err = run(["tty", "--remote", target], "Probe")
    assert (code, err) == (0, "")


@pytest.mark.parametrize("pipes", [False, True], ids=["server", "pipes-only server"])
def test_command_is_done_when_it_is(server, pipes):
    """
    A command that leaves a process running, holding its output pipes,
    is done when it exits. (On a terminal, that process gets SIGHUP.)
    """
    target = server("Probe", pipes=pipes).target
    started = time.monotonic()
    assert run(["spawn", "--remote", target], "Probe") == (0, "spawned\n", "")
    assert time.monotonic() - started < 10  # not the 15 s of what it left running


@unix
def test_end_of_input_from_a_terminal(server):
    """Ctrl-D at the client's terminal ends the input of a command that has a pipe for it."""
    target = server("Probe", pipes=True).target
    code, output, _ = on_terminal(["cat", "--remote", target], keys=b"hello\n\x04")
    assert code == 0
    assert output.count("hello") == 2  # as typed, and from cat


@unix  # ps tells the client's memory
def test_windows_like_client_reads_input_as_it_is_asked_for(server):
    """The thread that reads stdin waits for the server too."""
    s = server("Probe")
    client = subprocess.Popen(
        [sys.executable, "cli.py", "sleep", "--seconds", "20", "--remote", s.target],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=environment("Probe", FIREAID_REMOTE_TEST_NO_PTY="1"), cwd=TESTS,
    )
    sent = []

    def feed():
        try:
            for _ in range(200):
                client.stdin.write(b"x" * (1 << 20))
                sent.append(1)
        except (BrokenPipeError, ValueError):
            pass

    threading.Thread(target=feed, daemon=True).start()
    time.sleep(3)
    memory = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(client.pid)])) // 1024
    ended(client)
    assert len(sent) < 50, "the client's stdin backs up"
    assert memory < 100, f"the client holds {memory} MiB"


def test_bad_message_mid_command_is_reported():
    """A server's malformed message is an error to report, not a crash."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def bad_server():
        sock, _ = listener.accept()
        channel = remote._Channel(sock)
        channel.write(remote.CHALLENGE, {"proto": remote.PROTOCOL, "nonce": "ab" * 32, "auth": False, "pty": False})
        channel.read()  # HELLO
        channel.write(remote.AUTH_OK)
        channel.read()  # RUN
        channel.write(remote.EXIT, {"code": "x"})
        sock.close()

    thread = threading.Thread(target=bad_server)
    thread.start()
    result = run(["foo", "x", "--remote", f"127.0.0.1:{port}"])
    thread.join(10)
    listener.close()
    assert result == (1, "", f"cli.py: 127.0.0.1:{port}: bad code\n")


@unix
def test_terminal_is_restored_whatever_goes_wrong(monkeypatch):
    """Once the client's terminal is raw, an error on the way leaves it as it was."""
    master, slave = pty.openpty()
    saved_stdin = os.dup(0)
    ours, theirs = socket.socketpair()
    try:
        os.dup2(slave, 0)
        before = termios.tcgetattr(0)

        def fail(*args):
            raise RuntimeError("on the way")

        monkeypatch.setattr(remote._Client, "handshake", lambda self, argv: None)
        monkeypatch.setattr(remote, "_same_file", fail)
        with pytest.raises(RuntimeError):
            remote._Client(ours, remote.Target("h", 1, None)).run(["tty"])
        # What raw mode turns off. macOS adds PENDIN of its own on the way back.
        cooked = termios.ICANON | termios.ECHO | termios.ISIG
        assert termios.tcgetattr(0)[3] & cooked == before[3] & cooked
    finally:
        os.dup2(saved_stdin, 0)
        for fd in (saved_stdin, master, slave):
            os.close(fd)
        ours.close()
        theirs.close()


def test_clients_slow_to_log_in_keep_no_one_out(server):
    """Connections that never log in do not hold up one that does."""
    s = server(password=True)
    idle = [socket.create_connection(("127.0.0.1", s.port)) for _ in range(3)]
    try:
        started = time.monotonic()
        assert run(["foo", "x", "--remote", s.target])[:2] == (0, "foo name='x' count=1\n")
        assert time.monotonic() - started < 8  # not the idle ones' handshake time of 10 s each
    finally:
        for sock in idle:
            sock.close()


def test_server_password_from_the_environment(tmp_path):
    """CLI_SERVER_PASSWORD keeps the password off the command line; commands do not get it."""
    s = Server("Probe", False, tmp_path / "log", CLI_SERVER_PASSWORD=PASSWORD)
    try:
        assert not s.log.without_password()
        target = f"{PASSWORD}@127.0.0.1:{s.port}"
        assert run(["env", "CLI_SERVER_PASSWORD", "--remote", target], "Probe", input=b"")[:2] == (
            0, "CLI_SERVER_PASSWORD=-\n")
        code, out, err = run(["foo", "x", "--remote", f"wrong@127.0.0.1:{s.port}"], "Probe")
        assert (code, out) == (1, "") and Said.authentication_failed(err, s.port)
    finally:
        s.stop()


def test_server_logs_through_its_own_logger_only(monkeypatch):
    """Without the program's own logging, --debug shows the server's lines, not every library's."""
    import logging

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(remote.logger, "handlers", [])
    monkeypatch.setattr(remote.logger, "level", logging.NOTSET)
    monkeypatch.setattr(remote.logger, "propagate", True)
    level = root.level
    remote._plain_logging("t", debug=True)
    assert root.handlers == [] and root.level == level
    assert len(remote.logger.handlers) == 1 and remote.logger.level == logging.DEBUG
    assert not remote.logger.propagate


@pytest.mark.parametrize("pipes", [False, True])
def test_client_gone_stops_the_command(server, pipes):
    target = server("Probe", pipes=pipes).target
    client = subprocess.Popen(
        [sys.executable, "cli.py", "sleep", "--remote", target],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        env=environment("Probe"),
        cwd=TESTS,
    )
    assert client.stdout.readline().rstrip(b"\r\n") == b"started"
    ended(client)
    # The server is free for the next client at once, not after the sleep.
    assert run(["tty", "--remote", target], "Probe")[0] == 0


# -- Pipes only: a server on Windows, or one made to behave like it


@pytest.mark.parametrize("command", ["foo x", "help foo", "nonesuch", "echo say help"])
def test_pipes_only_server_runs_as_locally(server, command):
    s = server(pipes=True)
    assert s.pipes
    assert run([*command.split(), "--remote", s.target]) == run(command.split())


@pytest.mark.parametrize("client", ["", "1"], ids=["client", "windows-like client"])
@pytest.mark.parametrize("pipes", [False, True], ids=["server", "pipes-only server"])
def test_input_and_exit_code_between_kinds(server, pipes, client):
    """Unix and Windows-like servers and clients, in every combination."""
    s = server("Probe", pipes=pipes)
    if not pipes and not remote.HAS_PTY:
        pytest.skip("no server with terminals here")
    like = {"FIREAID_REMOTE_TEST_NO_PTY": client}
    assert run(["cat", "--remote", s.target], "Probe", input=b"hello\n", **like) == (0, "hello\n", ""), s.log.text
    data = os.urandom(1 << 18).hex().encode()
    assert run(["cat", "--remote", s.target], "Probe", input=data, **like)[1] == data.decode()
    assert run(["fail", "--code", "7", "--remote", s.target], "Probe", **like)[0] == 7
    assert run(["tty", "--remote", s.target], "Probe", input=b"", **like) == (0, "000 -\n", ""), s.log.text


def test_pipes_only_denied(server):
    s = server(pipes=True)
    code, out, err = run(["static", "bar", "--remote", s.target])
    assert (code, out) == (2, "") and Said.refused(err)


@unix
@pytest.mark.parametrize("pipes", [False, True], ids=["server", "pipes-only server"])
def test_interrupt_without_a_terminal(server, pipes):
    target = server("Probe", pipes=pipes).target
    with subprocess.Popen(
        [sys.executable, "cli.py", "sleep", "--remote", target],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment("Probe"),
        cwd=TESTS,
        # Python ignores SIGINT in a child of a non-interactive shell; undo that.
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    ) as client:  # closes its pipes, however the test ends
        assert client.stdout.readline() == b"started\n"
        client.send_signal(signal.SIGINT)
        assert client.wait(10) == 128 + signal.SIGINT
        assert client.stderr.read() == b"interrupted\n"


# -- Terminals


def on_terminal(argv, component="Probe", stdout_tty=True, keys=None, wait_for=b"", resize=None, **variables):
    """Run the test program, remote-controlled, on a terminal; see support.on_terminal."""
    env = environment(component, TERM="xterm-256color", **variables)
    return run_on_terminal(argv, env, stdout_tty, keys, wait_for, resize)


@unix
def test_terminal(server):
    target = server("Probe").target
    assert on_terminal(["tty", "--remote", target]) == on_terminal(["tty"]) == (0, "111 80x24\n", "")


@unix
def test_client_pager_stays_on_the_client(server):
    """
    Fire runs $PAGER in a shell when it pages help on a terminal. A
    client's PAGER would run on the server, as any command would: it
    does not go along, so the command uses the server's.
    """
    target = server("Probe").target
    pager = {"PAGER": "sed s/^/PAGED:/", "LC_CTYPE": "C.UTF-8"}
    code, output, _ = on_terminal(["foo", "--help", "x"], **pager)
    assert code == 0 and "PAGED:" in output  # locally, the program pages through it
    code, output, _ = on_terminal(["foo", "--help", "x", "--remote", target], **pager)
    assert code == 0 and "PAGED:" not in output and "foo" in output


@unix
def test_window_size_out_of_range_is_ignored():
    master, slave = pty.openpty()
    try:
        remote._set_size(master, {"rows": 24, "cols": 80})
        for size in ({"rows": 70000, "cols": 80}, {"rows": -1, "cols": 80}, {"rows": "x"}, None):
            remote._set_size(master, size)
        assert struct.unpack("HHHH", fcntl.ioctl(master, termios.TIOCGWINSZ, b"\0" * 8))[:2] == (24, 80)
    finally:
        os.close(master)
        os.close(slave)


@unix
def test_windows_like_client_on_a_terminal(server):
    """Its terminal stays as it is; the command still gets one on the server."""
    target = server("Probe").target
    like = {"FIREAID_REMOTE_TEST_NO_PTY": "1"}
    assert on_terminal(["tty", "--remote", target], **like) == (0, "111 80x24\n", "")
    assert on_terminal(["color", "--remote", target], stdout_tty=False, **like) == (0, "\x1b[31mred\x1b[0m\n", "result\n")


@unix
def test_terminal_client_of_a_pipes_only_server(server):
    """The command gets pipes, and so behaves as if redirected."""
    target = server("Probe", pipes=True).target
    assert on_terminal(["tty", "--remote", target]) == (0, "000 -\n", "")
    assert on_terminal(["color", "--remote", target]) == (0, "plain\nresult\n", "")


@unix
def test_terminal_for_stderr_and_a_pipe_for_stdout(server):
    target = server("Probe").target
    local = on_terminal(["color"], stdout_tty=False)
    assert local == (0, "\x1b[31mred\x1b[0m\n", "result\n")
    assert on_terminal(["color", "--remote", target], stdout_tty=False) == local
    assert on_terminal(["tty", "--remote", target], stdout_tty=False) == (0, "", "101 80x24\n")


@unix
def test_escape_from_a_raw_terminal(server):
    """With the client's terminal raw, Ctrl-C is a key for the server's; Enter ~ . disconnects."""
    target = server("Probe").target
    code, output, _ = on_terminal(["sleep", "--remote", target], keys=b"\r~.", wait_for=b"started")
    assert code == 255
    assert Said.disconnected(output, target)


@unix
def test_terminal_resized_during_a_command(server):
    """The command's terminal follows the client's as it is resized, as a local one would."""
    target = server("Probe").target
    local = on_terminal(["resized"], wait_for=b"started", resize=(30, 100))
    assert local[0] == 0 and local[1].endswith("100x30\n")
    code, output, _ = on_terminal(["resized", "--remote", target], wait_for=b"started", resize=(30, 100))
    assert code == 0 and output.endswith("100x30\n")


@unix
def test_second_ctrl_c_leaves_the_client(server):
    """
    Where Ctrl-C reaches the client, its terminal being cooked, a second
    one within a second leaves it, whatever the command makes of the first.
    """
    target = server("Probe", pipes=True).target
    started = time.monotonic()
    code, output, _ = on_terminal(["deaf", "--remote", target], keys=[b"\x03", b"\x03"], wait_for=b"started")
    assert code == 128 + signal.SIGINT
    assert time.monotonic() - started < 20, "not after the command's 30 s"


@pytest.mark.parametrize(
    "spec, script, prefix",
    [
        ("pkg.__main__", None, ("-m", "pkg")),  # python -m pkg
        ("pkg.tool", None, ("-m", "pkg.tool")),  # python -m pkg.tool
        (None, "tool.py", ("tool.py",)),  # python tool.py, or tool.py with its #!
        (None, "tool.exe", ()),  # a console script's launcher on Windows: run as it is
    ],
)
def test_launch_starts_the_program_again(monkeypatch, tmp_path, spec, script, prefix):
    import types

    monkeypatch.chdir(tmp_path)
    main = types.SimpleNamespace(__spec__=types.SimpleNamespace(name=spec) if spec else None)
    monkeypatch.setitem(sys.modules, "__main__", main)
    if script:
        (tmp_path / script).write_text("")
    monkeypatch.setattr(sys, "argv", [script or "-m"])
    launch = remote.Launch.capture()
    expected = (script,) if script and script.endswith(".exe") else (sys.executable, *prefix)
    assert (launch.prefix, launch.cwd, launch.error) == (expected, str(tmp_path), None)


def test_launch_without_a_script_says_so(monkeypatch, tmp_path):
    import types

    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "__main__", types.SimpleNamespace(__spec__=None))
    monkeypatch.setattr(sys, "argv", ["nonesuch.py"])
    launch = remote.Launch.capture()
    assert launch.prefix == () and launch.error == "cannot start this program again: nonesuch.py is not a file"


@pytest.mark.parametrize(
    "argv, code, message",
    [
        (["--port", "70000"], 2, "cli.py: 70000 is not a port\n"),
        (["--port=-1"], 2, "cli.py: -1 is not a port\n"),
        (["--bind", ""], 2, "cli.py: '' is not an address to bind to\n"),
    ],
)
def test_server_startup_errors(argv, code, message):
    result = run(["server", "--bind", "127.0.0.1", *argv] if "--bind" not in argv else ["server", *argv])
    assert result[0] == code and result[2].endswith(message), result


def test_server_port_in_use_is_reported(server):
    """A second server on a port in use says so, rather than sharing or taking it."""
    port = server().port
    code, out, err = run(["server", "--port", str(port), "--bind", "127.0.0.1"])
    assert (code, out) == (1, "")
    assert err.splitlines()[-1].startswith(f"cli.py: cannot listen on 127.0.0.1:{port}: ")


@unix
@pytest.mark.parametrize("pipes", [False, True], ids=["server", "pipes-only server"])
def test_ctrl_c(server, pipes):
    # A server with terminals gets Ctrl-C as a key. With a pipes-only server the client's
    # terminal stays cooked: Ctrl-C interrupts the client, which passes it on.
    target = server("Probe", pipes=pipes).target
    code, output, _ = on_terminal(["sleep", "--remote", target], keys=b"\x03", wait_for=b"started")
    assert code == 128 + signal.SIGINT
    assert output.endswith("interrupted\n")
