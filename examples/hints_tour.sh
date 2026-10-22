#!/bin/sh
# A tour of how fireaid reads values by their type hints, run on
# examples/reminders.py.
#
# Every command is printed before it runs, with what it prints on
# stderr shown too.

cd "$(dirname "$0")" || exit 1
export PAGER=cat

demo() {
    printf '\n$ reminders.py %s\n' "$*"
    python3 reminders.py "$@" 2>&1
}

echo '### str: the title as typed, where Fire would make 007 the number 7'
demo add 007 2026-10-15

echo
echo '### date and time, in ISO 8601'
demo add Plants 2026-10-15 --at 08:30

echo
echo '### A duration, an Optional, and an Enum by name in any case'
demo add Stand-up 2026-10-19 --at 09:15 --every 1d --priority HIGH

echo
echo '### int with a prefix, and a duration in units'
demo snooze 0x3 --by 1h30m

echo
echo '### What a hint does not take is a usage error naming the parameter'
demo add Call 15.10.2026
echo "(exit code $?)"
demo snooze 3 --by soon
echo "(exit code $?)"
demo add Call 2026-10-15 --priority urgent
echo "(exit code $?)"
