#!/bin/sh
# A tour of fireaid's help syntax, run on examples/pantry.py.
#
# Every command is printed before it runs. The pager is switched off so
# that the output is plain text.

cd "$(dirname "$0")" || exit 1
export PAGER=cat

demo() {
    printf '\n$ pantry.py %s\n' "$*"
    python3 pantry.py "$@"
}

echo '### An incomplete command gets the usage, and exit code 2'
demo
echo "(exit code $?)"
demo shelf

echo
echo '### Help at the top level, three ways'
demo --help
demo -h
demo help

echo
echo '### Help for a command, and Git-style help'
demo add --help
demo help add

echo
echo '### help is a command like any other, with help of its own'
demo help help

echo
echo '### Help for a group and a command within it'
demo shelf -h
demo help shelf label

echo
echo '### Fire itself would pass --help to a **kwargs function as a filter'
demo list --help

echo
echo '### -h stays the shortcut for a parameter starting with h'
demo search --help
demo search milk -h

echo
echo "### Fire's native syntax is untouched, and prints to stderr"
printf '\n$ pantry.py add -- --help 2>/dev/null | head -3\n'
python3 pantry.py add -- --help 2>/dev/null | head -3
echo '(nothing, it all went to stderr)'

echo
echo '### fireaid prints help to stdout, so it can be piped'
printf '\n$ pantry.py add --help 2>/dev/null | head -3\n'
python3 pantry.py add --help 2>/dev/null | head -3

echo
echo '### Help after arguments is help for the result, as in Fire'
printf '\n$ pantry.py add milk 2 --help | head -8\n'
python3 pantry.py add milk 2 --help | head -8
