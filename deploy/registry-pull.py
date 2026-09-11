#!/usr/bin/env python3
"""Make sure an image is present locally, using the host's proxy only if the normal pull fails.

The ordinary path is untouched: a plain `docker pull` runs first, and on a machine that can reach
the registry nothing else in this file ever executes. The rest exists for networks where the
registry is only reachable through a proxy that the Docker daemon does not know about.

Every documented way to give the daemon a proxy edits daemon configuration and restarts a service
the operator may share with other work, so this script does none of that. It serves the upstream
registry on loopback instead, which Docker already treats as insecure without configuration, pulls
through that, and renames the result to its canonical tag. The loopback address is never written
anywhere: the image in the local store carries the real name, so the next update repeats this
decision from scratch rather than depending on a port that has since gone away.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


DEPLOYMENT = Path(__file__).resolve().parent


def _load(name: str):
    """Import a sibling helper whose filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), DEPLOYMENT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = _load("registry-preflight")
forwarder = _load("registry-forwarder")


def split_reference(image: str) -> tuple[str, str, str]:
    """Split an image reference into registry, repository and tag or digest."""
    registry = preflight.registry_host(image)
    remainder = image[len(registry) + 1 :] if image.startswith(registry + "/") else image
    if "@" in remainder:
        repository, _, reference = remainder.partition("@")
    elif ":" in remainder.rsplit("/", 1)[-1]:
        repository, _, reference = remainder.rpartition(":")
    else:
        repository, reference = remainder, "latest"
    return registry, repository, reference


def run(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, text=True, capture_output=capture)


def image_id(reference: str) -> str:
    result = run(["docker", "image", "inspect", "--format", "{{.Id}}", reference], capture=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def image_present(image: str) -> bool:
    return bool(image_id(image))


def drop_stale_loopback_tags(repository: str) -> None:
    """Remove loopback tags a previous run may have left behind.

    The loopback name is an artefact of one pull. Leaving it around would accumulate tags that
    point at images nobody can fetch again, because the port they name is gone.
    """
    listing = run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture=True
    )
    if listing.returncode:
        return
    for line in listing.stdout.splitlines():
        name = line.strip()
        if name.startswith("127.0.0.1:") and f"/{repository}:" in name:
            run(["docker", "rmi", name], capture=True)


def plain_pull(image: str) -> tuple[bool, str]:
    result = run(["docker", "pull", image], capture=True)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def pull_through_forwarder(image: str, proxy: str) -> tuple[bool, str]:
    """Pull via a loopback view of the registry, then move the canonical name onto the result.

    Layers already in the local store are matched by content digest, so pulling under a different
    repository name is not supposed to re-download anything that is already here. The existing
    image is nevertheless given the loopback name first, which costs one tag and removes any
    dependence on that behaviour.

    The canonical tag is not touched until a pull has succeeded and the tag has been verified to
    point at what was just fetched. A failure anywhere leaves the previous image exactly where it
    was, because the alternative - reporting success while the old image still answers to the right
    name - would have the caller run a stale build believing it had just updated.
    """
    registry, repository, reference = split_reference(image)
    drop_stale_loopback_tags(repository)
    previous = image_id(image)
    server = forwarder.serve(registry, proxy, "127.0.0.1", 0, "")
    port = server.server_address[1]
    local = f"127.0.0.1:{port}/{repository}"
    local += f"@{reference}" if reference.startswith("sha256:") else f":{reference}"
    seeded = False
    try:
        if previous and not reference.startswith("sha256:"):
            seeded = run(["docker", "tag", image, local], capture=True).returncode == 0
        result = run(["docker", "pull", local], capture=True)
        detail = (result.stderr or result.stdout).strip()
        if result.returncode:
            return False, detail
        pulled = image_id(local)
        if not pulled:
            return False, "the pull reported success but produced no image"
        if seeded and pulled == previous and "up to date" not in detail.lower():
            return False, "the pull did not produce a new image; refusing to claim an update"
        if run(["docker", "tag", local, image], capture=True).returncode:
            return False, f"could not move {image} onto the image that was just pulled"
        if image_id(image) != pulled:
            return False, f"{image} still points at the previous image; refusing to use a stale copy"
        if previous and previous != pulled:
            print(f"  Replaced the previous {image} ({previous[7:19]}) with {pulled[7:19]}.")
        return True, ""
    finally:
        # The loopback name is an artefact of this run; the port it carries is about to disappear.
        run(["docker", "rmi", local], capture=True)
        server.shutdown()
        server.server_close()


def choose(question: str, decision: str) -> str:
    """Ask the operator how to proceed, or fail loudly when nobody can answer."""
    if decision in {"direct", "abort"}:
        return decision
    if not sys.stdin.isatty():
        return "abort"
    print(question)
    try:
        answer = input("  [d] retry the direct connection  [q] stop here > ").strip().lower()
    except EOFError:
        return "abort"
    return "direct" if answer.startswith("d") else "abort"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument(
        "--proxy", default=os.environ.get("PREDICTION_AGENT_REGISTRY_PROXY", "auto")
    )
    parser.add_argument(
        "--on-unreachable",
        default=os.environ.get("PREDICTION_AGENT_REGISTRY_FALLBACK", "ask"),
        choices=("ask", "direct", "abort"),
        help="what to do when neither a direct connection nor a proxy reaches the registry",
    )
    arguments = parser.parse_args()
    image = arguments.image

    ok, detail = plain_pull(image)
    if ok:
        return 0
    print(f"Pulling {image} failed. Checking how this machine can reach the registry…")
    print(f"  {detail.splitlines()[-1] if detail else 'no detail reported'}")

    decision = preflight.decide(image, str(arguments.proxy).strip() or "auto")
    proxy, source = decision["proxy"], decision["proxy_source"]
    print(f"  registry {decision['registry']}: direct={'ok' if decision['direct']['ok'] else 'no'}", end="")
    if proxy:
        reachable = decision["through_proxy"].get("ok")
        print(f", via {source} proxy {proxy}={'ok' if reachable else 'no'}")
    else:
        print(", no proxy configured on this host")

    if proxy and decision["through_proxy"].get("ok"):
        print("  Serving the registry on loopback through that proxy; Docker's own settings are untouched.")
        ok, detail = pull_through_forwarder(image, proxy)
        if ok:
            print(f"  Pulled {image} through the host proxy.")
            return 0
        last = detail.splitlines()[-1] if detail else ""
        print(f"  That did not work either: {last}")
        if "connection refused" in last or "no such host" in last:
            print(
                "  The Docker daemon does not share this machine's loopback address, which is the\n"
                "  case with Docker Desktop, where the daemon runs inside its own virtual machine.\n"
                "  Reaching it by name would require a certificate in Docker's own configuration,\n"
                "  which this script will not install. Turn the proxy on in Docker Desktop's\n"
                "  settings yourself, or run this again once the registry is reachable."
            )

    existing = image_id(image)
    if existing:
        print(
            f"  {image} was not updated. Continuing with the copy already on this machine"
            f" ({existing[7:19]}), which may be out of date."
        )
        return 0

    action = choose(
        "  The registry could not be reached and this image is not present locally.",
        arguments.on_unreachable,
    )
    if action == "direct":
        ok, detail = plain_pull(image)
        if ok:
            return 0
        print(f"  Still failing: {detail.splitlines()[-1] if detail else ''}")
    print("  Stopping without starting the robot.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
