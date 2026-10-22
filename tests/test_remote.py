"""
Remote control: a command line run through a server behaves as it does
locally, and the server runs only what it may.

On Windows, which has no pseudo-terminals, a server gives commands pipes.
FIREAID_REMOTE_TEST_NO_PTY makes a server or client anywhere behave like
one on Windows, so that the two kinds are tested against each other.
"""

import errno
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
        self.process = subprocess.Popen(
            [sys.executable, "cli.py", *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log.open("w"),
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


def test_open_server_without_a_password_warns(tmp_path):
    path = tmp_path / "log"
    process = subprocess.Popen(
        [sys.executable, "cli.py", "server", "--port", "0"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=path.open("w"),
        env=environment("INSTANCE"), cwd=TESTS,
    )
    log = Log(path)
    try:
        log.wait(lambda _: log.warned_no_password() and log.warned_open_network())
    finally:
        process.terminate()
        process.wait(10)
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
    client.kill()
    client.wait()
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
        first.kill()
        first.wait()
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
    first.kill()
    first.wait()
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
    client.kill()
    client.wait()
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
    client.kill()
    client.wait()
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
    client = subprocess.Popen(
        [sys.executable, "cli.py", "sleep", "--remote", target],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment("Probe"),
        cwd=TESTS,
        # Python ignores SIGINT in a child of a non-interactive shell; undo that.
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    assert client.stdout.readline() == b"started\n"
    client.send_signal(signal.SIGINT)
    assert client.wait(10) == 128 + signal.SIGINT
    assert client.stderr.read() == b"interrupted\n"


# -- Terminals


def on_terminal(argv, component="Probe", stdout_tty=True, keys=None, wait_for=b"", **variables):
    """Run the test program, remote-controlled, on a terminal; see support.on_terminal."""
    env = environment(component, TERM="xterm-256color", **variables)
    return run_on_terminal(argv, env, stdout_tty, keys, wait_for)


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
@pytest.mark.parametrize("pipes", [False, True], ids=["server", "pipes-only server"])
def test_ctrl_c(server, pipes):
    # A server with terminals gets Ctrl-C as a key. With a pipes-only server the client's
    # terminal stays cooked: Ctrl-C interrupts the client, which passes it on.
    target = server("Probe", pipes=pipes).target
    code, output, _ = on_terminal(["sleep", "--remote", target], keys=b"\x03", wait_for=b"started")
    assert code == 128 + signal.SIGINT
    assert output.endswith("interrupted\n")
