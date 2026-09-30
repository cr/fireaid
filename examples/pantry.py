#!/usr/bin/env python3
"""
A pantry manager, to show off fireaid's help.

Run examples/tour.sh to see all of the below, or try:

    pantry.py --help
    pantry.py help
    pantry.py help add
    pantry.py add --help
    pantry.py shelf -h
    pantry.py help shelf label
    pantry.py list --help
    pantry.py search --help
    pantry.py search milk -h
    pantry.py add -- --help
"""

import fireaid as fire


class Shelf:
    """A shelf of the pantry."""

    def label(self, text):
        """Label the shelf.

        Args:
            text: The text to write on the label.
        """
        return f"Shelf labelled {text!r}."

    def clear(self):
        """Take everything off the shelf."""
        return "Shelf cleared."


class Pantry:
    """Keep track of what is in the pantry.

    Ask for help with --help, -h or help, at any level:

        pantry.py help
        pantry.py add --help
        pantry.py help shelf label
    """

    def __init__(self):
        self.shelf = Shelf()

    def add(self, item, quantity=1, expires=None):
        """Put an item into the pantry.

        Args:
            item: What to put in.
            quantity: How many of it.
            expires: A date, or nothing for things that keep.
        """
        when = f", best before {expires}" if expires else ""
        return f"Added {quantity} x {item}{when}."

    def remove(self, item, quantity=1):
        """Take an item out of the pantry."""
        return f"Removed {quantity} x {item}."

    def list(self, **filters):
        """List the pantry, optionally filtered.

        Filters are free-form, such as --shelf=top or --expired=true.
        Fire would take --help for a filter here; fireaid shows help.
        """
        if filters:
            return f"Listing items where {filters}."
        return "Listing all items."

    def search(self, pattern, hidden=False):
        """Search the pantry.

        -h is Fire's shortcut for --hidden, so 'search milk -h' searches
        hidden items, while 'search --help' shows this help.

        Args:
            pattern: A substring of the item name.
            hidden: Also search items hidden at the back.
        """
        where = "everywhere" if hidden else "in plain sight"
        return f"Searching for {pattern!r} {where}."


if __name__ == "__main__":
    # An instance rather than the class: Fire then lists the commands in
    # the top-level help, and fireaid sees members created in __init__.
    fire.Fire(Pantry())
