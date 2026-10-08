#!/bin/sh
# Install, or upgrade, the contrib/uniswap_v3 paper runs on the Lightsail
# host (RUNBOOK.md, section 0):
#
#     sudo sh lightsail-install.sh <commit>
#
# As root it stops the timer, has the trader half below run, installs the
# units from the checkout into /etc/systemd/system and puts the timer back
# the way it found it: running if it was running, and otherwise left for
# `systemctl enable --now` once the store and the runs are there. As trader
# (which the root half runs it as, with --as-trader) it clones the checkout
# from the Hyperliquid checkout's origin, with that checkout's deploy key
# (its core.sshCommand) set in the clone, or from REPO_URL when set (sudo
# drops the caller's variables, so `sudo REPO_URL=<url> sh ...`), or fetches
# it, detaches it at <commit>, makes or updates its own venv, writes a
# template of .env and of the visit's settings when they are not there, and
# runs the package's tests: a checkout whose tests fail is left at <commit>
# with the timer stopped, to go back from by hand (the commit it came from
# is printed). A second run upgrades, and may be run from the checkout it
# rewrites: the whole script is parsed before any of it runs. Nothing here
# touches the Hyperliquid checkout, its venv, its service or its store.
set -eu

OWNER=trader
CHECKOUT=/home/trader/uniswap-paper
# The Hyperliquid checkout: the one remote the host has a deploy key for.
SOURCE=/home/trader/TradingAgents
DATA=/home/trader/data/uniswap
UNIT=uniswap-v3-paper
SCHEDULE=contrib/uniswap_v3/schedule
SELF=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")

# The Hyperliquid checkout reaches its origin with a deploy key its own config
# names (core.sshCommand), not one in ~/.ssh/config, which the trader has none
# of (#340). Prints that setting when $1 is that origin, for a checkout of it
# to carry; nothing for another URL (REPO_URL), which brings its own
# credential, or none.
deploy_key() {
    if [ "$1" = "$(git -C "$SOURCE" remote get-url origin 2>/dev/null)" ]; then
        git -C "$SOURCE" config --get core.sshCommand || true
    fi
}

as_trader() {
    before=
    if [ -d "$CHECKOUT/.git" ]; then
        # A clone that was cut short has no HEAD to go back to.
        before=$(git -C "$CHECKOUT" rev-parse --short HEAD 2>/dev/null || true)
    else
        url=${REPO_URL:-$(git -C "$SOURCE" remote get-url origin)}
        # Without any credential a URL may carry before its @.
        echo "cloning ${url##*@}"
        git init --quiet "$CHECKOUT"
        git -C "$CHECKOUT" remote add origin "$url"
    fi
    cd "$CHECKOUT"
    # The key goes in before the first fetch, and stays for every fetch after;
    # a checkout from before the key travelled gets it here too, one set by
    # hand is left as it is.
    ssh_command=$(deploy_key "$(git remote get-url origin)")
    if [ -n "$ssh_command" ] && ! git config --get core.sshCommand >/dev/null; then
        git config core.sshCommand "$ssh_command"
        echo "core.sshCommand set from the Hyperliquid checkout"
    fi
    git fetch --quiet origin
    git checkout --quiet --detach "$1"
    echo "checkout at $(git rev-parse --short HEAD): $(git log -1 --format=%s)${before:+ (was at $before)}"
    if [ ! -x .venv/bin/python ]; then
        python3 -m venv .venv
    fi
    .venv/bin/pip install --quiet -e ".[dev]" -r contrib/uniswap_v3/requirements.txt
    mkdir -p "$DATA"
    if [ ! -f .env ]; then
        (umask 077 && printf 'ETH_RPC_URL=\nOPENROUTER_API_KEY=\n' >.env)
        echo "wrote $CHECKOUT/.env"
    fi
    for key in ETH_RPC_URL OPENROUTER_API_KEY; do
        # A line with a value: not missing, not empty, not an empty pair of quotes;
        # a quoted value is one too.
        if ! grep -qE "^$key=([^[:space:]\"']|\"[^\"]|'[^'])" .env; then
            echo "warning: $CHECKOUT/.env has no $key with a value: the visits need it"
        fi
    done
    if [ ! -f "$SCHEDULE/paper-visit.local.sh" ]; then
        printf 'DB="%s/paper.db"\nLOG="%s/paper-visits.log"\nPYTHON="%s/.venv/bin/python"\n' \
            "$DATA" "$DATA" "$CHECKOUT" >"$SCHEDULE/paper-visit.local.sh"
        echo "wrote $CHECKOUT/$SCHEDULE/paper-visit.local.sh"
    fi
    .venv/bin/python -m pytest -q -m "not smoke" contrib/uniswap_v3/tests
}

main() {
    if [ "${1:-}" = "--as-trader" ]; then
        as_trader "${2:?a commit}"
        return 0
    fi
    if [ $# -ne 1 ]; then
        echo "usage: sudo sh $0 <commit>" >&2
        return 2
    fi
    if [ "$(id -u)" -ne 0 ]; then
        echo "run it with sudo: it installs the units" >&2
        return 2
    fi

    # No visit starts while the checkout changes under it; one that is running
    # finishes first. A oneshot that is running is "activating", not "active",
    # so the state is read, not is-active's exit code. The timer goes back the
    # way it was found: an upgrade does not decide whether visits run.
    was=$(systemctl is-active "$UNIT.timer" 2>/dev/null || true)
    trap left_stopped EXIT
    if [ "$was" = active ]; then
        systemctl stop "$UNIT.timer"
    fi
    case "$(systemctl show -p ActiveState --value "$UNIT.service" 2>/dev/null)" in
    active | activating)
        if [ "$was" = active ]; then systemctl start "$UNIT.timer"; fi
        echo "a visit is running (systemctl status $UNIT.service): run this again when it is done" >&2
        return 3
        ;;
    esac
    cd /
    # sudo resets the environment: REPO_URL is handed on by name.
    sudo -u "$OWNER" -H env "REPO_URL=${REPO_URL:-}" sh "$SELF" --as-trader "$1"
    install -m 644 "$CHECKOUT/$SCHEDULE/$UNIT.service" "$CHECKOUT/$SCHEDULE/$UNIT.timer" /etc/systemd/system/
    systemctl daemon-reload
    if [ "$was" = active ]; then
        systemctl enable --quiet --now "$UNIT.timer"
        systemctl list-timers "$UNIT.timer" --no-pager
    else
        echo "the timer is not running, as it was not before: once the store and the runs are there (RUNBOOK.md), sudo systemctl enable --now $UNIT.timer"
    fi
}

# Said on the way out, whatever the path out, when a timer that was running
# was stopped here and not put back.
left_stopped() {
    if [ "${was:-}" = active ] && ! systemctl is-active --quiet "$UNIT.timer"; then
        echo "the timer was running and is left stopped; a rerun does not start it: rerun this, then sudo systemctl enable --now $UNIT.timer, or sudo systemctl start $UNIT.timer to go back as it was" >&2
    fi
}

# Everything above is parsed before any of it runs, and nothing follows: the
# upgrade rewrites this file in the checkout under the very shell reading it.
main "$@"
exit
