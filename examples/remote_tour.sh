#!/bin/sh
# A tour of fireaid's remote control, run on examples/lamp.py with a
# server on this computer, for as long as the tour runs.
#
# Every command is printed before it runs, with what it prints on
# stderr shown too.

cd "$(dirname "$0")" || exit 1
export PAGER=cat
unset LAMP_REMOTE LAMP_SERVER_PASSWORD LAMP_ROOM

demo() {
    printf '\n$ %s\n' "$*"
    python3 "$@" 2>&1
}

echo '### Remote control is off unless the program turns it on in its call of'
echo '### fire.Fire. pantry.py does not: --remote is a flag like any other,'
echo '### passed to the command, and there is no server command.'
demo pantry.py list --remote 127.0.0.1
demo pantry.py server
echo "(exit code $?)"

echo
echo '### lamp.py does, for the commands it allows:'
echo '###     fire.Fire(Lamp, remote=fire.Remote(allow=("where", "on", "off", "brightness")))'
echo '### A server, here with its password in LAMP_SERVER_PASSWORD, out of the'
echo '### process list, and LAMP_ROOM to tell where a command runs:'
log=$(mktemp)
printf '\n$ LAMP_SERVER_PASSWORD=secret LAMP_ROOM=kitchen lamp.py server --port 0 --bind 127.0.0.1 &\n'
LAMP_SERVER_PASSWORD=secret LAMP_ROOM=kitchen python3 lamp.py server --port 0 --bind 127.0.0.1 2> "$log" &
server=$!
trap 'kill $server 2> /dev/null; rm -f "$log"' EXIT
tries=0
until grep -q "serving on" "$log"; do
    tries=$((tries + 1))
    if [ $tries -gt 100 ]; then cat "$log"; exit 1; fi
    sleep 0.1
done
port=$(sed -n 's/.*serving on [^:]*:\([0-9]*\).*/\1/p' "$log")
cat "$log"

echo
echo '### The same command, here and on the server'
demo lamp.py where
demo lamp.py where --remote "secret@127.0.0.1:$port"

echo
echo '### LAMP_REMOTE gives the server to every command line'
printf '\n$ LAMP_REMOTE=secret@127.0.0.1:%s lamp.py brightness 70\n' "$port"
LAMP_REMOTE="secret@127.0.0.1:$port" python3 lamp.py brightness 70 2>&1

echo
echo '### What is not allowed is refused, and so is a wrong password'
demo lamp.py firmware lamp-2.bin --remote "secret@127.0.0.1:$port"
echo "(exit code $?)"
demo lamp.py where --remote "wrong@127.0.0.1:$port"
echo "(exit code $?)"

echo
echo '### The help lists --remote among the flags'
printf '\n$ lamp.py brightness --help | sed -n "/^FLAGS/,\\$p"\n'
python3 lamp.py brightness --help | sed -n '/^FLAGS/,$p'

echo
echo "### The server's log"
kill $server
wait $server 2> /dev/null
cat "$log"
