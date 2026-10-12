"""Process + port plumbing shared by cellkit. No third-party dependencies."""
import os
import re
import socket
import subprocess
import time


def sh(cmd, **kw):
    kw.setdefault("shell", isinstance(cmd, str))
    return subprocess.run(cmd, **kw)


def port_open(port, host="127.0.0.1", timeout=0.3):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port(port, tries=45, delay=1):
    for _ in range(tries):
        if port_open(port):
            return True
        time.sleep(delay)
    return False


def _proc_pids_on_port(port):
    """Last resort: /proc/net/tcp* -> socket inode -> owning pid (no fuser/ss)."""
    want, inodes = f"{port:04X}", set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = open(f).read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            c = row.split()
            if len(c) > 9 and c[1].rsplit(":", 1)[-1] == want and c[3] == "0A":
                inodes.add(c[9])
    pids = set()
    for pid in os.listdir("/proc") if inodes else []:
        if not pid.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    t = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if t.startswith("socket:[") and t[8:-1] in inodes:
                    pids.add(int(pid))
                    break
        except OSError:
            continue
    return pids


def pids_on_port(port):
    """Who is listening on `port`: ss, then lsof, then a /proc scan."""
    out = sh(f"ss -tlnpH 2>/dev/null | grep ':{port} '", capture_output=True, text=True).stdout
    pids = {int(p) for p in re.findall(r"pid=(\d+)", out)}
    if pids:
        return pids
    out = sh(f"lsof -t -iTCP:{port} -sTCP:LISTEN 2>/dev/null", capture_output=True, text=True).stdout
    pids = {int(p) for p in out.split() if p.isdigit()}
    return pids or _proc_pids_on_port(port)


def kill_port(port):
    for _ in range(2):                       # second pass in case it respawned
        for pid in pids_on_port(port):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        time.sleep(0.3)


def tail(path, n=40):
    print(sh(f"tail -n {n} {path}", capture_output=True, text=True).stdout)


def regex_escape(value):
    """Escape PCRE metacharacters, staying backslash-light for nginx quotes."""
    return re.sub(r"([\\^$.?*+()\[\]{}|/])", r"\\\1", str(value))


def resolve_token(spec):
    import secrets
    if spec in ("", None, False, [], "none", "None"):
        return ""
    if spec == "auto":
        return secrets.token_urlsafe(24)
    return str(spec)


def spawn(cmd, log, cwd=None, env=None):
    """Start a long-lived child (survives the cell) with its output in `log`."""
    os.makedirs(os.path.dirname(log), exist_ok=True)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                            stdout=open(log, "w", buffering=1), stderr=subprocess.STDOUT,
                            start_new_session=True)
    return proc


def first_json(text):
    """Pull the first JSON object out of a body that may be SSE-framed."""
    import json
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    try:
        return json.loads(text)
    except Exception:
        return {}
