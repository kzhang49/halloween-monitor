#!/usr/bin/env python3
"""Cloudflare Worker 管理小工具：读 .cf.env，执行 schedule 设置 / 状态查询"""
import json, os, sys, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
env = {}
for line in open(os.path.join(BASE, ".cf.env")):
    line = line.strip()
    if line and "=" in line:
        k, v = line.split("=", 1)
        env[k] = v

def api(method, path, data=None):
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/accounts/%s%s" % (env["CF_ACCOUNT"], path),
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + env["CF_TOKEN"], "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "set-schedule":
        crons = [{"cron": c} for c in sys.argv[2].split(",")]
        d = api("PUT", "/workers/scripts/halloween-monitor/schedules", crons)
        print("success:", d.get("success"), [s["cron"] for s in d.get("result", {}).get("schedules", [])] or d.get("errors"))
    elif cmd == "get-schedule":
        d = api("GET", "/workers/scripts/halloween-monitor/schedules")
        print(json.dumps(d.get("result"), ensure_ascii=False))
    elif cmd == "get-script":
        d = api("GET", "/workers/scripts/halloween-monitor")
        print("script ok:", d.get("success"), d["result"].get("id") if d.get("success") else d.get("errors"))
