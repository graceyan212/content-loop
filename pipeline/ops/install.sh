#!/bin/bash
# Install the Dani loop on the box. Idempotent — safe to re-run.
set -euo pipefail
URL="$1"

install -d -o dani -g dani -m 755 /opt/dani
install -d -o dani -g dani -m 755 /var/log/dani
install -d -o root -g dani -m 750 /etc/dani

echo "== fetching package =="
curl -fsSL "$URL" -o /tmp/dani-deploy.tgz
echo "bytes: $(stat -c%s /tmp/dani-deploy.tgz)"

echo "== unpacking =="
rm -rf /opt/dani/ugc-pipeline /opt/dani/danielle
tar xzf /tmp/dani-deploy.tgz -C /opt/dani
rm -f /tmp/dani-deploy.tgz
chown -R dani:dani /opt/dani

echo "== python deps =="
/usr/local/bin/python3 -m pip install --quiet --upgrade pip
/usr/local/bin/python3 -m pip install --quiet Pillow certifi
/usr/local/bin/python3 -c "import PIL, certifi; print('Pillow', PIL.__version__, '| certifi ok')"

echo "== fonts =="
# overlay.ensure_fonts() downloads Anton / DM Sans / TikTok Sans on first use;
# do it now so the first real cycle isn't also a network-dependent font fetch
cd /opt/dani/ugc-pipeline
sudo -u dani /usr/local/bin/python3 -c "
import sys; sys.path.insert(0,'render')
import overlay; overlay.ensure_fonts(verbose=True)
print('fonts present:', __import__('os').listdir('render/fonts'))
"

echo "== persona registry =="
# personas.json holds absolute paths from the dev machine; repoint at /opt/dani
sudo -u dani /usr/local/bin/python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/opt/dani/ugc-pipeline/personas.json')
d = json.load(open(p))
def fix(v):
    if isinstance(v, str) and '/Desktop/alpha/GTM/' in v:
        return '/opt/dani/' + v.split('/Desktop/alpha/GTM/', 1)[1]
    return v
def walk(o):
    if isinstance(o, dict):  return {k: walk(fix(v)) for k, v in o.items()}
    if isinstance(o, list):  return [walk(fix(x)) for x in o]
    return fix(o)
json.dump(walk(d), open(p, 'w'), indent=2)
print(open(p).read()[:400])
PY

echo "== systemd =="
# Every *-loop.{service,timer} in ops/, so adding a persona is adding two files
# here rather than editing this script and forgetting one of them.
cp /opt/dani/ugc-pipeline/ops/*-loop.service /etc/systemd/system/
cp /opt/dani/ugc-pipeline/ops/*-loop.timer   /etc/systemd/system/
systemctl daemon-reload
ls /etc/systemd/system/*-loop.timer
echo "units installed (NOT enabled — that is a separate, deliberate step)"

echo "== done =="
ls -la /opt/dani/
