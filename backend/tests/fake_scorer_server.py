"""A stand-in for scorer/serve.py used by test_scorer_client.py: same line protocol, scripted behaviour."""
import json
import os
import sys
import time


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    rid, op = msg["id"], msg["op"]
    if op == "shutdown":
        send({"id": rid, "ok": True})
        sys.exit(0)
    elif op == "echo":
        send({"id": rid, "ok": True, "echo": msg.get("payload"), "pid": os.getpid()})
    elif op == "slow":
        time.sleep(msg["seconds"])
        send({"id": rid, "ok": True, "pid": os.getpid(), "tag": msg.get("tag")})
    elif op == "events":
        for i in range(1, 4):
            send({"id": rid, "event": "progress", "stage": "pages", "done": i, "total": 3})
        send({"id": rid, "ok": True})
    elif op == "crash":
        sys.stderr.write("Traceback...\nMemoryError: out of memory\n")
        sys.stderr.flush()
        sys.exit(3)
    elif op == "fatal":
        send({"id": rid, "ok": False, "fatal": "KeyError: bad listing"})
    elif op == "noise":
        sys.stdout.write("some library chatter\n")
        sys.stdout.write("{not json\n")
        sys.stdout.flush()
        send({"id": rid, "ok": True})
    elif op == "stderr_flood":
        sys.stderr.write("x" * 2_000_000 + "\n")
        sys.stderr.flush()
        send({"id": rid, "ok": True})
    elif op == "stale":
        send({"id": rid - 1, "ok": True, "stale": True})
        send({"id": rid, "ok": True})
