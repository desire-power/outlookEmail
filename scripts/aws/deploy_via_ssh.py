#!/usr/bin/env python3
"""CI transport. Private key, password and ECR token never enter command arguments."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


class TransportError(RuntimeError):
    pass


def required_secrets():
    values = {key: os.environ.get(key, "") for key in [
        "LIGHTSAIL_SSH_PRIVATE_KEY", "LIGHTSAIL_SSH_KNOWN_HOSTS", "LOGIN_PASSWORD"]}
    if any(not value.strip() for value in values.values()):
        raise TransportError("Configure LIGHTSAIL_SSH_PRIVATE_KEY, LIGHTSAIL_SSH_KNOWN_HOSTS and LOGIN_PASSWORD")
    if "PRIVATE KEY-----" not in values["LIGHTSAIL_SSH_PRIVATE_KEY"]:
        raise TransportError("The SSH credential must be a private key, not the .pub file")
    password = values["LOGIN_PASSWORD"]
    if len(password) < 12 or any(c in password for c in "\r\n\0"):
        raise TransportError("LOGIN_PASSWORD must be at least 12 characters without line breaks")
    return values


def execute(args, *, input_text=None, environment=None, capture=True):
    result = subprocess.run(args, input=input_text, env=environment, text=True,
                            capture_output=capture)
    if result.returncode:
        raise TransportError("Deployment transport operation failed")
    return result.stdout if capture else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-secrets", action="store_true")
    parser.add_argument("--host", default="52.199.82.242")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--image")
    parser.add_argument("--commit")
    args = parser.parse_args()
    secrets = required_secrets()
    if args.check_secrets:
        print("Deployment secrets are configured")
        return
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", args.host) or not re.fullmatch(r"[a-z][a-z0-9_-]*", args.user):
        raise TransportError("Invalid SSH destination")
    if not args.image or not re.fullmatch(r"[a-f0-9]{40}", args.commit or ""):
        raise TransportError("Missing image digest or commit SHA")
    token = execute(["aws", "ecr", "get-login-password"])
    token = token.strip()
    if not token:
        raise TransportError("ECR credential is unavailable")
    print("::add-mask::" + token, flush=True)
    with tempfile.TemporaryDirectory(prefix="outlookemail-ssh-") as temporary:
        temporary = Path(temporary)
        key = temporary / "key"
        hosts = temporary / "known_hosts"
        key.write_text(secrets["LIGHTSAIL_SSH_PRIVATE_KEY"].rstrip() + "\n")
        hosts.write_text(secrets["LIGHTSAIL_SSH_KNOWN_HOSTS"].rstrip() + "\n")
        key.chmod(0o600)
        hosts.chmod(0o600)
        environment = os.environ.copy()
        agent_started = False
        try:
            # Optional encrypted SSH key support; no passphrase is written into this helper.
            if environment.get("LIGHTSAIL_SSH_KEY_PASSPHRASE"):
                agent = execute(["ssh-agent", "-s"])
                for name in ["SSH_AUTH_SOCK", "SSH_AGENT_PID"]:
                    match = re.search(name + r"=([^;]+);", agent)
                    if not match:
                        raise TransportError("Could not start SSH agent")
                    environment[name] = match.group(1)
                agent_started = True
                askpass = temporary / "askpass.sh"
                askpass.write_text('#!/bin/sh\nprintf \'%s\' "$LIGHTSAIL_SSH_KEY_PASSPHRASE"\n')
                askpass.chmod(0o700)
                environment.update({"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"})
                execute(["ssh-add", str(key)], input_text="", environment=environment)
            common = ["-i", str(key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                      "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(hosts),
                      "-o", "ConnectTimeout=15"]
            target = args.user + "@" + args.host
            run_id = os.environ.get("GITHUB_RUN_ID", "local")
            attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", run_id + attempt):
                raise TransportError("Invalid staging identifier")
            stage = "outlookemail-deploy-" + run_id + "-" + attempt
            bundle = Path(__file__).resolve().parents[2] / "deploy/lightsail"
            ssh = ["ssh", *common, "-T", target]
            payload = json.dumps({"ecr_password": token, "login_password": secrets["LOGIN_PASSWORD"]})
            remote = "sudo -n python3 " + shlex.quote(stage + "/deploy.py") + " " + shlex.quote(args.image) + " " + shlex.quote(args.commit)
            try:
                execute([*ssh, "mkdir -m 700 -p " + shlex.quote(stage)], environment=environment)
                execute(["scp", *common, str(bundle / "compose.yaml"), str(bundle / "Caddyfile"),
                         str(bundle / "deploy.py"), target + ":" + stage + "/"], environment=environment)
                # Output is limited to messages deliberately emitted by deploy.py.
                execute([*ssh, remote], input_text=payload, environment=environment, capture=False)
            finally:
                execute([*ssh, "rm -rf -- " + shlex.quote(stage)], environment=environment)
        finally:
            if agent_started:
                subprocess.run(["ssh-agent", "-k"], env=environment, capture_output=True)
    print("Deployment transport completed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, TransportError) else "Deployment transport failed", file=sys.stderr)
        sys.exit(1)
