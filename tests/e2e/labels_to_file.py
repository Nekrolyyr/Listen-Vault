"""Convert the traefik.* labels from `docker compose config` into a Traefik file-provider config.

Only what must differ outside Docker is changed: container hostnames -> 127.0.0.1, @docker -> @file,
the ACME resolver -> a local certificate. Everything else (rules, middlewares, headers) is the same.
"""
import json
import subprocess
import sys

import yaml

repo, out, cert, key = sys.argv[1:5]  # optional 5th argument: an --env-file for compose
env = ["--env-file", sys.argv[5]] if len(sys.argv) > 5 else []
cfg = json.loads(subprocess.run(["docker", "compose", *env, "config", "--format", "json"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout)

LIST_KEYS = {"authResponseHeaders", "middlewares", "entrypoints"}
http: dict = {}
ports: dict[str, int] = {}
for name, svc in cfg["services"].items():
    for label, value in (svc.get("labels") or {}).items():
        if not label.startswith("traefik.http."):
            continue
        parts = label.split(".")[2:]
        node = http
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        leaf = parts[-1]
        if leaf == "certresolver":
            continue
        if leaf == "port":
            ports[parts[1]] = int(value)
            continue
        if leaf in LIST_KEYS:
            value = [v.strip().replace("@docker", "@file") for v in value.split(",")]
        elif leaf == "address":
            value = value.replace("listen-authelia", "127.0.0.1")
        elif value in ("true", "false"):
            value = value == "true"
        elif value.isdigit():
            value = int(value)
        node[leaf] = value

for svc_name, port in ports.items():
    http["services"][svc_name] = {"loadBalancer": {"servers": [{"url": f"http://127.0.0.1:{port}"}]}}
for router in http["routers"].values():
    router["tls"] = {}
    router["entryPoints"] = router.pop("entrypoints")
for mw in http["middlewares"].values():
    if "forwardauth" in mw:
        mw["forwardAuth"] = mw.pop("forwardauth")
    if "headers" in mw and "customrequestheaders" in mw["headers"]:
        mw["headers"]["customRequestHeaders"] = mw["headers"].pop("customrequestheaders")

dynamic = {"http": http, "tls": {"certificates": [{"certFile": cert, "keyFile": key}]}}
with open(out, "w") as f:
    yaml.safe_dump(dynamic, f, sort_keys=False)
print(yaml.safe_dump(dynamic, sort_keys=False))
