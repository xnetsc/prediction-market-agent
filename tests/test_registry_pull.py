from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "deploy" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = _load("registry-pull")
preflight = _load("registry-preflight")


class FakeDocker:
    """Records docker invocations and answers them from a scripted image store."""

    def __init__(self, images: dict[str, str], *, pull_result: str | None = None):
        self.images = dict(images)
        self.pull_result = pull_result
        self.calls: list[list[str]] = []
        self.tag_failures: set[str] = set()

    def __call__(self, arguments, *, capture=False):
        self.calls.append(list(arguments))
        command = arguments[1:]
        if command[:2] == ["image", "inspect"]:
            reference = command[-1]
            if reference in self.images:
                return subprocess.CompletedProcess(arguments, 0, self.images[reference] + "\n", "")
            return subprocess.CompletedProcess(arguments, 1, "", "No such image")
        if command[0] == "images":
            listing = "\n".join(self.images)
            return subprocess.CompletedProcess(arguments, 0, listing, "")
        if command[0] == "pull":
            if self.pull_result is None:
                return subprocess.CompletedProcess(arguments, 1, "", "connection refused")
            self.images[command[1]] = self.pull_result
            return subprocess.CompletedProcess(arguments, 0, "Downloaded newer image", "")
        if command[0] == "tag":
            source, target = command[1], command[2]
            if target in self.tag_failures:
                return subprocess.CompletedProcess(arguments, 1, "", "tag refused")
            self.images[target] = self.images.get(source, "")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if command[0] == "rmi":
            self.images.pop(command[1], None)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(arguments, 0, "", "")


class ReferenceTests(unittest.TestCase):
    def test_references_split_into_registry_repository_and_version(self) -> None:
        cases = {
            "ghcr.io/owner/app:latest": ("ghcr.io", "owner/app", "latest"),
            "ghcr.io/owner/app": ("ghcr.io", "owner/app", "latest"),
            "ghcr.io/owner/app@sha256:abc": ("ghcr.io", "owner/app", "sha256:abc"),
            "localhost:5000/app:v2": ("localhost:5000", "app", "v2"),
            "alpine:3": ("registry-1.docker.io", "alpine", "3"),
        }
        for reference, expected in cases.items():
            with self.subTest(reference=reference):
                self.assertEqual(driver.split_reference(reference), expected)


