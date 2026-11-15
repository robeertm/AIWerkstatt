#!/bin/bash
# Set up (or update) the remote test runner. Run on the CLIENT machine; it configures the test machine over SSH.
#
# Required variables (no defaults with hosts):
#   RTR_TARGET     user@host of the test machine (needs an administrative SSH login for this setup step only)
#   RTR_FROM       source address as the test machine sees the client (used in from="...")
# Optional:
#   RTR_REMOTE_DIR folder on the test machine, relative to its home (default: test-runner)
#   RTR_KEY        key file to create/use on the client (default: ~/.ssh/test_runner_ed25519)
#   RTR_CONFIG     client config JSON to write (default: ~/.config/aiwerkstatt/test-runner.json)
#   RTR_PROFILES   profiles.json to install on the test machine (default: profiles.json next to this script,
#                  falls back to profiles.example.json only with RTR_ALLOW_EXAMPLE=1)
#
# Installs remote_test_receiver.py and profiles.json on the test machine and ONE line in its authorized_keys:
#   restrict,from="<RTR_FROM>",command="/usr/bin/python3 -I <dir>/remote_test_receiver.py" <public key>
# (restrict = no port/agent/X11 forwarding, no pty). Afterwards it proves that the key can run `status` but no
# command of its choice. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
: "${RTR_TARGET:?set RTR_TARGET=user@host}"
: "${RTR_FROM:?set RTR_FROM=<client address as seen by the test machine>}"
REMOTE_DIR=${RTR_REMOTE_DIR:-test-runner}
KEY=${RTR_KEY:-$HOME/.ssh/test_runner_ed25519}
CONFIG=${RTR_CONFIG:-$HOME/.config/aiwerkstatt/test-runner.json}
PROFILES=${RTR_PROFILES:-profiles.json}
if [ ! -f "$PROFILES" ]; then
  [ "${RTR_ALLOW_EXAMPLE:-}" = 1 ] && PROFILES=profiles.example.json || { echo "no profiles.json (copy profiles.example.json and edit it)"; exit 2; }
fi
case "$REMOTE_DIR$RTR_FROM" in *[!A-Za-z0-9._/,:*-]*) echo "unsafe characters in RTR_REMOTE_DIR or RTR_FROM"; exit 2;; esac

[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "test-runner@$(hostname -s)" -f "$KEY"
install -d -m 700 "$(dirname "$CONFIG")"
printf '{"target": "%s", "key": "%s"}\n' "$RTR_TARGET" "$KEY" > "$CONFIG.new" && chmod 600 "$CONFIG.new" && mv -f "$CONFIG.new" "$CONFIG"

REMOTE_HOME=$(ssh -o BatchMode=yes "$RTR_TARGET" 'echo $HOME')
ssh -o BatchMode=yes "$RTR_TARGET" "mkdir -p ~/$REMOTE_DIR && chmod 755 ~/$REMOTE_DIR"
for f in remote_test_receiver.py "$PROFILES"; do
  dest=$([ "$f" = remote_test_receiver.py ] && echo remote_test_receiver.py || echo profiles.json)
  mode=$([ "$dest" = profiles.json ] && echo 644 || echo 755)
  ssh -o BatchMode=yes "$RTR_TARGET" "cat > ~/$REMOTE_DIR/$dest.new && chmod $mode ~/$REMOTE_DIR/$dest.new && mv -f ~/$REMOTE_DIR/$dest.new ~/$REMOTE_DIR/$dest" < "$f"
done
PUB=$(cat "$KEY.pub")
LINE="restrict,from=\"$RTR_FROM\",command=\"/usr/bin/python3 -I $REMOTE_HOME/$REMOTE_DIR/remote_test_receiver.py\" $PUB"
# replace an old line of this key, back up first; the file stays 0600
printf '%s\n' "$LINE" | ssh -o BatchMode=yes "$RTR_TARGET" 'set -e; f=~/.ssh/authorized_keys; k=$(cat); s=$(printf "%s" "$k" | awk "{print \$(NF-1)}");
  cp -p "$f" "$f.bak-before-test-runner-$(date +%Y%m%d%H%M%S)"; grep -vF "$s" "$f" > "$f.new" || true; printf "%s\n" "$k" >> "$f.new";
  chmod 600 "$f.new"; mv -f "$f.new" "$f"; ls -1t ~/.ssh/authorized_keys.bak-before-test-runner-* 2>/dev/null | tail -n +4 | xargs -r rm -f'
echo "test machine set up: $RTR_TARGET:~/$REMOTE_DIR (forced command, restrict, from=$RTR_FROM)"

# Probe 1: the new key can read the status
AIWERKSTATT_TEST_RUNNER_CONFIG="$CONFIG" python3 remote_test.py --available && echo "probe status: ok"
# Probe 2: the key cannot run a command of its choice
if ssh -T -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$RTR_TARGET" "id" 2>/dev/null | grep -q '^uid='; then
  echo "ERROR: the test-runner key runs arbitrary commands"; exit 1
fi
echo "probe forced command: ok (arbitrary command refused)"
echo "done. Start the collector with the example unit: remote-test-collector.service"
