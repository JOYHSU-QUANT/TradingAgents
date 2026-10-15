#!/usr/bin/env bash
# Install or upgrade the read-only status page on the Lightsail host
# (contrib/dashboard/app, dashboard plan PR 4). Run as root (sudo) from any
# directory; RUNBOOK section 7 walks through it. Idempotent: run it again
# after a deploy to pick up the new commit.
#
#   sudo /home/trader/TradingAgents/contrib/dashboard/schedule/install-web.sh
#
# What it does, and only this:
#   1. the dashboard system account (primary group dashboard, no shell), if
#      missing; its home is root's, readable by the account;
#   2. a root-owned copy of the package under /home/dashboard/app, taken with
#      `git archive` from the trader's checkout — the paths the page imports
#      and nothing else (no .env, no local.yaml, no stores) — and the unit
#      file from the same committed tree into /var/lib/dashboard-web;
#   3. that account's own venv under /home/dashboard/venv, installed from the
#      package's requirements.txt (pinned);
#   4. the unit verified where it is staged, the swap, the unit file into
#      /etc/systemd/system, enabled, started and probed with curl; a failure
#      after the page was stopped restores the previous tree and unit file and
#      starts the page on them again.
# It never touches the trader's files, the exporter or the daemons.

set -euo pipefail

CHECKOUT="${CHECKOUT:-/home/trader/TradingAgents}"
ACCOUNT=dashboard
HOME_DIR=/home/$ACCOUNT
APP_DIR=$HOME_DIR/app
VENV_DIR=$HOME_DIR/venv
UNIT=dashboard-web.service
# Root's own: the unit file staged for verification, and the previous one
# for a rollback. Never under the account's home, which pip writes into.
STATE_DIR=/var/lib/dashboard-web
# The staged unit keeps its real name (systemd-analyze verify refuses any
# other suffix), so it is staged in a directory of its own.
STAGE_DIR=$STATE_DIR/new
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
    useradd --system --gid $ACCOUNT --home-dir "$HOME_DIR" --no-create-home \
        --shell /usr/sbin/nologin --comment "read-only status page" $ACCOUNT
fi
# The home is root's, readable by the account (no skeleton files either):
# the tree under it is bound read-only into the unit anyway, and nothing
# the account runs (pip, the page) may replace what root installs from it.
# The venv alone is the account's, because pip writes there.
install -d -o root -g $ACCOUNT -m 750 "$HOME_DIR"
install -d -o root -g root -m 700 "$STATE_DIR"
rm -rf "$STAGE_DIR"
install -d -o root -g root -m 700 "$STAGE_DIR"

# 2. the code: an archive of the committed tree, not a clone (the account
#    needs no deploy key and gets no history). The trader's checkout is read
#    as the trader, so root never opens its files by hand.
COMMIT=$(sudo -u trader git -C "$CHECKOUT" rev-parse --short HEAD)
rm -rf "$APP_DIR.new"
install -d -o root -g $ACCOUNT -m 750 "$APP_DIR.new"
sudo -u trader git -C "$CHECKOUT" archive HEAD \
    contrib/__init__.py contrib/core contrib/dashboard \
    | tar -x -C "$APP_DIR.new"
# The exporter's tests and settings are not the page's business.
rm -rf "$APP_DIR.new/contrib/core/tests" "$APP_DIR.new/contrib/dashboard/tests" \
    "$APP_DIR.new/contrib/dashboard/configs"
echo "$COMMIT" > "$APP_DIR.new/COMMIT"
chown -R root:$ACCOUNT "$APP_DIR.new"
chmod -R u=rwX,g=rX,o= "$APP_DIR.new"
# The unit file, from the committed tree straight into root's state
# directory: the copy root verifies and installs is never one the account
# could have written.
sudo -u trader git -C "$CHECKOUT" show "HEAD:contrib/dashboard/schedule/$UNIT" > "$STAGE_DIR/$UNIT"

