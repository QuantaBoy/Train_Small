"""Keep the public link near-live: every EVERY_MIN minutes, rebuild the snapshot (snapshot.py) and force-push it
as a single commit to the `live` branch, which Vercel serves (Vercel: Settings -> Git -> Production Branch: live).
main's history stays clean: the commit is built with a separate index and never touches the working branch.

    python live/publish.py          # loop; run beside the website and the feed
    python live/publish.py --once
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
EVERY_MIN = 10
FILES = ["public", "vercel.json", ".vercelignore"]


def git(*args, env=None) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, env=env, check=True, capture_output=True, text=True).stdout.strip()


def publish():
    subprocess.run([sys.executable, str(ROOT / "snapshot.py")], cwd=ROOT, check=True, capture_output=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(ROOT / ".git" / "live-index")}  # own index: main is untouched
    git("read-tree", "--empty", env=env)
    git("add", "-f", *FILES, env=env)
    tree = git("write-tree", env=env)
    commit = git("commit-tree", tree, "-m", f"Live snapshot {datetime.now():%d %b %H:%M}", env=env)
    git("push", "-q", "-f", "origin", f"{commit}:refs/heads/live")
    print(f"{datetime.now():%H:%M} published {commit[:7]}", flush=True)


if __name__ == "__main__":
    while True:
        try:
            publish()
        except subprocess.CalledProcessError as e:  # a failed push or build must not stop the loop
            print(f"{datetime.now():%H:%M} publish failed: {(e.stderr or '')[-300:]}", flush=True)
        if "--once" in sys.argv:
            break
        time.sleep(EVERY_MIN * 60)
