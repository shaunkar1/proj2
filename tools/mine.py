#!/usr/bin/env python3
"""Development aid: find names that recur in one collection's PDF text and nowhere else
(title-block company names, authors, project names).  Reads the output of pdfscan.py.

    python3 tools/mine.py scratch/scan1 state [--min-df 2] > scratch/mined.json"""
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from deid.identities import STOP, SOFTWARE, is_first_name, is_word, scope_of  # noqa: E402
from deid.engine import PROSE_STOP  # noqa: E402

WORD = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ0-9]{2,}")
CAPSEQ = re.compile(r"(?<![A-Za-z])((?:[A-Z][A-Za-zÀ-ɏ]+|[A-Z]{2,})(?:[ \t]+(?:[A-Z][A-Za-zÀ-ɏ]+|[A-Z]{2,}|&|of|de)){1,3})(?![A-Za-z])")


def main():
    scan, state = sys.argv[1], sys.argv[2]
    min_df = 2
    recs = {}
    for line in open(os.path.join(scan, "scan.jsonl")):
        r = json.loads(line)
        recs[r["i"]] = r
    uni = collections.defaultdict(collections.Counter)      # scope -> token -> doc freq
    big = collections.defaultdict(collections.Counter)
    ndocs = collections.Counter()
    for i, r in recs.items():
        p = os.path.join(scan, "text", "%05d.txt" % i)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            f.readline()
            text = f.read()
        sc = scope_of(r["path"])
        ndocs[sc] += 1
        toks = set(t.lower() for t in WORD.findall(text))
        for t in toks:
            uni[sc][t] += 1
        phr = set()
        for m in CAPSEQ.finditer(text):
            parts = m.group(1).split()
            for n in (2, 3):
                for k in range(len(parts) - n + 1):
                    phr.add(" ".join(parts[k:k + n]).lower())
        for t in phr:
            big[sc][t] += 1
    spread_u = collections.Counter()
    spread_b = collections.Counter()
    for sc in uni:
        for t in uni[sc]:
            spread_u[t] += 1
        for t in big[sc]:
            spread_b[t] += 1
    out = {}
    for sc in sorted(uni, key=lambda s: -ndocs[s]):
        n = ndocs[sc]
        need = max(min_df, int(0.2 * n))
        us = []
        for t, df in uni[sc].most_common():
            if df < need:
                break
            if spread_u[t] > 2 or t in STOP or t in SOFTWARE or t in PROSE_STOP or is_word(t) or len(t) < 4:
                continue
            us.append((t, df))
        bs = []
        for t, df in big[sc].most_common():
            if df < need:
                break
            parts = t.split()
            if spread_b[t] > 2 or all(x in PROSE_STOP or x in STOP for x in parts):
                continue
            bs.append((t, df))
        if us or bs:
            out[sc] = {"n": n, "atoms": us[:40], "phrases": bs[:40]}
    json.dump(out, sys.stdout, indent=0)


if __name__ == "__main__":
    main()
