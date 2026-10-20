"""One bounded HTTP attempt. Private child process of core.sec_client.

No database connection, automatic redirect or HTTP retry lives in this worker.
The parent kills/reaps it on timeout; Linux parent-death signalling also closes
the socket if the coordinator is killed. Never invoke it as a collection job.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import signal
import sys
import time

import requests


def main() -> None:
    payload = json.load(sys.stdin)
    # Set parent-death behaviour before checking the parent, covering the race
    # where the parent exits between Popen and this program starting.
    if sys.platform != "linux":
        raise RuntimeError("SEC bounded transport currently requires Linux")
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
    if os.getppid() != payload["parent_pid"]:
        return
    remaining = payload["deadline"] - time.monotonic()
    if remaining <= 0:
        return
    # Default SIGALRM terminates the process even if a response trickles forever.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        with requests.Session() as session:
            session.trust_env = False  # no implicit proxies/netrc credentials
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
            with session.get(
                payload["url"], headers=payload["headers"],
                timeout=(min(5, remaining), min(30, remaining)),
                allow_redirects=False, stream=True,
            ) as response:
                try:
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 128 * 1024 * 1024:
                            raise ValueError("SEC response exceeds 128 MiB")
                        chunks.append(chunk)
                except requests.RequestException as exc:
                    # Headers arrived before the body failed: the parent's shared
                    # backoff needs the status and Retry-After the server sent.
                    result = {
                        "error": type(exc).__name__, "retryable": True,
                        "status": response.status_code,
                        "headers": dict(response.headers), "url": response.url,
                    }
                else:
                    result = {
                        "status": response.status_code, "headers": dict(response.headers),
                        "encoding": response.encoding, "url": response.url,
                        "body": base64.b64encode(b"".join(chunks)).decode("ascii"),
                    }
    except requests.RequestException as exc:
        result = {"error": type(exc).__name__, "retryable": True}
    except Exception as exc:
        result = {"error": str(exc), "retryable": False}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
