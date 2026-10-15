#!/usr/bin/env bash
# Install or upgrade the read-only status page on the Lightsail host
# (contrib/dashboard/app, dashboard plan PR 4). Run as root (sudo) from any
# directory; RUNBOOK section 7 walks through it. Idempotent: run it again
# after a deploy to pick up the new commit.
#
#   sudo /home/trader/TradingAgents/contrib/dashboard/schedule/install-web.sh
#
# What it does, and only this:
#   1. the dashboard system account (primary group dashboard, no shell), if missing;
#   2. a copy of the package for that account under /home/dashboard/app, taken
#      with `git archive` from the trader's checkout — the paths the page
#      imports and nothing else (no .env, no local.yaml, no stores);
#   3. that account's own venv under /home/dashboard/venv, installed from the
#      package's requirements.txt (pinned);
#   4. the swap and the unit file into /etc/systemd/system, verified, enabled,
#      started and probed with curl; a failure after the page was stopped
#      restores the previous tree and starts the page on it again.
# It never touches the trader's files, the exporter or the daemons.

set -euo pipefail

CHECKOUT="${CHECKOUT:-/home/trader/TradingAgents}"
ACCOUNT=dashboard
HOME_DIR=/home/$ACCOUNT
APP_DIR=$HOME_DIR/app
VENV_DIR=$HOME_DIR/venv
UNIT=dashboard-web.service
PYTHON="${PYTHON:-python3}"

if [ "$(id -u)" -ne 0 ]; then
    echo "install-web.sh: run as root (sudo)" >&2
    exit 1
fi
if [ ! -d "$CHECKOUT/.git" ]; then
    echo "install-web.sh: $CHECKOUT is not a git checkout (CHECKOUT=... to point elsewhere)" >&2
    exit 1
fi

# 1. the account. The group is the exporter's (RUNBOOK section 1.1: the
#    status directory's group); the account has it as primary group so the
#    0640 documents are readable.
if ! getent group $ACCOUNT >/dev/null; then
    groupadd --system $ACCOUNT
fi
if ! id -u $ACCOUNT >/dev/null 2>&1; then
    useradd --system --gid $ACCOUNT --home-dir "$HOME_DIR" --create-home \
        --shell /usr/sbin/nologin --comment "read-only status page" $ACCOUNT
fi
chmod 750 "$HOME_DIR"

# 2. the code: an archive of the committed tree, not a clone (the account
#    needs no deploy key and gets no history). The trader's checkout is read
#    as the trader, so root never opens its files by hand.
COMMIT=$(sudo -u trader git -C "$CHECKOUT" rev-parse --short HEAD)
rm -rf "$APP_DIR.new"
install -d -o $ACCOUNT -g $ACCOUNT -m 750 "$APP_DIR.new"
sudo -u trader git -C "$CHECKOUT" archive HEAD \
    contrib/__init__.py contrib/core contrib/dashboard \
    | tar -x -C "$APP_DIR.new"
# The exporter's tests and settings are not the page's business.
rm -rf "$APP_DIR.new/contrib/core/tests" "$APP_DIR.new/contrib/dashboard/tests" \
    "$APP_DIR.new/contrib/dashboard/configs"
echo "$COMMIT" > "$APP_DIR.new/COMMIT"
chown -R $ACCOUNT:$ACCOUNT "$APP_DIR.new"
chmod -R u=rX,g=rX,o= "$APP_DIR.new"

# 3. the venv, the account's own, from the pinned requirements. The page is
#    stopped first: pip writes into the venv the running page imports from
#    (a no-op while the pins hold, a few seconds when they moved), and the
#    tree is swapped next. From here a failure restores the old tree and
#    starts the page on it again (the trap), so the page is never left down
#    with its tree gone.
if systemctl is-active --quiet $UNIT; then
    systemctl stop $UNIT
fi
rollback() {
    echo "install-web.sh: failed at line $1; restoring the previous tree" >&2
    if [ -d "$APP_DIR.old" ]; then
        rm -rf "$APP_DIR"
        mv "$APP_DIR.old" "$APP_DIR"
    fi
    systemctl start $UNIT || true
    systemctl is-active $UNIT >&2 || true
}
trap 'rollback $LINENO' ERR
if [ ! -x "$VENV_DIR/bin/python" ]; then
    sudo -u $ACCOUNT -H "$PYTHON" -m venv "$VENV_DIR"
    # pip is brought up to date once, with the venv; a deploy that re-runs
    # this script must not take a new pip from PyPI every time.
    sudo -u $ACCOUNT -H "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
fi
# Pinned exactly, so this is a no-op (no index contact) when nothing moved.
sudo -u $ACCOUNT -H "$VENV_DIR/bin/python" -m pip install --quiet -r "$APP_DIR.new/contrib/dashboard/requirements.txt"

# 4. the swap and the unit.
rm -rf "$APP_DIR.old"
if [ -d "$APP_DIR" ]; then mv "$APP_DIR" "$APP_DIR.old"; fi
mv "$APP_DIR.new" "$APP_DIR"
install -m 644 "$APP_DIR/contrib/dashboard/schedule/$UNIT" /etc/systemd/system/$UNIT
# verify exits 0 on a directive this systemd does not know and only prints
# "Unknown key name": that is a unit installed with a hardening line dropped,
# which is a failure here (edit the committed unit, RUNBOOK section 7.2).
VERIFY=$(systemd-analyze verify /etc/systemd/system/$UNIT 2>&1 || true)
if [ -n "$VERIFY" ]; then
    echo "$VERIFY" >&2
    if echo "$VERIFY" | grep -qi "unknown key"; then
        false
    fi
fi
systemctl daemon-reload
systemctl enable $UNIT
systemctl start $UNIT
# Type=simple is "active" the moment the process forks: the page is up only
# when it answers. Fifteen seconds covers the imports on the small host.
for _ in $(seq 1 15); do
    if curl -sf -o /dev/null http://127.0.0.1:8787/; then
        break
    fi
    sleep 1
done
curl -sf -o /dev/null http://127.0.0.1:8787/ || {
    systemctl status $UNIT --no-pager >&2 || true
    journalctl -u $UNIT -n 20 --no-pager >&2 || true
    false
}
trap - ERR
rm -rf "$APP_DIR.old"
echo "dashboard-web: $COMMIT installed, $(systemctl is-active $UNIT); try: curl -s http://127.0.0.1:8787/ | head -5"
