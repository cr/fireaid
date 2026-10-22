#!/usr/bin/env python3
"""
A lamp plugged into another computer, to show off fireaid's remote control.

Remote control is off unless a program turns it on in its own call of
fire.Fire, as this one does at the bottom. Then it has a server command,
and every command line takes --remote to run on such a server. Without
it, as in pantry.py, there is no server and --remote is a flag like any
other. This one also names the commands that run remotely: firmware is
not among them, and so stays at the lamp, as would a command added later.

Run examples/remote_tour.sh to see all of the below on this computer,
or try, in one terminal:

    LAMP_SERVER_PASSWORD=secret LAMP_ROOM=kitchen lamp.py server --bind 127.0.0.1

and in another:

    lamp.py where
    lamp.py where --remote secret@127.0.0.1
    LAMP_REMOTE=secret@127.0.0.1 lamp.py brightness 70
    lamp.py firmware lamp-2.bin --remote secret@127.0.0.1
    lamp.py help server
"""

import os

import fireaid as fire


class Lamp:
    """A lamp, as far as saying what it would do."""

    def where(self):
        """Say where the command runs: LAMP_ROOM names the room."""
        return os.environ.get("LAMP_ROOM", "no room: LAMP_ROOM is not set here")

    def on(self):
        """Switch the lamp on."""
        return "lamp on"

    def off(self):
        """Switch the lamp off."""
        return "lamp off"

    def brightness(self, level: int):
        """Set the brightness.

        Args:
            level: From 0 to 100.
        """
        return f"brightness {max(0, min(level, 100))}%"

    def firmware(self, path: str):
        """Flash new firmware, which only ever happens at the lamp itself.

        Args:
            path: The firmware file.
        """
        return f"would flash {path}"


if __name__ == "__main__":
    # Remote control is on because, and only because, it is asked for here,
    # and only for the commands allowed: firmware stays at the lamp.
    fire.Fire(Lamp, remote=fire.Remote(allow=("where", "on", "off", "brightness")))
