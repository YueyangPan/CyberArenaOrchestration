import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env first so live tests (e.g. M4 smoke against the real router) pick
# up real credentials. Then setdefault placeholders for any var still missing,
# so non-LLM tests (M1/M2/M3) keep working without secrets.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

os.environ.setdefault("API_ROUTER_BASE", "https://api.anthropic.com")
os.environ.setdefault("API_ROUTER_KEY", "test-placeholder-key")


# ── DEFCON shared fixtures (docker-required tests) ─────────────────────


def _docker_available() -> bool:
    """True iff a docker daemon is reachable. Tests that need docker
    skip via this so the suite still runs on machines without docker."""
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


@pytest.fixture(scope="session")
def docker_required():
    """Skip the calling test if no docker daemon is reachable."""
    if not _docker_available():
        pytest.skip("docker daemon unavailable; skipping live container test")


@pytest.fixture(scope="session")
def dcf_ico_image(docker_required) -> str:
    """Build (or cache-hit) the nautilus-ico image once per session.
    Subsequent fixture invocations re-use the same tag.

    The image build is ~70s cold / ~1s cache-warm. Session scope keeps
    that out of every test.
    """
    from challenges.nautilus import NautilusSource

    src = NautilusSource(include=["ico"])
    try:
        specs = src.list_challenges()
    except FileNotFoundError as e:
        pytest.skip(f"nautilus vendor not synced: {e}")
    ico_spec = next(s for s in specs if s.name == "nautilus-ico")
    return src.build_image(ico_spec)


@pytest.fixture
def dcf_ico_container(dcf_ico_image, request):
    """Bring up a fresh container from the ico image; clean up after the test.

    Yields the container name (string). Caller talks to it via
    `docker exec`. Each test gets its own container so writes / patches
    are isolated.
    """
    # Unique per-test container name (collision-safe across parametrized runs).
    name = f"dcf-ico-{abs(hash(request.node.nodeid)) % (10**8):08d}"
    # Defensive: kill any leftover with the same name from a prior crash.
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d",
            "--name", name,
            "--platform=linux/amd64",
            dcf_ico_image,
        ],
        check=True,
        capture_output=True,
    )
    # Wait for entrypoint to finish initial fpc build + sshd start (~5-10s).
    import time as _time

    deadline = _time.monotonic() + 30
    while _time.monotonic() < deadline:
        rc = subprocess.run(
            ["docker", "exec", name, "pgrep", "sshd"],
            capture_output=True,
        ).returncode
        if rc == 0:
            break
        _time.sleep(0.3)
    else:
        # Surface the entrypoint log for triage instead of a silent fail.
        logs = subprocess.run(
            ["docker", "logs", name], capture_output=True, text=True
        ).stdout
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        raise RuntimeError(f"sshd didn't start within 30s. Logs:\n{logs}")

    yield name

    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
