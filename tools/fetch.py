#!/usr/bin/env python3
"""Download a listed subset of the input corpus for offline development (resumable).

    python3 tools/fetch.py --list state/fetch-list.txt --manifest state/manifest.tsv --out input

The list holds one manifest path per line.  Files already present with the right size
are skipped.  The input stays private on this machine."""
import argparse
import concurrent.futures
import http.client
import os
import sys
import threading
import time
import urllib.parse

UA = "machina-deid-fetch/1.0"
_local = threading.local()


def get(host, urlpath, size):
    last = ""
    for attempt in range(6):
        try:
            conn = getattr(_local, "conn", None)
            if conn is None:
                conn = _local.conn = http.client.HTTPSConnection(host, timeout=180)
            conn.request("GET", "/" + urlpath.lstrip("/"), headers={"User-Agent": UA})
            r = conn.getresponse()
            body = r.read()
            if r.status == 200 and len(body) == size:
                return body
            last = "HTTP %d, %d bytes" % (r.status, len(body))
            if r.status == 404:
                break
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None
        time.sleep(1 + attempt)
    raise RuntimeError(last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default="https://machina-input.joinbuilders.org")
    ap.add_argument("--jobs", type=int, default=24)
    a = ap.parse_args()
    host = urllib.parse.urlparse(a.base).netloc
    want = set(l.rstrip("\n") for l in open(a.list, encoding="utf-8") if l.strip())
    todo = []
    with open(a.manifest, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if p[0] not in want:
                continue
            size = int(p[1] or 0)
            url = p[3] if len(p) > 3 and p[3] else "corpus/" + urllib.parse.quote(p[0])
            dst = os.path.join(a.out, p[0])
            if os.path.exists(dst) and os.path.getsize(dst) == size:
                continue
            todo.append((p[0], size, url, dst))
    total = sum(t[1] for t in todo)
    print("%d files to fetch (%.2f GB); %d listed" % (len(todo), total / 1e9, len(want)), flush=True)
    done = [0, 0, 0]
    lock = threading.Lock()
    t0 = time.time()

    def one(t):
        path, size, url, dst = t
        try:
            body = get(host, url, size) if size else b""
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".part"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, dst)
            ok = True
        except Exception as exc:
            ok = False
            print("FAILED %s: %s" % (path, exc), flush=True)
        with lock:
            done[0] += 1
            done[1] += size
            done[2] += 0 if ok else 1
            if done[0] % 500 == 0 or done[0] == len(todo):
                el = time.time() - t0
                print("fetched %d/%d  %.2f/%.2f GB  %.1f MB/s  failed %d" % (
                    done[0], len(todo), done[1] / 1e9, total / 1e9, done[1] / 1e6 / max(el, 1), done[2]), flush=True)

    with concurrent.futures.ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(one, todo))
    print("fetch finished: %d files, %d failed" % (done[0], done[2]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
