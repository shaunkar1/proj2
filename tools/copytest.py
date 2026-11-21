#!/usr/bin/env python3
"""Check whether the upload token may copy objects inside the team folder (used for
incremental attempts) and list a few objects.   python3 tools/copytest.py state"""
import http.client
import json
import sys
import urllib.parse

state = sys.argv[1]
dest = open(state + "/dest.txt").read().strip().rstrip("/")
tok = open(state + "/token.txt").read().strip()
bucket, prefix = dest[5:].split("/", 1)
enc = lambda k: "/".join(urllib.parse.quote(p, safe="") for p in k.split("/"))
c = http.client.HTTPSConnection("storage.googleapis.com", timeout=120)
h = {"Authorization": "Bearer " + tok}
c.request("GET", "/storage/v1/b/%s/o?prefix=%s&maxResults=5" % (bucket, urllib.parse.quote(prefix + "/attempt-1/", safe="")), headers=h)
r = c.getresponse(); body = r.read()
print("list:", r.status, [i["name"][-60:] for i in json.loads(body).get("items", [])] if r.status == 200 else body[:200])
src = prefix + "/attempt-1/README.txt"
c.request("GET", "/%s/%s" % (bucket, enc(src)), headers=h)
r = c.getresponse(); body = r.read()
print("read:", r.status, len(body))
dst = prefix + "/scratch/copytest.txt"
hh = dict(h); hh.update({"Content-Length": "0", "x-goog-copy-source": "/%s/%s" % (bucket, enc(src))})
c.request("PUT", "/%s/%s" % (bucket, enc(dst)), body=b"", headers=hh)
r = c.getresponse(); body = r.read()
print("copy:", r.status, body[:300])
