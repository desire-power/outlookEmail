"""Offline deployment failure/secret/SSH rule checks. No AWS or SSH requests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deployment = load("aws_deployment", "deploy/lightsail/deploy.py")
firewall = load("aws_firewall", "scripts/aws/firewall.py")
transport = load("aws_transport", "scripts/aws/deploy_via_ssh.py")
IMAGE = deployment.REPOSITORY + "@sha256:" + "a" * 64
COMMIT = "b" * 40
CREDENTIALS = {"ecr_password": "synthetic-ecr-token", "login_password": "synthetic$pass#word'with\\slash"}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "host"
        self.bundle = ROOT / "deploy/lightsail"
        (self.root / "caddy").mkdir(parents=True)
        (self.root / "data").mkdir()
        (self.root / "host.env").write_text("OUTLOOK_EMAIL_DOMAIN=outlook.example.com\n")
        (self.root / "app.env").write_text("SECRET_KEY=fixed-synthetic-key\nLOGIN_PASSWORD=initial\n")
        (self.root / "compose.yaml").write_text("previous-compose\n")
        (self.root / "caddy/Caddyfile").write_text("previous-caddy\n")
        self.commands = []

    def command(self, args, **kwargs):
        self.commands.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    def perform(self, readiness=True, command=None):
        with patch.object(deployment, "run", side_effect=command or self.command), \
                patch.object(deployment, "healthy", return_value=readiness):
            deployment.deploy(self.root, self.bundle, IMAGE, COMMIT, CREDENTIALS)

    def existing(self):
        (self.root / "data/outlook_accounts.db").write_bytes(b"persisted-synthetic-db")
        (self.root / "deploy.env").write_text("OUTLOOK_EMAIL_IMAGE=previous-image\n")
        (self.root / "current-deployment.json").write_text('{"commit":"old"}\n')

    def test_first_deploy_preserves_fixed_key_and_creates_no_local_db_copy(self):
        self.perform()
        content = (self.root / "app.env").read_text()
        self.assertIn("SECRET_KEY=fixed-synthetic-key\n", content)
        self.assertNotIn("LOGIN_PASSWORD=initial", content)
        self.assertFalse((self.root / "data/outlook_accounts.db").exists())
        self.assertEqual(json.loads((self.root / "current-deployment.json").read_text()),
                         {"image": IMAGE, "commit": COMMIT})
        self.assertEqual((self.root / "app.env").stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(args[0] == "systemctl" for args, _ in self.commands))
        for args, _ in self.commands:
            self.assertNotIn(CREDENTIALS["login_password"], args)
            self.assertNotIn(CREDENTIALS["ecr_password"], args)

    def test_update_backs_up_before_recreate_and_preserves_database_password_key(self):
        self.existing()
        previous_env = (self.root / "app.env").read_bytes()
        self.perform()
        args = [item[0] for item in self.commands]
        backup = args.index(["systemctl", "start", "outlookemail-backup.service"])
        recreate = next(index for index, item in enumerate(args) if "up" in item)
        self.assertLess(backup, recreate)
        self.assertEqual((self.root / "app.env").read_bytes(), previous_env)
        self.assertEqual((self.root / "data/outlook_accounts.db").read_bytes(), b"persisted-synthetic-db")
        self.assertFalse(any("prune" in item for item in args))

    def test_backup_failure_does_not_replace_running_configuration(self):
        self.existing()
        def failing(args, **kwargs):
            self.command(args, **kwargs)
            if args[0] == "systemctl":
                raise deployment.DeployError("Synthetic backup failure")
            return subprocess.CompletedProcess(args, 0, "", "")
        with self.assertRaises(deployment.DeployError):
            self.perform(command=failing)
        self.assertEqual((self.root / "compose.yaml").read_text(), "previous-compose\n")
        self.assertFalse(any("up" in args for args, _ in self.commands))

    def test_failed_update_restores_previous_digest_and_proxy_without_restoring_db(self):
        self.existing()
        with self.assertRaises(deployment.DeployError):
            self.perform(readiness=False)
        self.assertEqual((self.root / "compose.yaml").read_text(), "previous-compose\n")
        self.assertEqual((self.root / "caddy/Caddyfile").read_text(), "previous-caddy\n")
        self.assertEqual((self.root / "deploy.env").read_text(), "OUTLOOK_EMAIL_IMAGE=previous-image\n")
        self.assertEqual((self.root / "current-deployment.json").read_text(), '{"commit":"old"}\n')
        self.assertEqual((self.root / "data/outlook_accounts.db").read_bytes(), b"persisted-synthetic-db")
        self.assertEqual(sum("up" in args for args, _ in self.commands), 2)
        self.assertFalse((self.root / "caddy/Caddyfile.next").exists())
        for args, _ in self.commands:
            if args[:2] == ["docker", "compose"]:
                self.assertIn("deploy.env", args)

    def test_proxy_failure_restores_waiting_page_on_first_deploy(self):
        with patch.object(deployment, "run", side_effect=self.command), \
                patch.object(deployment, "healthy", side_effect=[True, False]), \
                self.assertRaises(deployment.DeployError):
            deployment.deploy(self.root, self.bundle, IMAGE, COMMIT, CREDENTIALS)
        self.assertEqual((self.root / "caddy/Caddyfile").read_text(), "previous-caddy\n")
        self.assertFalse((self.root / "deploy.env").exists())
        self.assertIn(["docker", "stop", "outlookemail-app-1"], [args for args, _ in self.commands])

    def test_invalid_digest_and_credentials_fail_before_external_commands(self):
        for image, credentials in [("other.example/app@sha256:" + "a" * 64, CREDENTIALS),
                                   (IMAGE, {**CREDENTIALS, "login_password": "bad\npassword-long"})]:
            with patch.object(deployment, "run") as command, self.assertRaises(deployment.DeployError):
                deployment.deploy(self.root, self.bundle, image, COMMIT, credentials)
            command.assert_not_called()


class FirewallTests(unittest.TestCase):
    def invoke(self, operation, states):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "environment"
            path.touch()
            with patch.dict(os.environ, {"GITHUB_ENV": str(path)}), \
                    patch("sys.argv", ["firewall.py", operation, "prd-outlookemail", "8.8.8.8/32"]), \
                    patch.object(firewall, "aws", return_value={"portStates": states}) as request:
                firewall.main()
            return request.call_args_list, path.read_text()

    def test_existing_rule_or_wider_network_is_preserved(self):
        for cidr in ["8.8.8.8/32", "8.8.8.0/24"]:
            calls, metadata = self.invoke("open", [{"protocol": "tcp", "fromPort": 22, "toPort": 22,
                                                    "state": "open", "cidrs": [cidr]}])
            self.assertEqual(len(calls), 1)
            self.assertEqual(metadata, "")

    def test_new_rule_marks_cleanup_and_opens_only_one_ip_port22(self):
        calls, metadata = self.invoke("open", [])
        self.assertEqual(metadata, "RUNNER_SSH_RULE_ADDED=true\n")
        self.assertEqual(calls[1].args[0], "open-instance-public-ports")
        self.assertEqual(json.loads(calls[1].args[-1]),
                         {"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrs": ["8.8.8.8/32"]})

    def test_cleanup_metadata_is_written_even_if_open_response_fails(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "environment"
            with patch.dict(os.environ, {"GITHUB_ENV": str(path)}), \
                    patch("sys.argv", ["firewall.py", "open", "prd-outlookemail", "8.8.8.8/32"]), \
                    patch.object(firewall, "aws", side_effect=[{"portStates": []}, RuntimeError("network")]), \
                    self.assertRaises(RuntimeError):
                firewall.main()
            self.assertEqual(path.read_text(), "RUNNER_SSH_RULE_ADDED=true\n")

    def test_closed_or_http_rules_do_not_grant_ssh(self):
        for rule in [{"protocol": "tcp", "fromPort": 22, "toPort": 22, "state": "closed"},
                     {"protocol": "tcp", "fromPort": 80, "toPort": 80, "state": "open"}]:
            self.assertFalse(firewall.already_allowed([{**rule, "cidrs": ["8.8.8.8/32"]}], "8.8.8.8/32"))

    def test_close_uses_exact_cidr_without_replacing_all_rules(self):
        calls, _ = self.invoke("close", [])
        self.assertEqual(calls[0].args[0], "close-instance-public-ports")
        self.assertEqual(json.loads(calls[0].args[-1])["cidrs"], ["8.8.8.8/32"])


class TransportTests(unittest.TestCase):
    def test_missing_secrets_or_public_key_are_rejected(self):
        for values in [{}, {"LOGIN_PASSWORD": "synthetic-password", "LIGHTSAIL_SSH_PRIVATE_KEY": "ssh-rsa public",
                            "LIGHTSAIL_SSH_KNOWN_HOSTS": "synthetic-host-key"}]:
            with patch.dict(os.environ, values, clear=True), self.assertRaises(transport.TransportError):
                transport.required_secrets()

    def test_scp_failure_still_cleans_staging_and_does_not_leak_secrets_in_args(self):
        commands = []
        def execute(args, **kwargs):
            commands.append((args, kwargs))
            if args[0] == "aws":
                return CREDENTIALS["ecr_password"]
            if args[0] == "scp":
                raise transport.TransportError("synthetic upload failure")
            return ""
        environment = {"LOGIN_PASSWORD": CREDENTIALS["login_password"],
                       "LIGHTSAIL_SSH_PRIVATE_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n",
                       "LIGHTSAIL_SSH_KNOWN_HOSTS": "synthetic-host-key", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
        with patch.dict(os.environ, environment, clear=True), \
                patch("sys.argv", ["deploy_via_ssh.py", "--image", IMAGE, "--commit", COMMIT]), \
                patch.object(transport, "execute", side_effect=execute), \
                self.assertRaises(transport.TransportError):
            transport.main()
        self.assertEqual(commands[-1][0][-1], "rm -rf -- outlookemail-deploy-123-1")
        ssh = next(args for args, _ in commands if args[0] == "ssh")
        self.assertIn("StrictHostKeyChecking=yes", ssh)
        for args, _ in commands:
            self.assertNotIn(CREDENTIALS["login_password"], args)
            self.assertNotIn(CREDENTIALS["ecr_password"], args)


if __name__ == "__main__":
    unittest.main()