# 3. the venv, the account's own, from the pinned requirements. The page is
#    stopped first when it is installed, active or not (a unit waiting in
#    auto-restart would come back mid-swap): pip writes into the venv the
#    running page imports from (a no-op while the pins hold, a few seconds
#    when they moved), and the tree is swapped next. From the stop on, a
#    failure restores the previous tree and unit file and starts the page
#    on them again (the trap); the venv is not restored, so a pip failure
#    halfway through a pin change can still leave the page down, loudly.
command -v curl >/dev/null || { echo "install-web.sh: curl is needed for the probe" >&2; exit 1; }
# A .old left by a run killed mid-way (an ssh session dropping) must not be
# what a rollback restores: only this run's .old counts.
rm -rf "$APP_DIR.old"
UNIT_PREV="$STATE_DIR/$UNIT.prev"
rm -f "$UNIT_PREV"
if [ -f /etc/systemd/system/$UNIT ]; then
    cp /etc/systemd/system/$UNIT "$UNIT_PREV"
fi
# A unit not yet installed (the first run) has nothing to stop; one that is
# installed must stop, and loudly if it cannot.
if systemctl cat $UNIT >/dev/null 2>&1; then
    systemctl stop $UNIT
    systemctl reset-failed $UNIT 2>/dev/null || true
fi
rollback() {
    echo "install-web.sh: failed at line $1; restoring the previous page" >&2
    # A page started on the new tree (a probe that failed) must not keep
    # running while its tree is taken away under it.
    systemctl stop $UNIT 2>/dev/null || true
    if [ -d "$APP_DIR.old" ]; then
        rm -rf "$APP_DIR" || true
        mv "$APP_DIR.old" "$APP_DIR" || true
    fi
    if [ -f "$UNIT_PREV" ]; then
        install -m 644 "$UNIT_PREV" /etc/systemd/system/$UNIT || true
        systemctl daemon-reload || true
    fi
    systemctl start $UNIT || true
    echo "dashboard-web: $(systemctl is-active $UNIT || true) on $(cat "$APP_DIR/COMMIT" 2>/dev/null || echo '?')" >&2
}
trap 'rollback $LINENO' ERR
if [ ! -x "$VENV_DIR/bin/python" ]; then
    # The home is root's, so root makes the venv's directory for the account
    # (venv fills an empty one). The venv's bundled pip is enough for exact
    # pins; an unpinned `--upgrade pip` from PyPI is one moving part this
    # host does not need.
    install -d -o $ACCOUNT -g $ACCOUNT -m 750 "$VENV_DIR"
    sudo -u $ACCOUNT -H "$PYTHON" -m venv "$VENV_DIR"
fi
# Pinned exactly, so this is a no-op (no index contact) when nothing moved.
sudo -u $ACCOUNT -H "$VENV_DIR/bin/python" -m pip install --quiet --no-cache-dir -r "$APP_DIR.new/contrib/dashboard/requirements.txt"

# 4. the unit, verified where it is staged before it is installed: a verify
#    that fails, or that only warns "Unknown key name" (a hardening line this
#    systemd would drop), is a failure here; edit the committed unit instead
#    (RUNBOOK section 7.2).
STAGED="$STAGE_DIR/$UNIT"
VERIFY_STATUS=0
VERIFY=$(systemd-analyze verify "$STAGED" 2>&1) || VERIFY_STATUS=$?
if [ -n "$VERIFY" ]; then
    echo "$VERIFY" >&2
fi
if [ "$VERIFY_STATUS" -ne 0 ] || echo "$VERIFY" | grep -qi "unknown key"; then
    false
fi
# the swap, then the unit
if [ -d "$APP_DIR" ]; then mv "$APP_DIR" "$APP_DIR.old"; fi
mv "$APP_DIR.new" "$APP_DIR"
install -m 644 "$STAGED" /etc/systemd/system/$UNIT
systemctl daemon-reload
systemctl enable $UNIT
systemctl start $UNIT
# Type=simple is "active" the moment the process forks: the page is up only
# when it answers (curl -f fails on a 5xx). Fifteen seconds covers the
# imports on the small host.
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
rm -f "$UNIT_PREV"
rm -rf "$STAGE_DIR"
echo "dashboard-web: $COMMIT installed, $(systemctl is-active $UNIT); try: curl -s http://127.0.0.1:8787/ | head -5"
