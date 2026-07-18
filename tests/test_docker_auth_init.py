import tempfile
import unittest
from pathlib import Path

from apps.api.app.auth import verify_auth_token
from deploy.auth_init import generate_tokens


class DockerAuthInitTest(unittest.TestCase):
    def test_generates_owner_and_worker_service_tokens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = generate_tokens(
                Path(temp_dir),
                secret="test-secret",
                web_ttl_seconds=60,
                service_ttl_seconds=60,
            )
            web_token = Path(paths["web-token"]).read_text(encoding="utf-8").strip()
            service_token = Path(paths["browser-runner-token"]).read_text(encoding="utf-8").strip()

        web_context = verify_auth_token(web_token, secret="test-secret")
        service_context = verify_auth_token(service_token, secret="test-secret")
        self.assertTrue(web_context.has_project_role("default", "owner"))
        self.assertEqual(web_context.kind, "user")
        self.assertEqual(service_context.kind, "service")
        self.assertTrue(service_context.has_service_role("worker"))


if __name__ == "__main__":
    unittest.main()
