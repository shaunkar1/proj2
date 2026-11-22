"""Command line entry point.

  python -m deid.cli run --attempt 1 --dest gs://<bucket>/teams/<team>/attempt-1/ \
        --token-file state/token.txt --state state [--workers 10]
  python -m deid.cli finish --attempt 1 --dest ... --token-file ... --code-dir . --state state
  python -m deid.cli local --input ./input --output ./cleaned-corpus --state state
"""
import argparse
import json
import os
import sys

from . import handlers, pseudo, report, runner
from .engine import Ctx, Engine
from .identities import scope_of


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deid")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--state", default="state", help="private working folder (key, mapping, logs)")
        p.add_argument("--attempt", default="1")
        p.add_argument("--dest", default="", help="gs://bucket/teams/<team>/attempt-N/")
        p.add_argument("--token-file", default="state/token.txt")
        p.add_argument("--dry", default="", help="write outputs under this local folder instead of uploading")

    r = sub.add_parser("run", help="stream: download, de-identify, upload")
    common(r)
    r.add_argument("--base", default="https://machina-input.joinbuilders.org")
    r.add_argument("--workers", type=int, default=40)
    r.add_argument("--big-workers", type=int, default=2)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--sample", type=int, default=0, help="process N files spread over the manifest")
    r.add_argument("--prefix-filter", default="")
    r.add_argument("--only-list", default="", help="process only the manifest paths listed in this file")
    r.add_argument("--input-cache", default="", help="folder holding already-downloaded input files (used when present)")
    r.add_argument("--reuse-from", default="", help="previous attempt number: copy unchanged outputs inside the bucket")
    r.add_argument("--only-ext", default="", help="with --reuse-from: extensions to re-process, e.g. .pdf,.zip")
    r.add_argument("--only-prefix", default="", help="with --reuse-from: also re-process paths with this prefix")
    r.add_argument("--names-from", default="", help="attempt number whose output names are kept for the files produced now")
    r.add_argument("--copy-rename-ext", default="", help="with --reuse-from: copied files with these extensions get the --names-keep / --names-mode name instead of the earlier one")
    r.add_argument("--big-from", default="", help="attempt whose output is copied for documents larger than --big-mb (instead of producing them again)")
    r.add_argument("--big-mb", type=float, default=0, help="size limit in MB for --big-from")
    r.add_argument("--names-mode", default="", help="with --names-keep: 'ident' also keeps earlier replacements of words taken from the source folder's own name")
    r.add_argument("--names-keep", type=int, default=0, help="with --names-from: keep only this many leading folders of the earlier name; the rest of the path stays original")

    f = sub.add_parser("finish", help="write README.txt, upload code/ and README.txt")
    common(f)
    f.add_argument("--code-dir", default=".")

    l = sub.add_parser("local", help="process an already-downloaded ./input tree into ./cleaned-corpus")
    common(l)
    l.add_argument("--input", required=True)
    l.add_argument("--output", required=True)

    pr = sub.add_parser("promote", help="promote names found in identity fields to corpus-wide entries")
    common(pr)

    args = ap.parse_args(argv)
    if args.cmd == "run":
        return runner.run(args)
    if args.cmd == "finish":
        adir = os.path.join(args.state, "attempt-%s" % args.attempt)
        readme = os.path.join(adir, "README.txt")
        report.write_readme(adir, readme)
        n = runner.upload_tree(args, args.code_dir, "code")
        runner.upload_file(args, readme, "README.txt")
        print("uploaded %d code files and README.txt" % n)
        return 0
    if args.cmd == "promote":
        from . import pseudo
        pseudo.set_key(runner.load_key(args.state))
        n = report.promote(args.state)
        print("promoted %d names" % n)
        return 0
    if args.cmd == "local":
        return local(args)
    ap.print_help()
    return 2


def local(args):
    """Offline mode for a machine that holds the whole input on disk."""
    os.makedirs(args.state, exist_ok=True)
    key = runner.load_key(args.state)
    pseudo.set_key(key)
    rows = []
    for root, dirs, files in os.walk(args.input):
        dirs.sort()
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rows.append((os.path.relpath(full, args.input).replace(os.sep, "/"), os.path.getsize(full), "", ""))
    engine = Engine(runner.build_dictionary(rows, args.state))
    for path, size, _, _ in rows:
        with open(os.path.join(args.input, path), "rb") as fh:
            data = fh.read()
        ctx = Ctx(path, scope_of(path))
        out = handlers.process(path, data, engine, ctx)
        dst = os.path.join(args.output, handlers.rename_path(engine, ctx, path))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(out)
    print("processed %d files" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
