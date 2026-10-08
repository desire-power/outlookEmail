#!/usr/bin/env python3
"""Runs as root on the existing Lightsail VM. Credentials arrive only on stdin."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

REPOSITORY = "084828573765.dkr.ecr.ap-northeast-1.amazonaws.com/prd-outlookemail"


class DeployError(RuntimeError):
    pass


def run(command, *, root, input_text=None, check=True):
    result = subprocess.run(command, cwd=root, input=input_text, text=True,
                            capture_output=True)
    if check and result.returncode:
        # Native errors can include environment values. Never echo their raw output.
        raise DeployError("Command failed: " + command[0])
    return result


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content if isinstance(content, bytes) else content.encode())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_inputs(image, commit, credentials):
    if not re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[a-f0-9]{64}", image):
        raise DeployError("Expected a digest from the dedicated ECR repository")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise DeployError("Expected a full commit SHA")
    for name in ["ecr_password", "login_password"]:
        value = credentials.get(name)
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
            raise DeployError("Missing or invalid deployment credential")
    if len(credentials["login_password"]) < 12:
        raise DeployError("LOGIN_PASSWORD must have at least 12 characters")


def set_initial_password(path, password):
    # Quote using dotenv double-quote escapes; $$ prevents Compose interpolation.
    quoted = json.dumps(password, ensure_ascii=False).replace("$", "$$")
    lines = path.read_text().splitlines()
    updated = ["LOGIN_PASSWORD=" + quoted if line.startswith("LOGIN_PASSWORD=") else line
               for line in lines]
    if not any(line.startswith("LOGIN_PASSWORD=") for line in lines):
        updated.append("LOGIN_PASSWORD=" + quoted)
    atomic_write(path, "\n".join(updated) + "\n")


def compose(root, *args, check=True):
    command = ["docker", "compose", "--project-name", "outlookemail",
               "--env-file", "host.env", "--env-file", "deploy.env", "-f", "compose.yaml", *args]
    return run(command, root=root, check=check)


def healthy(url, attempts=60):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    return False


def restore_snapshot(snapshot):
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, content, 0o644 if path.name == "Caddyfile" else 0o600)


def deploy(root, bundle, image, commit, credentials):
    validate_inputs(image, commit, credentials)
    for name in ["host.env", "app.env", "compose.yaml", "caddy/Caddyfile"]:
        if not (root / name).is_file():
            raise DeployError("Lightsail bootstrap files are missing")
    if not any(line.startswith("SECRET_KEY=") and line.partition("=")[2]
               for line in (root / "app.env").read_text().splitlines()):
        raise DeployError("The fixed SECRET_KEY is missing")
    database = root / "data/outlook_accounts.db"
    was_deployed = (root / "deploy.env").exists()
    snapshot = {root / name: (root / name).read_bytes() if (root / name).exists() else None
                for name in ["compose.yaml", "caddy/Caddyfile", "deploy.env", "current-deployment.json"]}
    registry = REPOSITORY.split("/", 1)[0]
    print("Pulling the verified image digest", flush=True)
    with tempfile.TemporaryDirectory(prefix="ecr-login-") as auth_directory:
        # Keep the ECR token out of persistent Docker configuration on this VM.
        run(["docker", "--config", auth_directory, "login", "--username", "AWS",
             "--password-stdin", registry], root=root,
            input_text=credentials["ecr_password"] + "\n")
        run(["docker", "--config", auth_directory, "pull", image], root=root)

    if database.exists():
        print("Backing up existing application data", flush=True)
        # systemctl start waits for the oneshot job; failure aborts before recreating the app.
        run(["systemctl", "start", "outlookemail-backup.service"], root=root)
    else:
        set_initial_password(root / "app.env", credentials["login_password"])

    mutated = False
    try:
        mutated = True
        atomic_write(root / "deploy.env", "OUTLOOK_EMAIL_IMAGE=" + image + "\n")
        atomic_write(root / "compose.yaml", (bundle / "compose.yaml").read_bytes())
        compose(root, "config", "--quiet")
        print("Starting the application (single instance)", flush=True)
        # Recreate the same service, never run two schedulers against the same SQLite DB.
        compose(root, "up", "-d", "--no-build", "--pull", "never")
        if not healthy("http://127.0.0.1:5000/login"):
            raise DeployError("Application readiness check failed")
        atomic_write(root / "caddy/Caddyfile.next", (bundle / "Caddyfile").read_bytes(), 0o644)
        compose(root, "exec", "-T", "caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile.next")
        atomic_write(root / "caddy/Caddyfile", (bundle / "Caddyfile").read_bytes(), 0o644)
        compose(root, "exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile")
        if not healthy("http://127.0.0.1:8080/healthz", attempts=15):
            raise DeployError("Proxy readiness check failed")
        atomic_write(root / "current-deployment.json", json.dumps({"image": image, "commit": commit}) + "\n")
        print("Application and proxy are ready", flush=True)
    except Exception as error:
        failure = str(error) if isinstance(error, DeployError) else "Unexpected deployment error"
        if mutated:
            print("Deployment failed; restoring the previous image/configuration", flush=True)
            # Do not restore the DB automatically: preserve writes and avoid destructive rollback.
            restore_snapshot(snapshot)
            try:
                if was_deployed:
                    compose(root, "up", "-d", "--no-build", "--pull", "never")
                    compose(root, "exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile")
                else:
                    # First deployment failed: retain the waiting page and stop the failed app.
                    run(["docker", "stop", "outlookemail-app-1"], root=root, check=False)
                    run(["docker", "compose", "--project-name", "outlookemail", "-f", "compose.yaml", "up", "-d"], root=root)
                    run(["docker", "compose", "--project-name", "outlookemail", "-f", "compose.yaml",
                         "exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"], root=root)
            except Exception:
                print("Rollback requires manual inspection; data and backups have been retained", flush=True)
        raise DeployError("Deployment failed: " + failure + "; inspect the VM without sharing secret-bearing logs") from None
    finally:
        (root / "caddy/Caddyfile.next").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("commit")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise DeployError("Run this script with sudo")
    os.umask(0o077)
    root = Path("/opt/outlookemail")
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        credentials = json.load(sys.stdin)
        deploy(root, Path(__file__).resolve().parent, args.image, args.commit, credentials)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Credentials are never included in these messages or traceback output.
        print(str(error) if isinstance(error, DeployError) else "Deployment failed", file=sys.stderr)
        sys.exit(1)
