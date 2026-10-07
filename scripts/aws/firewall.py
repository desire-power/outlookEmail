#!/usr/bin/env python3
"""Add only a CI runner /32, preserving existing rules. Used only by the deploy job."""
import argparse
import ipaddress
import json
import os
import subprocess
import sys


def aws(*args):
    result = subprocess.run(["aws", "lightsail", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Lightsail firewall request failed")
    return json.loads(result.stdout or "{}")


def already_allowed(states, cidr):
    runner = ipaddress.ip_network(cidr)
    for rule in states:
        if rule.get("state", "open") != "open":
            continue
        if rule.get("protocol") in ["tcp", "all"] and rule.get("fromPort", 0) <= 22 <= rule.get("toPort", 0):
            for existing in rule.get("cidrs", []):
                network = ipaddress.ip_network(existing)
                if network.version == 4 and runner.subnet_of(network):
                    return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["open", "close"])
    parser.add_argument("instance")
    parser.add_argument("cidr")
    args = parser.parse_args()
    network = ipaddress.ip_network(args.cidr, strict=True)
    if network.version != 4 or network.prefixlen != 32 or not network.network_address.is_global:
        raise ValueError("Expected the runner's global IPv4 /32")
    port = json.dumps({"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrs": [args.cidr]})
    if args.operation == "open":
        result = aws("get-instance-port-states", "--instance-name", args.instance)
        added = not already_allowed(result.get("portStates", []), args.cidr)
        if added:
            # Publish cleanup metadata before the request, so a network error after an accepted
            # request still leaves enough information for the always-run cleanup step.
            with open(os.environ["GITHUB_ENV"], "a") as stream:
                stream.write("RUNNER_SSH_RULE_ADDED=true\n")
            aws("open-instance-public-ports", "--instance-name", args.instance, "--port-info", port)
        print("Runner SSH access is ready")
    else:
        aws("close-instance-public-ports", "--instance-name", args.instance, "--port-info", port)
        print("Temporary runner SSH rule removed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Firewall operation failed; inspect the specific instance rule", file=sys.stderr)
        sys.exit(1)
