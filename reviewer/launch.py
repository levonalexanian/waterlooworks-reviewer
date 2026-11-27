"""Open the separate jobs reviewer on port 8767."""
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8767'


def ready():
    try:
        with urlopen(URL + '/api/jobs', timeout=1) as response:
            return isinstance(json.load(response), list)
    except (OSError, ValueError):
        return False


def main():
    if not ready():
        logs = ROOT / 'local'
        logs.mkdir(exist_ok=True, mode=0o700)
        with (logs / 'server.log').open('ab') as log:
            subprocess.Popen([sys.executable, str(ROOT / 'server.py')], cwd=ROOT,
                             stdout=log, stderr=log, start_new_session=True)
        for _ in range(30):
            if ready():
                break
            time.sleep(.2)
        else:
            raise SystemExit('Could not start the jobs reviewer on port 8767. See local/server.log.')
    webbrowser.open(URL)


if __name__ == '__main__':
    main()
