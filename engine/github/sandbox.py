"""
R2.11 PR service-mode execution sandbox (DESIGN_v2.md §9 ⚠️).

The verifier's ``run_command`` tool executes untrusted code. On a local,
trusted working tree that runs on the host; for random external GitHub PRs
(PR service mode) the DESIGN requires an ephemeral container:

    review job → ephemeral container → clone PR
      → NO production credentials, NO host filesystem, NO docker socket
      → network default-deny → resource caps → GitReins agent tools
    Then let the verifier go wild.

:class:`DockerSandbox` implements that contract with the **docker CLI via
subprocess** — no new dependencies, mirroring how ``checkout.py`` shells out
to ``gh``. Every :meth:`DockerSandbox.run` is one ``docker run --rm`` with:

- ``--network none`` — network default-deny (explicit opt-in to open it)
- resource caps: ``--cpus`` / ``--memory`` / ``--pids-limit`` plus a
  wall-clock timeout per command
- the PR checkout mounted **read-only** at ``/work`` — never ``/``, ``/home``
  or the docker socket (enforced by :func:`_deny_host_mount`)
- a read-only rootfs with a tmpfs ``/tmp`` (writable scratch without host
  mounts) plus an optional host scratch dir mounted at ``/scratch``
- **no production credentials**: only allowlisted, scrubbed env vars
  (:func:`scrub_env` — GITHUB_TOKEN / GITREINS_LLM_* / OPENAI_* / *_TOKEN /
  *_API_KEY / ... can never reach the container, even when explicitly passed)
- ``--rm`` + ``--cidfile``: the container is removed after every run, and a
  timed-out/errored run force-removes it (:meth:`DockerSandbox._force_remove`)
  so no ephemeral container ever outlives its run

Missing docker / dead daemon raise :class:`SandboxUnavailableError` (graceful
degradation — callers fall back to host mode or report a clear error). The
verifier routes its ``run_command`` tool through any :class:`Sandbox` via
``VerifierAgent(..., sandbox=...)`` (engine/review/verifier.py); host
execution stays the default.

``clone_pr_checkout`` materializes a PR head checkout on the host
(``gh repo clone`` + ``gh pr checkout``) so :meth:`DockerSandbox.from_pull_request`
has a minimal read-only directory to mount. The clone runs on the HOST
because gh needs host credentials; the sandbox only ever sees the checkout.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger("gitreins.github.sandbox")

__all__ = [
    "SandboxError",
    "SandboxUnavailableError",
    "SandboxTimeoutError",
    "SandboxResult",
    "Sandbox",
    "DockerSandbox",
    "clone_pr_checkout",
    "scrub_env",
    "_run_subprocess",
    "DEFAULT_IMAGE",
    "DEFAULT_CHECKOUT_MOUNT",
    "DEFAULT_SCRATCH_MOUNT",
    "CREDENTIAL_ENV_PREFIXES",
    "CREDENTIAL_ENV_SUFFIXES",
    "CREDENTIAL_ENV_KEYS",
    "DENIED_MOUNT_PATHS",
]

#: Default image for sandboxed runs — a Python image so the verifier can run
#: the PR's tests/lint/build. Callers override for other toolchains.
DEFAULT_IMAGE = "python:3.12-slim"

#: Container path the read-only PR checkout is mounted at (also Sandbox.cwd).
DEFAULT_CHECKOUT_MOUNT = "/work"

#: Container path for the optional writable scratch dir.
DEFAULT_SCRATCH_MOUNT = "/scratch"

#: tmpfs /tmp size — writable scratch inside a read-only rootfs, no host mount.
DEFAULT_TMPFS_SIZE = "128m"

#: Default per-command resource caps (docker flags).
DEFAULT_CPU_LIMIT = 2.0
DEFAULT_MEMORY_LIMIT = "2g"
DEFAULT_PIDS_LIMIT = 512
DEFAULT_COMMAND_TIMEOUT = 120.0

#: Host paths that must NEVER be mounted into the sandbox — the docker socket
#: and the ``/`` and ``/home`` roots themselves. A *specific* checkout dir
#: under them (e.g. ``/home/alice/work/repo``) is the minimal read-only mount
#: the DESIGN explicitly allows, so only the roots are denied.
DENIED_MOUNT_PATHS = (
    "/",
    "/home",
    "/root",
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/lib/docker",
)

#: Env var name prefixes treated as credentials — scrubbed from every sandbox
#: environment (GITREINS_LLM_* covers the API key, base URL, model config…).
CREDENTIAL_ENV_PREFIXES = (
    "GITHUB_TOKEN", "GH_TOKEN", "GITREINS_LLM_", "GITREINS_API_KEY",
    "GITREINS_GH_", "OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "GEMINI_",
    "GOOGLE_", "GCP_", "AWS_", "AZURE_", "GITLAB_", "BITBUCKET_",
    "SLACK_", "JENKINS_", "VAULT_", "KUBERNETES_", "KUBE_", "MYSQL_",
    "POSTGRES_", "MARIADB_", "REDIS_", "MONGO_", "STRIPE_", "TWILIO_",
    "SENDGRID_", "DOCKER_AUTH", "NPM_", "PYPI_", "HUGGINGFACE_", "HF_",
    "CI_JOB_", "JWT_", "PRIVATE_", "SECRET_",
)

#: Env var name suffixes treated as credentials (checked against the
#: upper-cased name; the leading underscore avoids matching e.g. MONKEY).
CREDENTIAL_ENV_SUFFIXES = (
    "_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_PASSWD", "_CREDENTIAL",
    "_CREDENTIALS", "_API_KEY", "_ACCESS_KEY", "_PRIVATE_KEY", "_AUTH",
    "_AUTHORIZATION", "_SESSION", "_SIGNATURE",
)

#: Exact env var names always treated as credentials.
CREDENTIAL_ENV_KEYS = frozenset({
    "TOKEN", "API_KEY", "API_TOKEN", "ACCESS_KEY", "SECRET", "SECRET_KEY",
    "PASSWORD", "PASSWD", "CREDENTIALS", "PRIVATE_KEY", "AUTHORIZATION",
    "AUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "GITREINS_LLM_API_KEY",
    "GITREINS_LLM_BASE_URL", "DOCKER_AUTH_CONFIG",
})


class SandboxError(Exception):
    """A sandbox operation failed (infrastructure, not the command itself)."""


class SandboxUnavailableError(SandboxError):
    """Docker is missing or the daemon is down — the sandbox cannot run.

    Graceful degradation: PR service mode can fall back to host execution
    or report a clear, actionable error.
    """


class SandboxTimeoutError(SandboxError):
    """A sandboxed command exceeded its wall-clock timeout.

    The ephemeral container was force-removed before this was raised.
    """


@dataclass
class SandboxResult:
    """Outcome of one sandboxed command: stdout, stderr, exit code."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class Sandbox(ABC):
    """Ephemeral execution environment for untrusted code (PR service mode).

    A sandbox owns a read-only view of the PR checkout (``cwd`` is the
    container-side path) and executes commands inside the ephemeral
    environment with no production credentials, no host mounts beyond the
    checkout, no docker socket, default-deny network, and resource caps —
    DESIGN_v2.md §9 ⚠️.

    ``run`` returns stdout/stderr/exit_code; infrastructure failures raise
    :class:`SandboxError` (missing docker/daemon →
    :class:`SandboxUnavailableError`, wall-clock limit →
    :class:`SandboxTimeoutError`).
    """

    #: Container-side working directory (the mounted PR checkout).
    cwd: str

    @abstractmethod
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> SandboxResult:
        """Run ``command`` in the sandbox; return stdout/stderr/exit_code."""

    def close(self) -> None:
        """Release resources. No-op by default (per-run ``--rm`` containers)."""

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ── Subprocess plumbing (module-level for hermetic monkeypatching) ───────


