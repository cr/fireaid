"""
The tour in examples/ runs, and shows what it says it shows.
"""

import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_tour():
    completed = subprocess.run(
        ["sh", str(EXAMPLES / "tour.sh")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin"},
    )
    assert completed.returncode == 0
    assert completed.stderr == ""
    out = completed.stdout
    assert "Usage: pantry.py <group|command>\n" in out
    assert "(exit code 2)" in out
    assert "  available commands:    add | help | list | remove | search\n" in out
    assert "pantry.py help - Show help for the program, or for a command." in out
    assert "Usage: pantry.py shelf <command>\n" in out
    assert out.count("pantry.py - Keep track of what is in the pantry.") == 3
    assert out.count("pantry.py add - Put an item into the pantry.") == 3
    assert "pantry.py shelf label - Label the shelf." in out
    assert "pantry.py list - List the pantry" in out
    assert "Searching for 'milk' everywhere." in out
    assert "(nothing, it all went to stderr)" in out
    assert 'The string "Added 2 x milk."' in out
