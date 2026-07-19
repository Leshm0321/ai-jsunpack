import subprocess
import tempfile
import unittest
from pathlib import Path

from deploy.release_readiness import (
    ReleaseReadinessConfig,
    parse_repository,
    run_release_readiness,
)


class ReleaseReadinessTest(unittest.TestCase):
    def test_release_readiness_reports_ready_when_external_tools_are_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow = Path(temp_dir) / "release-gate.yml"
            workflow.write_text("name: Release Gate\n", encoding="utf-8")
            report = run_release_readiness(
                ReleaseReadinessConfig(
                    repository="owner/ai-jsunpack",
                    workflow_path=workflow,
                    output_path=Path(temp_dir) / "readiness.json",
                ),
                command_runner=_runner(
                    {
                        ("gh", "--version"): _ok("gh version 2.87.3"),
                        ("gh", "auth", "status"): _ok("Logged in to github.com"),
                        ("gh", "workflow", "list", "--repo", "owner/ai-jsunpack"): _ok("Release Gate enabled"),
                        ("docker", "info", "--format", "{{json .ServerVersion}}"): _ok('"29.1.3"'),
                    }
                ),
            )

            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["failedChecks"], [])
            self.assertEqual(report["blockers"], [])
            self.assertEqual(
                {secret["name"] for secret in report["requiredSecrets"] if secret["required"]},
                {
                    "AI_JSUNPACK_AUTH_SECRET",
                    "AI_JSUNPACK_ARTIFACT_S3_SECRET_ACCESS_KEY",
                    "AI_JSUNPACK_BROWSER_RUNNER_TOKEN",
                },
            )

    def test_release_readiness_reports_blockers_without_repository_auth_or_docker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow = Path(temp_dir) / "release-gate.yml"
            workflow.write_text("name: Release Gate\n", encoding="utf-8")
            report = run_release_readiness(
                ReleaseReadinessConfig(
                    workflow_path=workflow,
                    output_path=Path(temp_dir) / "readiness.json",
                ),
                command_runner=_runner(
                    {
                        ("git", "config", "--get", "remote.origin.url"): _fail(""),
                        ("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"): _fail(
                            "not logged in"
                        ),
                        ("gh", "--version"): _ok("gh version 2.87.3"),
                        ("gh", "auth", "status"): _fail("not logged in"),
                        ("docker", "info", "--format", "{{json .ServerVersion}}"): _fail("daemon unavailable"),
                    }
                ),
            )

            self.assertEqual(report["status"], "blocked")
            self.assertIn("github_repository_known", report["blockers"])
            self.assertIn("github_cli_authenticated", report["blockers"])
            self.assertIn("docker_daemon_available", report["blockers"])
            self.assertIn("release_gate_workflow_visible", report["blockers"])
            self.assertTrue(any("配置 git remote origin" in action for action in report["nextActions"]))

    def test_parse_repository_accepts_github_remote_urls(self):
        self.assertEqual(parse_repository("git@github.com:owner/ai-jsunpack.git"), "owner/ai-jsunpack")
        self.assertEqual(parse_repository("https://github.com/owner/ai-jsunpack.git"), "owner/ai-jsunpack")
        self.assertEqual(parse_repository("owner/ai-jsunpack"), "owner/ai-jsunpack")
        self.assertEqual(parse_repository("not a slug"), "")


def _runner(results: dict[tuple[str, ...], subprocess.CompletedProcess[str]]):
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return results.get(tuple(command), _fail("unexpected command"))

    return run


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


if __name__ == "__main__":
    unittest.main()
