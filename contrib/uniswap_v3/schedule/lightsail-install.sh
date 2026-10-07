#!/bin/sh
# Install, or upgrade, the contrib/uniswap_v3 paper runs on the Lightsail
# host (RUNBOOK.md, section 0):
#
#     sudo sh lightsail-install.sh <commit>
#
# As root it stops the timer, has the trader half below run, installs the
# units from the checkout into /etc/systemd/system and starts the timer
# again when a store is there. As trader (which the root half runs it as,
# with --as-trader) it clones the checkout from the Hyperliquid checkout's
# origin or fetches it, detaches it at <commit>, makes or updates its own
# venv, writes a template of .env and of the visit's settings when they are
# not there, and runs the package's tests: a checkout whose tests fail is
# left at <commit> with the timer stopped, to go back from by hand.
# A second run upgrades. Nothing here touches the Hyperliquid checkout, its
# venv, its service or its store.
set -eu

OWNER=trader
CHECKOUT=/home/trader/uniswap-paper
# The Hyperliquid checkout: the one remote the host has a deploy key for.
SOURCE=/home/trader/TradingAgents
DATA=/home/trader/data/uniswap
UNIT=uniswap-v3-paper
SCHEDULE=contrib/uniswap_v3/schedule
SELF=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")

as_trader() {
    if [ ! -d "$CHECKOUT/.git" ]; then
        git clone --quiet --no-checkout "$(git -C "$SOURCE" remote get-url origin)" "$CHECKOUT"
    fi
    cd "$CHECKOUT"
    git fetch --quiet origin
    git checkout --quiet --detach "$1"
    echo "checkout at $(git rev-parse --short HEAD): $(git log -1 --format=%s)"
    if [ ! -x .venv/bin/python ]; then
        python3 -m venv .venv
    fi
    .venv/bin/pip install --quiet -e ".[dev]" -r contrib/uniswap_v3/requirements.txt
    mkdir -p "$DATA"
    if [ ! -f .env ]; then
        (umask 077 && printf 'ETH_RPC_URL=\nOPENROUTER_API_KEY=\n' >.env)
        echo "wrote $CHECKOUT/.env: fill in ETH_RPC_URL and OPENROUTER_API_KEY"
    fi
    if [ ! -f "$SCHEDULE/paper-visit.local.sh" ]; then
        printf 'DB="%s/paper.db"\nLOG="%s/paper-visits.log"\nPYTHON="%s/.venv/bin/python"\n' \
            "$DATA" "$DATA" "$CHECKOUT" >"$SCHEDULE/paper-visit.local.sh"
        echo "wrote $CHECKOUT/$SCHEDULE/paper-visit.local.sh"
    fi
    .venv/bin/python -m pytest -q -m "not smoke" contrib/uniswap_v3/tests
}

if [ "${1:-}" = "--as-trader" ]; then
    as_trader "${2:?a commit}"
    exit 0
fi
if [ $# -ne 1 ]; then
    echo "usage: sudo sh $0 <commit>" >&2
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "run it with sudo: it installs the units" >&2
    exit 2
fi

# No visit starts while the checkout changes under it; one that is running
# finishes first. A oneshot that is running is "activating", not "active",
# so the state is read, not is-active's exit code.
systemctl stop "$UNIT.timer" 2>/dev/null || true
case "$(systemctl show -p ActiveState --value "$UNIT.service" 2>/dev/null)" in
active | activating)
    systemctl start "$UNIT.timer" 2>/dev/null || true
    echo "a visit is running (systemctl status $UNIT.service): run this again when it is done" >&2
    exit 3
    ;;
esac
sudo -u "$OWNER" -H sh "$SELF" --as-trader "$1"
install -m 644 "$CHECKOUT/$SCHEDULE/$UNIT.service" "$CHECKOUT/$SCHEDULE/$UNIT.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet "$UNIT.timer"
if [ -f "$DATA/paper.db" ]; then
    systemctl start "$UNIT.timer"
    systemctl list-timers "$UNIT.timer" --no-pager
else
    echo "no store at $DATA/paper.db yet: copy one and open the runs (RUNBOOK.md), then: sudo systemctl start $UNIT.timer"
fi
