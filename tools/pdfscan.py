#!/usr/bin/env python3
"""Development check: run the PDF handler over locally cached PDFs and record, per file,
what was found, what changed in the extracted text and what was left behind.

    python3 tools/pdfscan.py --state state --input input --out scratch/scan1 [--limit N] [--jobs N]

Nothing is uploaded; the records stay on this machine."""
import argparse
import io
import json
import multiprocessing
import os
import re
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TOKEN = re.compile(r"[A-Za-z0-9À-ɏ][A-Za-z0-9À-ɏ._@+-]*")
_G = {}


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def _text(data, limit=40):
    try:
        import pypdf
    except Exception:
        return None
    try:
        rd = pypdf.PdfReader(io.BytesIO(data), strict=False)
        out = []
        for i, page in enumerate(rd.pages):
            if i >= limit:
                break
            out.append(page.extract_text() or "")
        return "\n".join(out)
    except _Timeout:
        raise
    except Exception:
        return None


def _init(state):
    from deid import pseudo, runner
    from deid.engine import Engine
    pseudo.set_key(runner.load_key(state))
    rows = runner.read_manifest(os.path.join(state, "manifest.tsv"))
    _G["engine"] = Engine(runner.build_dictionary(rows, state))


def _one(job):
    idx, root, path, outdir, want_text, save = job
    from deid import handlers
    from deid.engine import Ctx
    from deid.identities import STOP, is_word, scope_of
    rec = {"i": idx, "path": path}
    t0 = time.time()
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(240)
        with open(os.path.join(root, path), "rb") as f:
            data = f.read()
        rec["in"] = len(data)
        ctx = Ctx(path, scope_of(path))
        out = handlers.process(path, data, _G["engine"], ctx)
        rec.update(out=len(out), changed=out != data, hits=ctx.hits, notes=ctx.notes[:6],
                   findings=ctx.findings[:80], secs=round(time.time() - t0, 2),
                   out_path=handlers.rename_path(_G["engine"], ctx, path))
        if save:
            for sub, blob in (("orig", data), ("out", out)):
                with open(os.path.join(outdir, sub, "%05d.pdf" % idx), "wb") as f:
                    f.write(blob)
        if want_text and len(data) < 25e6:
            a = _text(data)
            b = _text(out) if out != data else a
            if a is not None and b is not None:
                rec["ntext"] = len(a)
                ta, tb = TOKEN.findall(a), TOKEN.findall(b)
                sa, sb = set(ta), set(tb)
                rec["removed"] = [t for t in dict.fromkeys(ta) if t not in sb][:60]
                rec["added"] = [t for t in dict.fromkeys(tb) if t not in sa][:60]
                rec["ntok"] = [len(ta), len(tb)]
                low = b.lower()
                resid = []
                for kind, value in ctx.findings:
                    for atom in re.findall(r"[A-Za-z0-9]+", value):
                        al = atom.lower()
                        if len(al) >= 4 and al not in STOP and not is_word(al) and re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(al), low):
                            resid.append(al)
                rec["resid"] = sorted(set(resid))[:30]
                with open(os.path.join(outdir, "text", "%05d.txt" % idx), "w", encoding="utf-8") as f:
                    f.write(path + "\n" + a[:200000])
            else:
                rec["ntext"] = -1
        rec["secs_all"] = round(time.time() - t0, 2)
    except _Timeout:
        rec["err"] = "timeout"
    except Exception as exc:
        rec["err"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        signal.alarm(0)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--ext", default=".pdf")
    ap.add_argument("--no-text", action="store_true")
    ap.add_argument("--save-every", type=int, default=0, help="keep the original and the output of every Nth file")
    a = ap.parse_args()
    exts = tuple(a.ext.lower().split(","))
    files = []
    for r, ds, fs in os.walk(a.input):
        for f in fs:
            if f.lower().endswith(exts):
                files.append(os.path.relpath(os.path.join(r, f), a.input))
    files.sort()
    if a.limit and len(files) > a.limit:
        step = len(files) / float(a.limit)
        files = [files[int(i * step)] for i in range(a.limit)]
    os.makedirs(os.path.join(a.out, "text"), exist_ok=True)
    for sub in ("orig", "out"):
        os.makedirs(os.path.join(a.out, sub), exist_ok=True)
    jobs = [(i, a.input, p, a.out, not a.no_text, bool(a.save_every) and i % a.save_every == 0 and os.path.getsize(os.path.join(a.input, p)) < 3e6)
            for i, p in enumerate(files)]
    t0 = time.time()
    n = 0
    with multiprocessing.get_context("spawn").Pool(a.jobs, initializer=_init, initargs=(a.state,), maxtasksperchild=200) as pool, \
            open(os.path.join(a.out, "scan.jsonl"), "w") as out:
        for rec in pool.imap_unordered(_one, jobs, chunksize=4):
            out.write(json.dumps(rec) + "\n")
            n += 1
            if n % 250 == 0 or n == len(jobs):
                out.flush()
                print("scanned %d/%d in %.0fs" % (n, len(jobs), time.time() - t0), flush=True)
    print("scan finished", flush=True)


if __name__ == "__main__":
    main()