def _run_subprocess(cmd, *, timeout: float, cwd: str | None = None):
    """Run ``cmd`` capturing output; module-level so tests monkeypatch it."""
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
    )


def _run(cmd, *, timeout: float, cwd: str | None = None):
    """Run a host-side setup command (gh clone/checkout); raise on failure."""
    try:
        result = _run_subprocess(cmd, timeout=timeout, cwd=cwd)
    except FileNotFoundError as exc:
        raise SandboxError(f"binary not found ({cmd[0]!r}): {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"{' '.join(cmd[:2])} timed out after {timeout}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SandboxError(
            f"{' '.join(cmd[:2])} failed (rc={result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result


# ── Credential scrubbing ─────────────────────────────────────────────────


def _is_credential_key(name: str) -> bool:
    """True when ``name`` could carry a credential (exact/prefix/suffix)."""
    upper = name.upper()
    if upper in CREDENTIAL_ENV_KEYS:
        return True
    if any(upper.startswith(p) for p in CREDENTIAL_ENV_PREFIXES):
        return True
    if any(upper.endswith(s) for s in CREDENTIAL_ENV_SUFFIXES):
        return True
    return False


def scrub_env(env: dict[str, str] | None) -> dict[str, str]:
    """Return ``env`` minus every key that could carry a credential.

    Applies the exact blocklist, credential prefixes (GITHUB_TOKEN,
    GITREINS_LLM_*, OPENAI_*, AWS_*, …) and credential suffixes
    (_TOKEN/_KEY/_SECRET/_PASSWORD/_API_KEY/…). Sandboxed processes must
    never see production credentials (DESIGN_v2.md §9 ⚠️).
    """
    if not env:
        return {}
    return {k: v for k, v in env.items() if not _is_credential_key(k)}


def _deny_host_mount(path: str, label: str) -> None:
    """Refuse to mount a denied host path (/, /home, the docker socket, …)."""
    for denied in DENIED_MOUNT_PATHS:
        if path == denied:
            raise SandboxError(
                f"refusing to mount {label} {path!r}: {denied!r} is never "
                "mounted into the sandbox (DESIGN_v2.md §9)"
            )


# ── PR checkout cloning (host side, for the read-only mount) ─────────────


def clone_pr_checkout(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    dest: str | None = None,
    gh_binary: str = "gh",
    timeout: float = 120.0,
) -> str:
    """Materialize a PR head checkout on the host for the sandbox to mount.

    ``gh repo clone`` then ``gh pr checkout <n>`` (detached HEAD at the PR
    head), mirroring checkout.py's gh-subprocess style. The clone happens on
    the HOST because gh needs host credentials — the sandbox only ever sees
    the resulting checkout directory, mounted read-only.

    Args:
        owner/repo/pr_number: The pull request to check out.
        dest: Target directory; defaults to a fresh temp dir (the caller
            owns cleanup — or use :meth:`DockerSandbox.from_pull_request`,
            which cleans up after itself).
        gh_binary: gh CLI path (defaults to ``gh`` on PATH).
        timeout: Per-command wall-clock limit.

    Returns:
        The checkout directory path.

    Raises:
        SandboxError: gh missing, unauthenticated, or the clone/checkout
            failed — never a raw stack trace.
    """
    dest = dest or tempfile.mkdtemp(prefix=f"gitreins-pr-{owner}-{repo}-")
    repo_arg = f"{owner}/{repo}"
    _run([gh_binary, "repo", "clone", repo_arg, dest, "--", "--quiet"], timeout=timeout)
    _run(
        [gh_binary, "pr", "checkout", str(pr_number), "-R", repo_arg],
        timeout=timeout,
        cwd=dest,
    )
    return dest


# ── DockerSandbox ────────────────────────────────────────────────────────


class DockerSandbox(Sandbox):
    """Ephemeral container sandbox via the docker CLI (subprocess, no deps).

    One ``docker run --rm`` per :meth:`run`, carrying the full DESIGN_v2.md
    §9 security posture: default-deny network, resource caps, read-only PR
    checkout mount, read-only rootfs + tmpfs scratch, scrubbed env only, and
    ``--cidfile``-tracked teardown so the container is removed even when the
    command times out or the daemon errors mid-run.
    """

    def __init__(
        self,
        checkout_dir: str,
        image: str = DEFAULT_IMAGE,
        *,
        network: str = "none",
        cpus: float = DEFAULT_CPU_LIMIT,
        memory: str = DEFAULT_MEMORY_LIMIT,
        pids_limit: int = DEFAULT_PIDS_LIMIT,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        checkout_mount: str = DEFAULT_CHECKOUT_MOUNT,
        scratch_dir: str | None = None,
        scratch_mount: str = DEFAULT_SCRATCH_MOUNT,
        tmpfs_size: str = DEFAULT_TMPFS_SIZE,
        read_only_rootfs: bool = True,
        env_allowlist: Iterable[str] = frozenset(),
        docker_binary: str = "docker",
        check_available: bool = True,
        cleanup_checkout: bool = False,
    ):
        """``checkout_dir`` is the host-side PR checkout, mounted read-only
        at ``checkout_mount`` (refused if it is ``/``, ``/home`` or the
        docker socket). ``scratch_dir`` (optional) is mounted writable at
        ``scratch_mount``. ``env_allowlist`` selects which HOST env keys may
        enter the container (default: none); credential patterns are scrubbed
        regardless. ``check_available`` probes the docker daemon on the first
        run (set False in tests / when the probe is unwanted).
        """
        self.checkout_dir = os.path.abspath(checkout_dir)
        _deny_host_mount(self.checkout_dir, "checkout_dir")
        if not checkout_mount.startswith("/"):
            raise SandboxError(
                f"checkout_mount must be an absolute container path, got {checkout_mount!r}"
            )
        self.image = image
        self.network = network
        self.cpus = float(cpus)
        self.memory = memory
        self.pids_limit = int(pids_limit)
        self.command_timeout = float(command_timeout)
        self.checkout_mount = checkout_mount
        self.scratch_dir = None if scratch_dir is None else os.path.abspath(scratch_dir)
        if self.scratch_dir is not None:
            _deny_host_mount(self.scratch_dir, "scratch_dir")
        self.scratch_mount = scratch_mount
        self.tmpfs_size = tmpfs_size
        self.read_only_rootfs = read_only_rootfs
        self.env_allowlist = frozenset(env_allowlist)
        self.docker_binary = docker_binary
        self._docker_checked = not check_available
        self._cleanup_checkout = cleanup_checkout
        self.cwd = self.checkout_mount

    @classmethod
    def from_pull_request(
        cls,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        dest: str | None = None,
        gh_binary: str = "gh",
        clone_timeout: float = 120.0,
        **kwargs,
    ) -> "DockerSandbox":
        """Build a sandbox over a freshly cloned PR head checkout.

        The clone runs on the HOST (gh needs host credentials); the sandbox
        receives only the minimal read-only checkout mount. When ``dest`` is
        None the temp checkout is removed by :meth:`close`.
        """
        checkout_dir = clone_pr_checkout(
            owner, repo, pr_number, dest=dest, gh_binary=gh_binary, timeout=clone_timeout
        )
        return cls(checkout_dir=checkout_dir, cleanup_checkout=dest is None, **kwargs)

    # ── Availability ──────────────────────────────────────

    def _ensure_docker(self) -> None:
        """Probe docker once; raise SandboxUnavailableError when missing."""
        if self._docker_checked:
            return
        if shutil.which(self.docker_binary) is None:
            raise SandboxUnavailableError(
                f"docker binary {self.docker_binary!r} not found on PATH — "
                "install docker or run without a sandbox (local mode)"
            )
        try:
            probe = _run_subprocess(
                [self.docker_binary, "version", "--format", "{{.Server.Version}}"],
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError(f"docker unavailable: {exc}") from exc
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip()
            raise SandboxUnavailableError(
                f"docker daemon unavailable: {detail or 'docker version failed'}"
            )
        self._docker_checked = True

    # ── Environment ───────────────────────────────────────

    def _effective_env(self, env: dict[str, str] | None) -> dict[str, str]:
        """Allowlisted host env + explicit env, then scrubbed.

        Scrub runs LAST so credential patterns win even when a caller
        explicitly allowlists or passes them — never pass GITHUB_TOKEN /
        GITREINS_LLM_* into the container (DESIGN_v2.md §9 ⚠️).
        """
        merged: dict[str, str] = {}
        if self.env_allowlist:
            for key in self.env_allowlist:
                if key in os.environ:
                    merged[key] = os.environ[key]
        if env:
            merged.update(env)
        return scrub_env(merged)

    # ── Command building ──────────────────────────────────

    def _build_run_args(
        self, command: str, cwd: str, env: dict[str, str]
    ) -> list[str]:
        """The ``docker run`` argv for one sandboxed command (minus cidfile).

        Security posture (DESIGN_v2.md §9 ⚠️): ``--network none``
        default-deny, resource caps (cpus/memory/pids), read-only rootfs with
        tmpfs /tmp, the PR checkout mounted read-only, no docker socket, and
        only scrubbed env vars. ``run()`` appends ``--cidfile`` and relies on
        ``--rm`` for teardown.
        """
        args = [
            self.docker_binary,
            "run",
            "--rm",
            "--network",
            self.network,
            "--cpus",
            str(self.cpus),
            "--memory",
            self.memory,
            "--pids-limit",
            str(self.pids_limit),
            "--workdir",
            cwd,
            "--tmpfs",
            f"/tmp:size={self.tmpfs_size}",
        ]
        if self.read_only_rootfs:
            args.append("--read-only")
        args += ["-v", f"{self.checkout_dir}:{self.checkout_mount}:ro"]
        if self.scratch_dir is not None:
            args += ["-v", f"{self.scratch_dir}:{self.scratch_mount}"]
        for key in sorted(env):
            args += ["--env", f"{key}={env[key]}"]
        args += [self.image, "/bin/sh", "-c", command]
        return args

    # ── Teardown ──────────────────────────────────────────

    @staticmethod
    def _new_cidfile() -> str:
        """Path for docker's ``--cidfile`` (records the container id)."""
        fd, path = tempfile.mkstemp(prefix="gitreins-sandbox-", suffix=".cid")
        os.close(fd)
        return path

    def _force_remove(self, cidfile: str) -> None:
        """Best-effort teardown: ``docker rm -f`` the cidfile container.

        Called on timeout/error so an ephemeral container never outlives its
        sandbox run, even when the run failed.
        """
        cid = ""
        try:
            with open(cidfile, encoding="utf-8") as f:
                cid = f.read().strip()
        except OSError:
            pass
        if cid:
            try:
                _run_subprocess([self.docker_binary, "rm", "-f", cid], timeout=30)
            except Exception:  # noqa: BLE001 — teardown is best-effort
                logger.warning("failed to force-remove sandbox container %s", cid)
        try:
            os.unlink(cidfile)
        except OSError:
            pass

    def close(self) -> None:
        """Release resources.

        Per-run ``--rm`` containers mean there is nothing to remove; if this
        sandbox cloned its own checkout (:meth:`from_pull_request` without
        ``dest``), remove that temp dir.
        """
        if self._cleanup_checkout:
            shutil.rmtree(self.checkout_dir, ignore_errors=True)

    # ── The run surface ───────────────────────────────────

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> SandboxResult:
        """Run ``command`` inside an ephemeral container; return its output.

        One ``docker run --rm`` per call with the full §9 posture (see
        :meth:`_build_run_args`). The container id is recorded via
        ``--cidfile``; on success ``--rm`` removes the container and on
        timeout/error :meth:`_force_remove` guarantees teardown.

        Args:
            command: Shell command to run inside the container.
            cwd: Container working directory (default: the checkout mount).
            env: Extra env vars — scrubbed, so credentials are never passed.
            timeout: Wall-clock limit (default: ``command_timeout``).

        Raises:
            SandboxUnavailableError: docker missing or daemon down.
            SandboxTimeoutError: the command exceeded its timeout (container
                force-removed).
            SandboxError: other sandbox infrastructure failures.
        """
        self._ensure_docker()
        cwd = cwd or self.cwd
        effective_env = self._effective_env(env)
        timeout = self.command_timeout if timeout is None else timeout
        args = self._build_run_args(command, cwd, effective_env)

        try:
            cidfile = self._new_cidfile()
        except OSError as exc:
            raise SandboxError(f"failed to create cidfile: {exc}") from exc

        try:
            proc = _run_subprocess([*args, "--cidfile", cidfile], timeout=timeout)
        except subprocess.TimeoutExpired:
            self._force_remove(cidfile)
            raise SandboxTimeoutError(
                f"command timed out after {timeout}s — container force-removed"
            ) from None
        except FileNotFoundError as exc:
            raise SandboxUnavailableError(
                f"docker binary {self.docker_binary!r} not found: {exc}"
            ) from exc
        except OSError as exc:
            self._force_remove(cidfile)
            raise SandboxError(f"failed to run docker: {exc}") from exc

        if proc.returncode == 125 and "daemon" in (proc.stderr or "").lower():
            self._force_remove(cidfile)
            detail = (proc.stderr or proc.stdout).strip()
            raise SandboxUnavailableError(detail or "docker daemon error (rc=125)")

        try:
            os.unlink(cidfile)  # --rm already removed the container
        except OSError:
            pass
        return SandboxResult(
            stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode
        )