class ForwarderPullTests(unittest.TestCase):
    IMAGE = "ghcr.io/owner/app:latest"

    def setUp(self) -> None:
        self._run = driver.run
        self._serve = driver.forwarder.serve

        class Server:
            server_address = ("127.0.0.1", 45999)

            def shutdown(self) -> None:
                pass

            def server_close(self) -> None:
                pass

        driver.forwarder.serve = lambda *args, **kwargs: Server()
        self.addCleanup(setattr, driver, "run", self._run)
        self.addCleanup(setattr, driver.forwarder, "serve", self._serve)

    def _install(self, docker: FakeDocker) -> None:
        driver.run = docker

    def test_a_successful_pull_moves_the_canonical_tag_to_the_new_image(self) -> None:
        docker = FakeDocker({self.IMAGE: "sha256:old"}, pull_result="sha256:new")
        self._install(docker)
        ok, detail = driver.pull_through_forwarder(self.IMAGE, "http://proxy:1")
        self.assertTrue(ok, detail)
        self.assertEqual(docker.images[self.IMAGE], "sha256:new")

    def test_a_failed_pull_leaves_the_previous_image_in_place(self) -> None:
        docker = FakeDocker({self.IMAGE: "sha256:old"}, pull_result=None)
        self._install(docker)
        ok, detail = driver.pull_through_forwarder(self.IMAGE, "http://proxy:1")
        self.assertFalse(ok)
        self.assertIn("connection refused", detail)
        self.assertEqual(
            docker.images[self.IMAGE], "sha256:old", "a failure must not disturb the existing image"
        )

    def test_a_refused_retag_is_not_reported_as_an_update(self) -> None:
        """The previous image still answers to the canonical name, so this must fail loudly."""
        docker = FakeDocker({self.IMAGE: "sha256:old"}, pull_result="sha256:new")
        docker.tag_failures.add(self.IMAGE)
        self._install(docker)
        ok, detail = driver.pull_through_forwarder(self.IMAGE, "http://proxy:1")
        self.assertFalse(ok)
        self.assertIn("could not move", detail)
        self.assertEqual(docker.images[self.IMAGE], "sha256:old")

    def test_the_loopback_name_never_survives_the_call(self) -> None:
        for pull_result in ("sha256:new", None):
            with self.subTest(pulled=bool(pull_result)):
                docker = FakeDocker({self.IMAGE: "sha256:old"}, pull_result=pull_result)
                self._install(docker)
                driver.pull_through_forwarder(self.IMAGE, "http://proxy:1")
                leftovers = [name for name in docker.images if name.startswith("127.0.0.1:")]
                self.assertEqual(leftovers, [], "the port in that name is gone once this returns")

    def test_loopback_tags_from_earlier_runs_are_cleared_first(self) -> None:
        docker = FakeDocker(
            {self.IMAGE: "sha256:old", "127.0.0.1:1111/owner/app:latest": "sha256:ancient"},
            pull_result="sha256:new",
        )
        self._install(docker)
        driver.pull_through_forwarder(self.IMAGE, "http://proxy:1")
        self.assertNotIn("127.0.0.1:1111/owner/app:latest", docker.images)

    def test_the_existing_image_is_offered_under_the_loopback_name_before_pulling(self) -> None:
        """Seeding the name costs one tag and removes any reliance on cross-repository reuse."""
        docker = FakeDocker({self.IMAGE: "sha256:old"}, pull_result="sha256:new")
        self._install(docker)
        driver.pull_through_forwarder(self.IMAGE, "http://proxy:1")
        tags = [call for call in docker.calls if call[1:2] == ["tag"]]
        self.assertTrue(
            tags and tags[0][2] == self.IMAGE and tags[0][3].startswith("127.0.0.1:"),
            f"expected the existing image to be seeded first, saw {tags}",
        )


class PreflightTests(unittest.TestCase):
    def test_registry_host_follows_docker_defaults(self) -> None:
        self.assertEqual(preflight.registry_host("ghcr.io/owner/app:1"), "ghcr.io")
        self.assertEqual(preflight.registry_host("owner/app:1"), "registry-1.docker.io")
        self.assertEqual(preflight.registry_host("localhost:5000/app"), "localhost:5000")

    def test_a_direct_route_is_preferred_and_needs_no_proxy(self) -> None:
        self._patch(direct=True, through=False, daemon={})
        decision = preflight.decide("ghcr.io/owner/app:1", "off")
        self.assertEqual(decision["action"], "proceed")

    def test_a_working_proxy_the_daemon_does_not_use_calls_for_the_loopback_route(self) -> None:
        self._patch(direct=False, through=True, daemon={"available": "true"})
        decision = preflight.decide("ghcr.io/owner/app:1", "http://proxy:1")
        self.assertEqual(decision["action"], "proxy-pull")

    def test_a_proxy_the_daemon_already_uses_needs_no_special_handling(self) -> None:
        self._patch(direct=False, through=True, daemon={"available": "true", "https_proxy": "http://p:1"})
        decision = preflight.decide("ghcr.io/owner/app:1", "http://proxy:1")
        self.assertEqual(decision["action"], "proceed")

    def test_an_unusable_proxy_leaves_the_choice_to_the_operator(self) -> None:
        self._patch(direct=False, through=False, daemon={"available": "true"})
        decision = preflight.decide("ghcr.io/owner/app:1", "http://proxy:1")
        self.assertEqual(decision["action"], "ask")

    def _patch(self, *, direct: bool, through: bool, daemon: dict) -> None:
        original_probe, original_daemon = preflight.probe, preflight.daemon_proxy
        self.addCleanup(setattr, preflight, "probe", original_probe)
        self.addCleanup(setattr, preflight, "daemon_proxy", original_daemon)
        preflight.probe = lambda host, proxy: {"ok": through if proxy else direct}
        preflight.daemon_proxy = lambda: daemon


if __name__ == "__main__":
    unittest.main()
