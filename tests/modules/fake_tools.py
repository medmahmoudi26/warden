"""Stand-ins for the modules' tools, driven by the names they are given.

Called as ``fake_tools.py <tool> <args...>`` by wrapper scripts named after the
tools. Every call is appended to ``$FAKE_STATE/calls.jsonl`` as ``[tool, args]``.
Names (domains, hosts or URLs) steer the behaviour:

* ``fail``  the tool exits 2 every time;
* ``flaky`` it exits 3 the first time, then works (``bruteagain``: only puredns bruteforce);
* ``slow``  it starts a ``sleep 300`` child, records both pids in ``$FAKE_STATE/pids``, and waits;
* ``dead``  names never resolve (puredns), hosts never answer (httpx);
* ``vuln``  nuclei reports a high finding with raw request and response.
"""

import json
import os
import subprocess
import sys

tool, args = sys.argv[1], sys.argv[2:]
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps([tool, args]) + "\n")


def arg(flag: str) -> str:
    return args[args.index(flag) + 1]


def lines(path: str) -> list[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def behave(names: list[str]) -> None:
    for n in names:
        if "slow" in n:
            child = subprocess.Popen(["sleep", "300"])
            with open(os.path.join(state, "pids"), "w") as f:
                f.write(f"{os.getpid()} {child.pid}\n")
            child.wait()
        if "fail" in n:
            sys.exit(2)
        if "flaky" in n:
            marker = os.path.join(state, f"flaky-{tool}-{n.replace('/', '_')}")
            if not os.path.exists(marker):
                open(marker, "w").close()
                sys.exit(3)


def write(path: str, names: list[str]) -> None:
    with open(path, "w") as f:
        f.write("".join(f"{n}\n" for n in names))


if tool == "subfinder":
    d = arg("-d")
    behave([d])
    pad = int(os.environ.get("FAKE_LINES", "0"))
    found = [f"sub.{d}", f"WWW.{d}", f"dead-old.{d}", "unrelated.example.org"]
    found += [f"host-{i:07d}-padding-padding-padding.{d}" for i in range(pad)]
    write(arg("-o"), found)
    print("\n".join(found[:4]), flush=True)
elif tool == "puredns" and args[0] == "resolve":
    write(arg("-w"), [n for n in lines(args[1]) if "dead" not in n])
elif tool == "puredns" and args[0] == "bruteforce":
    d = args[2]
    if "bruteagain" in d and not os.path.exists(os.path.join(state, f"flaky-brute-{d}")):
        open(os.path.join(state, f"flaky-brute-{d}"), "w").close()
        sys.exit(3)  # brute force fails once on *bruteagain* domains
    write(arg("-w"), [f"vpn.{d}", f"mail.{d}"])
elif tool == "gotator":
    names = lines(arg("-sub"))
    for _ in range(int(os.environ.get("FAKE_GOTATOR_REPEAT", "1"))):
        for n in names:
            print(f"dev-{n}\ndead-{n}", flush=True)
elif tool == "regulator":
    d = arg("-t")
    write(arg("-o"), [f"api-prod-2.{d}"])
elif tool == "httpx":
    targets = lines(arg("-l"))
    behave(targets)
    print("[INF] not json", flush=True)
    for t in targets:
        if "dead" in t:
            continue
        url = t if "://" in t else f"https://{t}"
        rec = {
            "url": url,
            "input": t,
            "status_code": 200,
            "content_length": 1234,
            "content_type": "text/html",
            "title": "Home",
            "webserver": "nginx",
            "tech": ["Nginx", "React"],
            "cname": [f"cdn.{t}"],
            "a": ["192.0.2.1"],
            "cdn": True,
            "cdn_name": "cloudflare",
            "failed": False,
        }
        print(json.dumps(rec), flush=True)
elif tool == "nuclei":
    targets = lines(arg("-l"))
    behave(targets)
    raw = "x" * int(os.environ.get("FAKE_RAW_BYTES", "100"))
    for t in targets:
        print(
            json.dumps(
                {
                    "template-id": "tech-detect",
                    "matcher-name": "nginx",
                    "type": "http",
                    "info": {"severity": "info"},
                    "host": t,
                    "matched-at": t,
                    "extracted-results": ["1.25"],
                }
            ),
            flush=True,
        )
        if "vuln" in t:
            print(
                json.dumps(
                    {
                        "template-id": "CVE-2021-41773",
                        "type": "http",
                        "info": {"severity": "high"},
                        "host": t,
                        "matched-at": f"{t}/cgi-bin/.%2e/etc/passwd",
                        "request": raw,
                        "response": raw,
                    }
                ),
                flush=True,
            )
elif tool == "ffuf":
    base = arg("-u").removesuffix("FUZZ")
    behave([base])
    for i, entry in enumerate(lines(arg("-w"))[:2]):
        print(
            json.dumps({"status": 200, "length": 92 + i, "url": f"{base}{entry}", "host": base}), flush=True
        )
else:
    sys.exit(f"fake_tools: unknown call {tool} {args}")
