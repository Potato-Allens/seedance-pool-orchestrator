# -*- coding: utf-8 -*-
"""Release pool accounts that were leased by local shards but are not tied to a
server job (those leak as status='busy' when a shard is killed)."""

import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.abspath(_os.path.join(_HERE, "..", ".."))
_SRV = _os.path.join(_ROOT, "src", "server")
if _SRV not in _sys.path: _sys.path.insert(0, _SRV)
_os.environ.setdefault("PP_HOME", _ROOT)
APP_DIR = _os.environ.get("APP_DIR", _os.path.join(_ROOT, "var"))
LOG_DIR = _os.environ.get("LOG_DIR", _os.path.join(_ROOT, "logs"))
OUT_DIR = _os.environ.get("OUT_DIR", _os.path.join(_ROOT, "outputs"))
for _d in (APP_DIR, LOG_DIR, OUT_DIR): _os.makedirs(_d, exist_ok=True)

import paramiko, sys
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(os.environ.get("POOL_HOST", "127.0.0.1"), 22, "root", os.environ.get("POOL_SSH_PASSWORD", ""), timeout=15)
src = ("python3 - <<'EOF'\n"
"import sqlite3, time\n"
"d=sqlite3.connect('/opt/pippit-pool/data/pool.db')\n"
"before=d.execute(\"select count(*) from accounts where status='busy'\").fetchone()[0]\n"
"rows=d.execute(\"select uid from accounts where status='busy' and uid not in \"\n"
"               \"(select account_uid from jobs where status in ('running','queued') and account_uid is not null)\").fetchall()\n"
"for (u,) in rows:\n"
"    d.execute(\"update accounts set status='ready', cooldown_until=max(cooldown_until, ?) where uid=?\", (time.time()+60, u))\n"
"d.commit()\n"
"after=d.execute(\"select count(*) from accounts where status='busy'\").fetchone()[0]\n"
"print('busy before', before, 'released', len(rows), 'busy after', after)\n"
"EOF")
i,o,e = c.exec_command(src, timeout=60)
sys.stdout.write(o.read().decode()); sys.stdout.write(e.read().decode())
c.close()
