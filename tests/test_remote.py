"""
Remote control: a command line run through a server behaves as it does
locally, and the server runs only what it may.

On Windows, which has no pseudo-terminals, a server gives commands pipes.
FIREAID_REMOTE_TEST_NO_PTY makes a server or client anywhere behave like
one on Windows, so that the two kinds are tested against each other.
"""

import inspect
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import fireaid
import pytest
from fireaid import remote

try:
    import fcntl
    import pty
    import struct
    import termios
except ImportError:  # Windows
    pty = None

unix = pytest.mark.skipif(pty is None, reason="needs Unix terminals and signals")

TESTS = Path(__file__).parent
PASSWORD = "p@ss word"


def environment(component, **variables):
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR", "CLI_REMOTE")}
    env.update(
        FIREAID_TEST_MODULE="fireaid",
        FIREAID_TEST_COMPONENT=component,
        FIREAID_TEST_REMOTE="1",
        PAGER="cat",
        COLUMNS="80",
    )
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


class Server:
    def __init__(self, component, password, log, pipes=False, debug=False):
        # Loopback, so that no firewall asks about the tests.
        argv = ["server", "--port", "0", "--bind", "127.0.0.1"] + ([f"--password={PASSWORD}"] if password else [])
        argv += ["--debug"] if debug else []
        self.log = log
        self.process = subprocess.Popen(
            [sys.executable, "cli.py", *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log.open("w"),
            env=environment(component, FIREAID_REMOTE_TEST_NO_PTY="1" if pipes else ""),
            cwd=TESTS,
        )
        deadline = time.monotonic() + 10
        while "serving on" not in log.read_text():
            assert self.process.poll() is None, log.read_text()
            assert time.monotonic() < deadline, "the server does not start"
            time.sleep(0.05)
        self.setup_line, first = log.read_text().split("\n")[:2]  # the program's setup hook ran first
        self.pipes = first.endswith(", commands get pipes")
        self.port = int(first.rsplit(":", 1)[1].split(",")[0])
        self.target = f"{PASSWORD}@127.0.0.1:{self.port}" if password else f"127.0.0.1:{self.port}"

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
        ("foo -- --remote x", False),
        ("foo -- --interactive", True),
        ("foo -- -i", True),
        ("foo -- --int", True),
        ("foo -- --trace", False),
        ("foo -- -i -- x", False),  # Fire takes its flags from after the last separator
    ],
)
def test_refusal(command, refused):
    assert (remote.refusal(command.split(), ["firmware", "db drop"]) is not None) == refused


