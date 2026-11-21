"""Streaming driver: download one input file, de-identify it, upload it, forget it.

Nothing but small state files is kept on disk, so the 140 GB corpus can be processed
on a machine with little free space.  Work is spread over worker processes; a second,
smaller pool handles very large files so memory stays bounded.  The run is resumable:
finished paths are appended to ``done.jsonl`` and skipped next time.
"""
import hashlib
import http.client
import json
import multiprocessing
import os
import random
import signal
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool

from . import handlers, pseudo
from .engine import Ctx, Engine
from .identities import Dictionary, scope_of

GCS_HOST = "storage.googleapis.com"
BIG = 200 * 1024 * 1024
UA = "machina-deid-pipeline/0.1"

_W = {}


def read_manifest(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            while len(p) < 4:
                p.append("")
            rows.append((p[0], int(p[1] or 0), p[2], p[3] or ("corpus/" + urllib.parse.quote(p[0]))))
    return rows


def fetch_manifest(base, dest):
    u = urllib.parse.urlparse(base)
    conn = http.client.HTTPSConnection(u.netloc, timeout=120)
    conn.request("GET", "/manifest.tsv", headers={"User-Agent": UA})
    r = conn.getresponse()
    body = r.read()
    if r.status != 200:
        raise RuntimeError("manifest fetch failed: %s" % r.status)
    with open(dest, "wb") as f:
        f.write(body)


def build_dictionary(rows, state_dir):
    d = Dictionary()
    d.add_curated()
    paths = [r[0] for r in rows]
    d.add_manifest_paths(paths)
    d.add_web_domains(paths)
    disc = os.environ.get("DEID_DISCOVERED") or os.path.join(state_dir, "discovered.json")
    if os.path.exists(disc):
        with open(disc) as f:
            d.add_discovered(json.load(f))
    if os.environ.get("DEID_CODES", "1") != "0":
        d.add_codes(paths)
    d.mark_word_parts()
    return d


def load_key(state_dir):
    p = os.path.join(state_dir, "secret.key")
    if not os.path.exists(p):
        with open(p, "wb") as f:
            f.write(os.urandom(32))
        os.chmod(p, 0o600)
    with open(p, "rb") as f:
        return f.read()


# ------------------------------------------------------------------ workers
def _init(cfg):
    _W["cfg"] = cfg
    pseudo.set_key(bytes.fromhex(cfg["key"]))
    with open(cfg["dict"]) as f:
        _W["engine"] = Engine(Dictionary.from_json(json.load(f)))
    _W["in"] = None
    _W["out"] = None
    _W["token"] = ("", 0.0)


def _token():
    cfg = _W["cfg"]
    try:
        mt = os.path.getmtime(cfg["token_file"])
    except OSError:
        return _W["token"][0]
    if mt != _W["token"][1]:
        with open(cfg["token_file"]) as f:
            _W["token"] = (f.read().strip(), mt)
    return _W["token"][0]


def _download(urlpath, size, path=None):
    cfg = _W["cfg"]
    last = None
    cache = cfg.get("input_cache")
    if cache and path:
        lp = os.path.join(cache, path)
        try:
            if os.path.getsize(lp) == size:
                with open(lp, "rb") as f:
                    return f.read()
        except OSError:
            pass
    for attempt in range(8):
        try:
            if _W["in"] is None:
                _W["in"] = http.client.HTTPSConnection(cfg["in_host"], timeout=180)
            conn = _W["in"]
            conn.request("GET", "/" + urlpath.lstrip("/"), headers={"User-Agent": UA})
            r = conn.getresponse()
            body = r.read()
            if r.status == 200 and (len(body) == size or size == 0 and not body):
                return body
            if r.status == 200 and len(body) != size:
                last = "size %d != %d" % (len(body), size)
            else:
                last = "HTTP %d" % r.status
                if r.status == 404:
                    break
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        try:
            if _W["in"] is not None:
                _W["in"].close()
        except Exception:
            pass
        _W["in"] = None
        time.sleep(min(30, 1.5 ** attempt) + random.random())
    raise RuntimeError("download failed (%s)" % last)


def _expired(status, body):
    return status == 401 or (status in (400, 403) and b"xpired" in body)


def _upload(key, data):
    cfg = _W["cfg"]
    if cfg.get("dry"):
        outp = os.path.join(cfg["dry"], key)
        os.makedirs(os.path.dirname(outp), exist_ok=True)
        with open(outp, "wb") as f:
            f.write(data)
        return
    path = "/%s/%s" % (cfg["bucket"], "/".join(urllib.parse.quote(p, safe="") for p in key.split("/")))
    last = None
    attempt = 0
    waited = 0
    while attempt < 8:
        attempt += 1
        tok = _token()
        try:
            if _W["out"] is None:
                _W["out"] = http.client.HTTPSConnection(GCS_HOST, timeout=300)
            conn = _W["out"]
            conn.request("PUT", path, body=data, headers={
                "Authorization": "Bearer " + tok, "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data)), "User-Agent": UA})
            r = conn.getresponse()
            body = r.read()
            if 200 <= r.status < 300:
                return
            if _expired(r.status, body) or not tok:
                # wait (up to 3 h) for a fresh token in the token file
                attempt -= 1
                while waited < 3 * 3600:
                    time.sleep(5)
                    waited += 5
                    if _token() != tok:
                        break
                else:
                    raise RuntimeError("upload token expired and was not renewed")
                continue
            last = "HTTP %d %s" % (r.status, body[:160].decode("utf-8", "replace"))
            if r.status < 500 and r.status != 429 and r.status != 408:
                break
        except RuntimeError:
            raise
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        try:
            if _W["out"] is not None:
                _W["out"].close()
        except Exception:
            pass
        _W["out"] = None
        time.sleep(min(30, 1.5 ** attempt) + random.random())
    raise RuntimeError("upload failed (%s)" % last)


def _copy(src_key, dst_key, holder=None):
    """Server-side copy inside the bucket (no bytes travel).  Returns True on success."""
    cfg = _W["cfg"]
    if holder is not None:
        return _copy_with(holder, cfg, src_key, dst_key)
    return _copy_with(_W, cfg, src_key, dst_key)


def _copy_with(_W, cfg, src_key, dst_key):
    enc = lambda k: "/".join(urllib.parse.quote(p, safe="") for p in k.split("/"))
    for attempt in range(4):
        tok = _token()
        try:
            if _W["out"] is None:
                _W["out"] = http.client.HTTPSConnection(GCS_HOST, timeout=300)
            conn = _W["out"]
            conn.request("PUT", "/%s/%s" % (cfg["bucket"], enc(dst_key)), body=b"", headers={
                "Authorization": "Bearer " + tok, "Content-Length": "0", "User-Agent": UA,
                "x-goog-copy-source": "/%s/%s" % (cfg["bucket"], enc(src_key))})
            r = conn.getresponse()
            body = r.read()
            if 200 <= r.status < 300:
                return True
            if _expired(r.status, body):
                for _ in range(2160):
                    time.sleep(5)
                    if _token() != tok:
                        break
                continue
            if r.status < 500 and r.status != 429:
                return False
        except Exception:
            pass
        try:
            if _W["out"] is not None:
                _W["out"].close()
        except Exception:
            pass
        _W["out"] = None
        time.sleep(1.5 ** attempt)
    return False


def _copy_phase(rows, cfg, record, stop_file, nthreads=48):
    """Copy the unchanged files of an incremental attempt with many light threads (the
    requests carry no data).  Returns the rows that could not be copied."""
    import queue
    _W["cfg"] = cfg
    _W.setdefault("token", ("", 0.0))
    q = queue.Queue()
    for r in rows:
        q.put(r)
    failed = []
    flock = threading.Lock()

    def work():
        holder = {"out": None}
        while True:
            try:
                r = q.get_nowait()
            except queue.Empty:
                return
            prev = r[4]
            t0 = time.time()
            ok = False
            try:
                if not os.path.exists(stop_file):
                    ok = _copy_with(holder, cfg, cfg["prev_prefix"] + "corpus/" + prev["out_path"],
                                    cfg["prefix"] + "corpus/" + prev["out_path"])
            except Exception:
                ok = False
            if ok:
                record({"path": r[0], "in": r[1], "ok": True, "copied": True, "out_path": prev["out_path"],
                        "out": prev.get("out", 0), "md5": prev.get("md5"), "changed": prev.get("changed"), "hits": 0,
                        "notes": prev.get("notes", []), "findings": prev.get("findings", []),
                        "t": [0, 0, round(time.time() - t0, 2)]})
            else:
                with flock:
                    failed.append(r)

    ts = [threading.Thread(target=work, daemon=True) for _ in range(nthreads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return failed


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def _task(row):
    path, size, md5, urlpath = row[:4]
    safe = len(row) > 4 and row[4] is True
    prev = row[4] if len(row) > 4 and isinstance(row[4], dict) else None
    t0 = time.time()
    rec = {"path": path, "in": size}
    if prev and not _W["cfg"].get("dry"):
        cfg = _W["cfg"]
        try:
            if _copy(cfg["prev_prefix"] + "corpus/" + prev["out_path"], cfg["prefix"] + "corpus/" + prev["out_path"]):
                rec.update(ok=True, copied=True, out_path=prev["out_path"], out=prev.get("out", 0), md5=prev.get("md5"),
                           changed=prev.get("changed"), hits=0, notes=prev.get("notes", []), findings=prev.get("findings", []),
                           t=[0, 0, round(time.time() - t0, 2)])
                return rec
        except Exception:
            pass
    try:
        engine = _W["engine"]
        data = _download(urlpath, size, path) if size else b""
        t1 = time.time()
        ctx = Ctx(path, scope_of(path))
        try:
            if hasattr(signal, "SIGALRM"):
                signal.signal(signal.SIGALRM, _alarm)
                signal.alarm(600 + size // 200000)
            if safe:
                ctx.notes.append("format handler crashed; generic pass used")
                out = engine.scrub(data, ctx, not handlers.is_text(data), "")
            else:
                out = handlers.process(path, data, engine, ctx)
        except _Timeout:
            out = data
            ctx.notes.append("processing timed out; original bytes kept")
        except Exception as exc:
            out = data
            ctx.notes.append("processing failed (%s); original bytes kept" % type(exc).__name__)
        finally:
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)
        new_path = handlers.rename_path(engine, ctx, path)
        # self-check: distinctive names found in this file that are still readable in the
        # raw output (plain or UTF-16).  Logged privately to steer later improvements.
        residual = []
        if len(out) < 64 * 1024 * 1024 and ctx.local_wide:
            low = out.lower()
            for name in sorted(ctx.local_wide)[:40]:
                nb = name.encode("latin-1", "replace")
                if nb in low or b"\x00".join(bytes([c]) for c in nb) in low:
                    residual.append(name)
        t2 = time.time()
        _upload(_W["cfg"]["prefix"] + "corpus/" + new_path, out)
        rec.update(ok=True, out_path=new_path, out=len(out), md5=hashlib.md5(out).hexdigest(),
                   changed=out != data, hits=ctx.hits, notes=ctx.notes[:8], residual=residual[:10],
                   findings=sorted(set(ctx.findings))[:40],
                   t=[round(t1 - t0, 2), round(t2 - t1, 2), round(time.time() - t2, 2)])
    except Exception as exc:
        rec.update(ok=False, error="%s: %s" % (type(exc).__name__, str(exc)[:300]), tb=traceback.format_exc()[-600:])
    return rec


# ------------------------------------------------------------------ driver
def run(args):
    state = args.state
    os.makedirs(state, exist_ok=True)
    adir = os.path.join(state, "attempt-%s" % args.attempt)
    os.makedirs(adir, exist_ok=True)
    man = os.path.join(state, "manifest.tsv")
    if not os.path.exists(man):
        print("fetching manifest ...", flush=True)
        fetch_manifest(args.base, man)
    rows = read_manifest(man)
    if args.prefix_filter:
        rows = [r for r in rows if r[0].startswith(args.prefix_filter)]
    if getattr(args, "only_list", ""):
        with open(args.only_list, encoding="utf-8") as f:
            want = set(l.rstrip("\n") for l in f if l.strip())
        rows = [r for r in rows if r[0] in want]
    key = load_key(state)
    pseudo.set_key(key)
    d = build_dictionary(read_manifest(man), state)
    dict_path = os.path.join(adir, "dictionary.json")
    d.save(dict_path)
    with open(os.path.join(adir, "mapping.private.json"), "w") as f:
        json.dump(d.mapping_table(), f, indent=0)
    print("dictionary: %d strong atoms, %d phrases, %d scoped" % (
        len(d.strong), len(d.phrases), sum(len(v) for v in d.scoped.values())), flush=True)

    done_path = os.path.join(adir, "done.jsonl")
    done = set()
    if os.path.exists(done_path):
        with open(done_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("ok"):
                    done.add(rec["path"])
    todo = [r for r in rows if r[0] not in done]
    # incremental attempt: files whose handler did not change are copied inside the
    # bucket from the previous attempt instead of being downloaded and processed again
    prev_prefix = ""
    if getattr(args, "reuse_from", "") and not args.dry:
        only = set(x.strip().lower() for x in (args.only_ext or "").split(",") if x.strip())
        pdir = os.path.join(state, "attempt-%s" % args.reuse_from, "done.jsonl")
        prevrec = {}
        if os.path.exists(pdir):
            with open(pdir) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("ok") and rec.get("out_path"):
                        prevrec[rec["path"]] = rec
        ncopy = 0
        new_todo = []
        for r in todo:
            pr = prevrec.get(r[0])
            if pr is not None and handlers.ext_of(r[0]) not in only and not (args.only_prefix and r[0].startswith(args.only_prefix)):
                slim = {k: pr.get(k) for k in ("out_path", "out", "md5", "changed", "notes", "findings")}
                new_todo.append(r + (slim,))
                ncopy += 1
            else:
                new_todo.append(r)
        todo = new_todo
        print("incremental: %d files copied from attempt-%s, %d re-processed" % (ncopy, args.reuse_from, len(todo) - ncopy), flush=True)
    if args.sample:
        step = max(1, len(todo) // args.sample)
        todo = todo[::step][:args.sample]
    elif args.limit:
        todo = todo[:args.limit]
    total_bytes = sum(r[1] for r in todo if not (len(r) > 4 and isinstance(r[4], dict)))
    print("%d files to do (%.1f GB), %d already done" % (len(todo), total_bytes / 1e9, len(done)), flush=True)
    if not todo:
        return 0

    bucket, prefix = "", ""
    if not args.dry:
        dest = args.dest
        if not dest.startswith("gs://"):
            raise SystemExit("--dest must be gs://bucket/teams/<team>/attempt-N/")
        bucket, _, prefix = dest[5:].partition("/")
        if not prefix.endswith("/"):
            prefix += "/"
    if getattr(args, "reuse_from", "") and prefix:
        prev_prefix = prefix.rstrip("/").rsplit("/", 1)[0] + "/attempt-%s/" % args.reuse_from
    cfg = {"key": key.hex(), "dict": dict_path, "token_file": args.token_file,
           "in_host": urllib.parse.urlparse(args.base).netloc, "bucket": bucket, "prefix": prefix,
           "prev_prefix": prev_prefix, "dry": args.dry, "input_cache": getattr(args, "input_cache", "")}
    is_copy = lambda r: len(r) > 4 and isinstance(r[4], dict)
    copies = [r for r in todo if is_copy(r)] if not args.dry else []
    small = [r for r in todo if r[1] < BIG and not (copies and is_copy(r))]
    big = [r for r in todo if r[1] >= BIG and not (copies and is_copy(r))]
    if args.dry:
        small = [r for r in todo if r[1] < BIG or is_copy(r)]
        big = [r for r in todo if r[1] >= BIG and not is_copy(r)]
    # mix sizes so progress is steady and memory use is even
    random.Random(7).shuffle(small)
    ctxm = multiprocessing.get_context("spawn")
    stats = {"n": 0, "bytes": 0, "out": 0, "err": 0, "changed": 0, "hits": 0, "t0": time.time()}
    lock = threading.Lock()
    donef = open(done_path, "a")

    def record(rec):
        with lock:
            donef.write(json.dumps(rec) + "\n")
            stats["n"] += 1
            stats["bytes"] += 0 if rec.get("copied") else rec.get("in", 0)
            stats["copied"] = stats.get("copied", 0) + (1 if rec.get("copied") else 0)
            if rec.get("ok"):
                stats["out"] += rec.get("out", 0)
                stats["changed"] += 1 if rec.get("changed") else 0
                stats["hits"] += rec.get("hits", 0)
            else:
                stats["err"] += 1
                print("ERROR %s: %s" % (rec["path"], rec.get("error")), flush=True)
            if stats["n"] % 200 == 0:
                donef.flush()

    stop_file = os.path.join(args.state, "stop-%s" % args.attempt)

    def consume(items, nworkers):
        """Run tasks; survive a worker process dying (re-run its files one at a time,
        and fall back to the generic pass for a file that kills a worker twice)."""
        it = iter(items)
        suspects = []
        exhausted = False
        while not exhausted:
            ex = ProcessPoolExecutor(nworkers, mp_context=ctxm, initializer=_init, initargs=(cfg,))
            inflight = {}
            broken = False
            try:
                while True:
                    while len(inflight) < nworkers * 3 and not exhausted:
                        if os.path.exists(stop_file):      # state/stop-N: finish what is running, then end
                            exhausted = True
                            break
                        row = next(it, None)
                        if row is None:
                            exhausted = True
                            break
                        inflight[ex.submit(_task, row)] = row
                    if not inflight:
                        break
                    finished, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                    for fut in finished:
                        row = inflight.pop(fut)
                        try:
                            record(fut.result())
                        except BrokenProcessPool:
                            broken = True
                            suspects.append(row)
                        except Exception as exc:
                            record({"path": row[0], "in": row[1], "ok": False, "error": repr(exc)[:200]})
                    if broken:
                        suspects.extend(inflight.values())
                        inflight.clear()
                        break
            finally:
                ex.shutdown(wait=not broken)
            if broken:
                print("worker died; %d files will be re-run one at a time" % len(suspects), flush=True)
                exhausted = False if not exhausted else True
        for row in suspects:
            for safe in (False, True):
                ex = ProcessPoolExecutor(1, mp_context=ctxm, initializer=_init, initargs=(cfg,))
                try:
                    record(ex.submit(_task, row[:4] + (safe,)).result())
                    break
                except BrokenProcessPool:
                    if safe:
                        record({"path": row[0], "in": row[1], "ok": False, "error": "worker crashed twice"})
                except Exception as exc:
                    record({"path": row[0], "in": row[1], "ok": False, "error": repr(exc)[:200]})
                    break
                finally:
                    ex.shutdown(wait=False)

    def reporter():
        while not stop.is_set():
            stop.wait(30)
            with lock:
                el = max(1.0, time.time() - stats["t0"])
                rate = stats["bytes"] / el
                eta = (total_bytes - stats["bytes"]) / rate if rate > 0 else 0
                msg = {"done": stats["n"], "total": len(todo), "gb": round(stats["bytes"] / 1e9, 2),
                       "gb_total": round(total_bytes / 1e9, 2), "mb_s": round(rate / 1e6, 1),
                       "eta_min": round(eta / 60), "errors": stats["err"], "changed": stats["changed"],
                       "hits": stats["hits"], "copied": stats.get("copied", 0), "time": time.strftime("%H:%M:%S")}
                donef.flush()
            print("progress " + json.dumps(msg), flush=True)
            try:
                with open(os.path.join(adir, "progress.json"), "w") as f:
                    json.dump(msg, f)
            except Exception:
                pass

    stop = threading.Event()
    rep = threading.Thread(target=reporter, daemon=True)
    rep.start()
    threads = []
    leftover = []

    def copy_all():
        try:
            leftover.extend(_copy_phase(copies, cfg, record, stop_file))
        except Exception as exc:
            print("copy phase stopped (%r)" % (exc,), flush=True)
            leftover.extend(copies)

    if copies:
        threads.append(threading.Thread(target=copy_all))
    if small:
        threads.append(threading.Thread(target=consume, args=(small, args.workers)))
    if big:
        threads.append(threading.Thread(target=consume, args=(big, args.big_workers)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if leftover and not os.path.exists(stop_file):
        # files that could not be copied are produced again (the worker retries the copy first)
        print("%d files could not be copied; producing them again" % len(leftover), flush=True)
        consume([r for r in leftover if r[1] < BIG], args.workers)
        rest = [r for r in leftover if r[1] >= BIG]
        if rest:
            consume(rest, args.big_workers)
    stop.set()
    donef.close()
    el = time.time() - stats["t0"]
    print("finished: %d files, %.1f GB in %.0f min, %d errors, %d files changed, %d replacements" % (
        stats["n"], stats["bytes"] / 1e9, el / 60, stats["err"], stats["changed"], stats["hits"]), flush=True)
    return 1 if stats["err"] else 0


def upload_tree(args, local_dir, sub):
    """Upload the pipeline source (or any folder) under <dest>/<sub>/."""
    bucket, _, prefix = args.dest[5:].partition("/")
    if not prefix.endswith("/"):
        prefix += "/"
    _W["cfg"] = {"bucket": bucket, "prefix": prefix, "token_file": args.token_file, "dry": args.dry}
    _W["out"] = None
    _W["token"] = ("", 0.0)
    n = 0
    for root, dirs, files in os.walk(local_dir):
        dirs[:] = sorted(x for x in dirs if x not in ("__pycache__", ".git", ".venv", "state", "logs"))
        for fn in sorted(files):
            if fn.endswith((".pyc", ".DS_Store")):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, local_dir).replace(os.sep, "/")
            with open(full, "rb") as f:
                _upload(prefix + (sub + "/" if sub else "") + rel, f.read())
            n += 1
    return n


def upload_file(args, local, key):
    bucket, _, prefix = args.dest[5:].partition("/")
    if not prefix.endswith("/"):
        prefix += "/"
    _W["cfg"] = {"bucket": bucket, "prefix": prefix, "token_file": args.token_file, "dry": args.dry}
    _W["out"] = None
    _W["token"] = ("", 0.0)
    with open(local, "rb") as f:
        _upload(prefix + key, f.read())
