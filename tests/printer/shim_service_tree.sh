#!/bin/sh
# On-printer test of files/system/supervisorctl with fake services only.
#
#   sh tests/printer/shim_service_tree.sh <shim>
#
# Touches no real service: init scripts, pid files and programs live in a
# temp dir under /tmp, and the shim is pointed there with SHIM_INIT_DIR and
# SHIM_RUN_DIR. Two cases from 2026-11-05:
#  1. a plugin whose init script starts a sh wrapper that runs the program as
#     a child (OctoEverywhere, OctoApp): restart must leave exactly one
#     program, and stop none;
#  2. "restart moonraker" issued by Moonraker itself, which on SIGTERM
#     cancels the running shim (SIGINT, wait up to 2 s) and lingers before
#     exiting: a new instance must come up.
# Prints PASS/FAIL per check; exit code 1 if any check fails.

SHIM="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
T="$(mktemp -d /tmp/shimtest.XXXXXX)"
mkdir -p "$T/init" "$T/run" "$T/bin"
cp /bin/busybox "$T/bin/sh"            # own exe path, like moonraker's python
export SHIM_INIT_DIR="$T/init" SHIM_RUN_DIR="$T/run"
FAILS=0
check() { if [ "$2" = 0 ]; then echo "PASS  $1"; else echo "FAIL  $1 $3"; FAILS=$((FAILS + 1)); fi; }
alive() { [ -d "/proc/$1" ]; }
wait_for() { i=0; while [ $i -lt "$2" ]; do eval "$1" && return 0; usleep 100000; i=$((i + 1)); done; return 1; }
count_progs() { n=0; for s in /proc/[0-9]*/cmdline; do { tr '\0' ' ' < "$s"; } 2>/dev/null | grep -q "^sleep 1001 " && n=$((n + 1)); done; echo $n; }

# --- fake plugin: wrapper sh runs "sleep 1001" as a child ----------------
cat > "$T/wrapper.sh" <<'W'
#!/bin/sh
sleep 1001
exit $?
W
chmod +x "$T/wrapper.sh"
cat > "$T/init/S66fakeplugin_service" <<I
#!/bin/sh
PID_FILE=$T/run/fakeplugin.pid
case "\$1" in
  start) start-stop-daemon -S -q -b -m -p \$PID_FILE --exec $T/wrapper.sh ;;
  stop) start-stop-daemon -K -q -p \$PID_FILE ;;
  restart) start-stop-daemon -K -q -p \$PID_FILE; sleep 1; start-stop-daemon -S -q -b -m -p \$PID_FILE --exec $T/wrapper.sh ;;
esac
I
chmod +x "$T/init/S66fakeplugin_service"

"$SHIM" start fakeplugin
wait_for '[ "$(count_progs)" = 1 ]' 30
old_wrapper="$(cat "$T/run/fakeplugin.pid")"
check "plugin started (1 program)" $([ "$(count_progs)" = 1 ]; echo $?)
"$SHIM" restart fakeplugin
wait_for '[ "$(count_progs)" = 1 ] && [ "$(cat $T/run/fakeplugin.pid)" != "$old_wrapper" ]' 50
check "plugin restart leaves exactly 1 program" $([ "$(count_progs)" = 1 ]; echo $?) "(found $(count_progs))"
check "plugin restart replaced the wrapper" $(! alive "$old_wrapper"; echo $?)
"$SHIM" stop fakeplugin
wait_for '[ "$(count_progs)" = 0 ]' 30
check "plugin stop leaves no program" $([ "$(count_progs)" = 0 ]; echo $?) "(found $(count_progs))"

# --- fake moonraker: restarts itself through the shim --------------------
cat > "$T/moon.sh" <<M
#!/bin/sh
echo \$\$ >> $T/moon_starts
child=""
on_term() {
  # Moonraker's shutdown: cancel the running shim, then linger (teardown).
  if [ -n "\$child" ]; then
    kill -INT \$child 2>/dev/null
    i=0; while [ \$i -lt 20 ] && [ -d /proc/\$child ]; do usleep 100000; i=\$((i + 1)); done
    kill -TERM \$child 2>/dev/null
  fi
  usleep 1500000
  exit 0
}
trap on_term TERM
while :; do
  if [ -f $T/trigger ]; then
    rm -f $T/trigger
    "$SHIM" restart moonraker &
    child=\$!
  fi
  usleep 100000
done
M
cat > "$T/init/S56moonraker_service" <<I
#!/bin/sh
PID_FILE=$T/run/moonraker.pid
start() { start-stop-daemon -S -q -b -m -p \$PID_FILE --exec $T/bin/sh -- $T/moon.sh; }
stop() { start-stop-daemon -K -q -p \$PID_FILE; }
case "\$1" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
esac
I
chmod +x "$T/init/S56moonraker_service"

"$T/init/S56moonraker_service" start
wait_for '[ -s "$T/moon_starts" ]' 30
first="$(cat "$T/run/moonraker.pid")"
touch "$T/trigger"
wait_for '[ "$(wc -l < $T/moon_starts)" -ge 2 ] && alive "$(cat $T/run/moonraker.pid)"' 150
now="$(cat "$T/run/moonraker.pid")"
check "moonraker self-restart: old instance gone" $(! alive "$first"; echo $?)
check "moonraker self-restart: new instance up" $([ "$now" != "$first" ] && alive "$now"; echo $?) \
  "(starts: $(wc -l < "$T/moon_starts"))"

# --- cleanup ---------------------------------------------------------------
for pf in "$T"/run/*.pid; do
  p="$(cat "$pf" 2>/dev/null)"
  [ -n "$p" ] && kill "$p" 2>/dev/null
done
usleep 2000000
for s in /proc/[0-9]*/cmdline; do
  if { tr '\0' ' ' < "$s"; } 2>/dev/null | grep -qE "^sleep 1001 |$T/moon.sh"; then
    p="${s%/cmdline}"; kill -9 "${p##*/}" 2>/dev/null
  fi
done
rm -rf "$T"
echo "failures: $FAILS"
[ "$FAILS" = 0 ]