def test_open_server_without_a_password_warns(tmp_path):
    log = tmp_path / "log"
    process = subprocess.Popen(
        [sys.executable, "cli.py", "server", "--port", "0"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log.open("w"),
        env=environment("INSTANCE"), cwd=TESTS,
    )
    try:
        deadline = time.monotonic() + 10
        while "warning" not in log.read_text() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        process.terminate()
        process.wait(10)
    lines = log.read_text().splitlines()
    assert lines[0] == "SETUP debug=False"
    assert lines[1].startswith("cli.py: serving on 0.0.0.0:") and lines[1].endswith(", no password")
    assert lines[2] == ("cli.py: warning: anyone who can reach this port may run cli.py commands here; "
                        "--password asks them for a password, --bind 127.0.0.1 keeps it to this computer")


def test_server_setup_hook_and_debug(server):
    """The program's setup gets the --debug flag; the server then logs each client."""
    quiet, loud = server(), server(debug=True)
    assert quiet.setup_line == "SETUP debug=False" and loud.setup_line == "SETUP debug=True"
    assert run(["foo", "x", "--remote", loud.target])[:2] == (0, "foo name='x' count=1\n")
    log = loud.log.read_text()
    assert "connected" in log and "runs 'foo x', tty 000" in log and "command exited 0, client gets 0" in log
    run(["foo", "x", "--remote", quiet.target])
    assert "connected" not in quiet.log.read_text()


def test_server_listens_everywhere_by_default():
    """--bind narrows it; a password does not change where it listens."""
    command = fireaid._server_command(fireaid.Remote(), "t", "T", None)[0]
    parameters = inspect.signature(command).parameters
    assert parameters["bind"].default == "0.0.0.0"
    assert parameters["password"].default is None
    out = run(["help", "server"], "INSTANCE")[1]
    assert "--bind=BIND" in out and "all of this computer's by default" in out


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
    assert "     server\n       Serve the commands of this program" in out
    code, out, err = run(["help", "server"], "INSTANCE")
    assert "CLI_REMOTE" in out and "These commands only run locally: static." in out


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
        "foo -- --help",
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
    assert err.startswith("cli.py: 127.0.0.1:") and "is not available remotely" in err
    denied = "server" if command.startswith("server") else "static"
    assert f"cli.py: 127.0.0.1: '{denied}' is not available remotely" in server().log.read_text()


def test_password(server):
    s = server(password=True)
    assert run(["foo", "x", "--remote", s.target])[:2] == (0, "foo name='x' count=1\n")
    for target in (f"wrong@127.0.0.1:{s.port}", f"127.0.0.1:{s.port}"):
        code, out, err = run(["foo", "x", "--remote", target])
        assert (code, out) == (1, "")
        assert err == f"cli.py: 127.0.0.1:{s.port}: authentication failed\n"


def test_password_given_to_a_server_without_one_is_warned_about(server):
    s = server()
    assert "no password" in s.log.read_text().split("\n")[1]
    assert "warning" not in s.log.read_text()  # it listens on loopback
    code, out, err = run(["foo", "x", "--remote", f"pw@{s.target}"])
    assert (code, out) == (0, "foo name='x' count=1\n")
    assert err == f"warning: {s.target} asks for no password; the one given is not used\n"


def test_unreachable():
    code, out, err = run(["foo", "x", "--remote", "127.0.0.1:1"])
    assert code == 1
    assert err.startswith("cli.py: cannot reach 127.0.0.1:1:")


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
    assert run(["cat", "--remote", s.target], "Probe", input=b"hello\n", **like) == (0, "hello\n", ""), s.log.read_text()
    data = os.urandom(1 << 18).hex().encode()
    assert run(["cat", "--remote", s.target], "Probe", input=data, **like)[1] == data.decode()
    assert run(["fail", "--code", "7", "--remote", s.target], "Probe", **like)[0] == 7
    assert run(["tty", "--remote", s.target], "Probe", input=b"", **like) == (0, "000 -\n", ""), s.log.read_text()


def test_pipes_only_denied_and_password(server):
    s = server(pipes=True)
    code, out, err = run(["static", "bar", "--remote", s.target])
    assert (code, out) == (2, "") and "is not available remotely" in err


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
    """
    Run the test program with a terminal of 80x24 as stdin and stderr,
    and as stdout too unless stdout_tty is false: then stdout is a pipe.
    Writes keys to the terminal once wait_for has appeared there.
    Returns the exit code, the terminal's output and stdout's.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    try:
        process = subprocess.Popen(
            [sys.executable, "cli.py", *argv],
            stdin=slave,
            stdout=slave if stdout_tty else subprocess.PIPE,
            stderr=slave,
            env=environment(component, TERM="xterm-256color", **variables),
            cwd=TESTS,
            # Its own terminal, as from a shell: Ctrl-C typed there reaches it.
            start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
        )
    finally:
        os.close(slave)
    output = b""
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if keys is not None and wait_for in output:
                os.write(master, keys)
                keys = None
    finally:
        os.close(master)
    stdout = process.stdout.read().decode() if process.stdout else ""
    return process.wait(30), output.decode().replace("\r\n", "\n"), stdout


@unix
def test_terminal(server):
    target = server("Probe").target
    assert on_terminal(["tty", "--remote", target]) == on_terminal(["tty"]) == (0, "111 80x24\n", "")


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
    # Ctrl-C at a cooked terminal interrupts the client, which passes it on.
    code, output, _ = on_terminal(["sleep", "--remote", target], keys=b"\x03", wait_for=b"started")
    assert code == 128 + signal.SIGINT
    assert output.endswith("interrupted\n")


@unix
def test_terminal_for_stderr_and_a_pipe_for_stdout(server):
    target = server("Probe").target
    local = on_terminal(["color"], stdout_tty=False)
    assert local == (0, "\x1b[31mred\x1b[0m\n", "result\n")
    assert on_terminal(["color", "--remote", target], stdout_tty=False) == local
    assert on_terminal(["tty", "--remote", target], stdout_tty=False) == (0, "", "101 80x24\n")


@unix
def test_ctrl_c(server):
    target = server("Probe").target
    code, output, _ = on_terminal(["sleep", "--remote", target], keys=b"\x03", wait_for=b"started")
    assert code == 128 + signal.SIGINT
    assert output.endswith("interrupted\n")
