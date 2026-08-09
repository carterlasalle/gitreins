"""
R2.11 tests for engine/github/sandbox — the PR service-mode execution
sandbox (DESIGN_v2.md §9 ⚠️).

Hermetic (no docker required): command building (--network none, --rm,
--cpus/--memory/--pids-limit caps, read-only checkout mount, tmpfs /tmp,
--read-only rootfs, no docker-socket/root mounts), credential env scrubbing
(GITHUB_TOKEN / GITREINS_LLM_* / OPENAI_* / *_TOKEN / *_API_KEY dropped
from scrub_env AND from the docker run --env flags even when explicitly
passed or allowlisted), denied host mounts (/, /home, docker socket),
graceful SandboxUnavailableError (binary missing, daemon down, rc=125
mid-run), teardown-on-error (TimeoutExpired → docker rm -f the cidfile
container), clone_pr_checkout (gh repo clone + gh pr checkout sequence),
and verifier-in-sandbox routing: VerifierAgent(sandbox=FakeSandbox) runs
run_command through the sandbox — never host subprocess — while the host
path stays intact when sandbox is None.

Plus one real-docker smoke test marked skipif(no docker/busybox) — it
skips gracefully on CI, and exercises an actual ephemeral container here.
"""

import os
import shutil
import subprocess

import pytest

import engine.github.sandbox as sandbox_mod
from engine.github.sandbox import (
    DENIED_MOUNT_PATHS,
    DockerSandbox,
    Sandbox,
    SandboxError,
    SandboxResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
    clone_pr_checkout,
    scrub_env,
)
from engine.llm import LLMResponse, ToolCall
from engine.review import VerifierAgent, VerifierCandidate, VerifierFindings

# The DESIGN_v2.md §12-shaped CONFIRMED verdict (from tests/test_review_verifier.py).
CONFIRMED_JSON = (
    '{"findings": [{"finding_id": "F19", "impact": "high", '
    '"patch_causality": "confirmed", "reproducible": true, '
    '"execution_path_confirmed": true, "verifier_confidence": 0.94, '
    '"developer_relevance": "high", "verdict": "CONFIRMED", '
    '"notes": "Claim survived falsification."}], '
    '"summary": "F19 survived falsification."}'
)


class StubLLM:
    """Deterministic fake LLMClient: plays back responses, records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages_seen = []
        self.tools_seen = []

    def chat(self, messages, tools=None, max_tokens=16384, temperature=0.1):
        self.calls += 1
        self.messages_seen.append(list(messages))
        self.tools_seen.append(tools or [])
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return resp() if callable(resp) else resp


class FakeRouter:
    """Minimal ModelRouter stand-in returning one fixed client for any role."""

    def __init__(self, client):
        self.client = client
        self.roles = []

    def for_role(self, role: str) -> StubLLM:
        self.roles.append(role)
        return self.client


def content_response(text):
    return LLMResponse(content=text)


def make_candidate():
    """The DESIGN_v2.md §9 candidate finding (F19)."""
    return VerifierCandidate(
        finding_id="F19",
        file="auth/session.py",
        line=182,
        claim="Deleted users can reach this dereference with user=None.",
        trigger="refresh token belonging to deleted account",
        evidence=["E12", "E33", "E52"],
        verification_plan=["inspect caller guard", "run targeted regression test"],
    )


class FakeSandbox:
    """Recorded-run Sandbox stand-in — no docker required.

    ``cwd`` mirrors DockerSandbox: the container-side checkout path.
    """

    cwd = "/work"

    def __init__(self, result=None, error=None):
        self.result = result or SandboxResult(stdout="hi from sandbox", exit_code=0)
        self.error = error
        self.runs = []
        self.closed = False

    def run(self, command, *, cwd=None, env=None, timeout=None):
        self.runs.append(
            {"command": command, "cwd": cwd, "env": env, "timeout": timeout}
        )
        if self.error is not None:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


def make_sandbox(tmp_path, check_available=False, **kwargs):
    """A DockerSandbox over a temp checkout, without the docker probe."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    return DockerSandbox(str(checkout), check_available=check_available, **kwargs)


def run_args(sb, command="echo hi", cwd="/work", env=None):
    """The docker run argv DockerSandbox would execute for ``command``."""
    return sb._build_run_args(command, cwd, env if env is not None else {})


def mount_specs(args):
    """The values of every ``-v`` flag in a docker argv (in order)."""
    return [args[i + 1] for i, a in enumerate(args) if a == "-v"]


def env_flags(args):
    """The values of every ``--env`` flag in a docker argv (in order)."""
    return [args[i + 1] for i, a in enumerate(args) if a == "--env"]


# ── Sandbox protocol + result shapes ─────────────────────────────────────


class TestSandboxProtocol:
    def test_sandbox_is_abstract(self):
        with pytest.raises(TypeError):
            Sandbox()

    def test_sandbox_result_defaults(self):
        r = SandboxResult()
        assert r.stdout == "" and r.stderr == "" and r.exit_code == 0

    def test_docker_sandbox_cwd_is_checkout_mount(self, tmp_path):
        sb = make_sandbox(tmp_path)
        assert sb.cwd == "/work"
        assert sb.checkout_mount == "/work"


# ── Command building (docker run flags) ──────────────────────────────────


class TestCommandBuilding:
    def test_network_default_deny(self, tmp_path):
        sb = make_sandbox(tmp_path)
        args = run_args(sb)
        assert "--network" in args
        assert args[args.index("--network") + 1] == "none"

    def test_rm_and_resource_caps(self, tmp_path):
        sb = make_sandbox(tmp_path)
        args = run_args(sb)
        assert "--rm" in args
        for flag, value in (
            ("--cpus", "2.0"),
            ("--memory", "2g"),
            ("--pids-limit", "512"),
        ):
            assert flag in args
            assert args[args.index(flag) + 1] == value

    def test_checkout_mounted_read_only(self, tmp_path):
        sb = make_sandbox(tmp_path)
        args = run_args(sb)
        assert mount_specs(args) == [f"{sb.checkout_dir}:/work:ro"]

    def test_read_only_rootfs_and_tmpfs_scratch(self, tmp_path):
        sb = make_sandbox(tmp_path)
        args = run_args(sb)
        assert "--read-only" in args
        assert "--tmpfs" in args
        assert args[args.index("--tmpfs") + 1] == "/tmp:size=128m"

    def test_scratch_mount_added_when_configured(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        sb = make_sandbox(tmp_path, scratch_dir=str(scratch))
        args = run_args(sb)
        assert f"{scratch}:/scratch" in mount_specs(args)

    def test_no_docker_socket_or_denied_root_mounts(self, tmp_path):
        """The only -v mount is the read-only checkout — never /, /home,
        or the docker socket."""
        sb = make_sandbox(tmp_path)
        args = run_args(sb)
        mounts = mount_specs(args)
        assert len(mounts) == 1
        for m in mounts:
            host_part = m.split(":", 1)[0]
            assert host_part not in ("/", "/home", "/var/run/docker.sock")
            assert "/var/run/docker.sock" not in m
            assert m.endswith(":ro")

    def test_command_runs_via_shell_in_container(self, tmp_path):
        sb = make_sandbox(tmp_path)
        args = run_args(sb, command="pytest -x tests/")
        assert args[-4:] == [sb.image, "/bin/sh", "-c", "pytest -x tests/"]


# ── Credential env scrubbing ─────────────────────────────────────────────


class TestEnvScrub:
    def test_scrub_env_drops_credential_keys(self):
        env = {
            "GITHUB_TOKEN": "t",
            "GH_TOKEN": "t",
            "GITREINS_LLM_API_KEY": "k",
            "GITREINS_LLM_BASE_URL": "u",
            "OPENAI_API_KEY": "k",
            "AWS_SECRET_ACCESS_KEY": "a",
            "MYAPP_TOKEN": "t",
            "DB_PASSWORD": "p",
            "FOO": "bar",
            "PATH": "/usr/bin",
        }
        out = scrub_env(env)
        assert out["FOO"] == "bar" and out["PATH"] == "/usr/bin"
        for key in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GITREINS_LLM_API_KEY",
            "GITREINS_LLM_BASE_URL",
            "OPENAI_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "MYAPP_TOKEN",
            "DB_PASSWORD",
        ):
            assert key not in out

    def test_scrub_is_case_insensitive(self):
        out = scrub_env({"api_key": "x", "Github_Token": "x", "safe": "y"})
        assert out == {"safe": "y"}

    def test_scrub_keeps_benign_names(self):
        out = scrub_env(
            {"MONKEY": "1", "GITREINS_JOB_DIR": "/tmp/jobs", "LANG": "C.UTF-8"}
        )
        assert set(out) == {"MONKEY", "GITREINS_JOB_DIR", "LANG"}

    def test_scrub_none_is_empty(self):
        assert scrub_env(None) == {}

    def test_allowlist_plus_explicit_env_scrubbed(self, tmp_path):
        """Credential patterns win even when explicitly allowlisted."""
        sb = make_sandbox(tmp_path, env_allowlist={"GITHUB_TOKEN", "LANG"})
        eff = sb._effective_env({"GITHUB_TOKEN": "t", "LANG": "C.UTF-8"})
        assert eff == {"LANG": "C.UTF-8"}

    def test_allowlist_reads_host_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_FOO", "hostval")
        sb = make_sandbox(tmp_path, env_allowlist={"SANDBOX_FOO", "MISSING_VAR"})
        assert sb._effective_env(None) == {"SANDBOX_FOO": "hostval"}

    def test_no_allowlist_passes_no_host_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "leak")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "leak")
        monkeypatch.setenv("PATH", "/usr/bin")
        sb = make_sandbox(tmp_path)  # env_allowlist default: nothing
        assert sb._effective_env(None) == {}

    def test_run_passes_only_scrubbed_env_flags(self, tmp_path, monkeypatch):
        """End-to-end: the docker argv carries no credential --env flags."""
        sb = make_sandbox(tmp_path, env_allowlist={"LANG"})
        calls = []

        def fake_run(cmd, *, timeout, cwd=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="out", stderr="")

        monkeypatch.setattr(sandbox_mod, "_run_subprocess", fake_run)
        result = sb.run(
            "echo hi", env={"LANG": "C.UTF-8", "GITHUB_TOKEN": "secret"}
        )
        assert result.exit_code == 0 and result.stdout == "out"
        assert len(calls) == 1
        run_cmd = calls[0]
        assert run_cmd[1] == "run"
        flags = env_flags(run_cmd)
        assert flags == ["LANG=C.UTF-8"]
        assert not any("GITHUB_TOKEN" in f for f in flags)
        assert "--cidfile" in run_cmd


# ── Denied host mounts ───────────────────────────────────────────────────


class TestDeniedMounts:
    @pytest.mark.parametrize(
        "path", ["/", "/home", "/root", "/var/run/docker.sock", "/run/docker.sock"]
    )
    def test_refuses_denied_checkout(self, path):
        with pytest.raises(SandboxError, match="refusing to mount"):
            DockerSandbox(path, check_available=False)

    @pytest.mark.parametrize(
        "path", ["/", "/home", "/var/run/docker.sock", "/run/docker.sock"]
    )
    def test_refuses_denied_scratch(self, tmp_path, path):
        with pytest.raises(SandboxError, match="refusing to mount"):
            DockerSandbox(
                str(tmp_path / "co"), scratch_dir=path, check_available=False
            )

    def test_denied_paths_constant_covered(self):
        assert "/" in DENIED_MOUNT_PATHS
        assert "/home" in DENIED_MOUNT_PATHS
        assert "/var/run/docker.sock" in DENIED_MOUNT_PATHS

    def test_specific_subdir_under_home_is_allowed(self):
        """The rule is never to mount the / or /home ROOTS — a specific PR
        checkout dir under them is the minimal read-only mount §9 allows."""
        sb = DockerSandbox(
            os.path.join("/home", "alice", "work", "repo"), check_available=False
        )
        assert sb.checkout_dir == "/home/alice/work/repo"

    def test_non_absolute_checkout_mount_rejected(self, tmp_path):
        with pytest.raises(SandboxError, match="absolute"):
            make_sandbox(tmp_path, checkout_mount="work")


# ── Graceful unavailability ──────────────────────────────────────────────


class TestUnavailable:
    def test_missing_docker_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sandbox_mod.shutil, "which", lambda name: None)
        sb = DockerSandbox(str(tmp_path / "co"), check_available=True)
        with pytest.raises(SandboxUnavailableError) as ei:
            sb.run("echo hi")
        assert "not found" in str(ei.value)

    def test_daemon_down_probe_fails(self, tmp_path, monkeypatch):
        def fake_run(cmd, *, timeout, cwd=None):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Cannot connect to the Docker daemon"
            )

        monkeypatch.setattr(sandbox_mod, "_run_subprocess", fake_run)
        sb = DockerSandbox(str(tmp_path / "co"), check_available=True)
        with pytest.raises(SandboxUnavailableError) as ei:
            sb.run("echo hi")
        assert "daemon" in str(ei.value).lower()

    def test_daemon_error_mid_run_rc125(self, tmp_path, monkeypatch):
        calls = []

        def fake_run(cmd, *, timeout, cwd=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 125, stdout="", stderr="docker: Cannot connect to the Docker daemon"
            )

        monkeypatch.setattr(sandbox_mod, "_run_subprocess", fake_run)
        sb = make_sandbox(tmp_path)  # probe skipped
        with pytest.raises(SandboxUnavailableError):
            sb.run("echo hi")
        assert len(calls) == 1  # only the run attempt — no rm without a cid

    def test_command_exit_code_passes_through(self, tmp_path, monkeypatch):
        def fake_run(cmd, *, timeout, cwd=None):
            return subprocess.CompletedProcess(cmd, 42, stdout="o", stderr="e")

        monkeypatch.setattr(sandbox_mod, "_run_subprocess", fake_run)
        sb = make_sandbox(tmp_path)
        result = sb.run("exit 42")
        assert result.exit_code == 42
        assert result.stdout == "o" and result.stderr == "e"


# ── Teardown on error ────────────────────────────────────────────────────


class TestTeardownOnError:
    def test_timeout_force_removes_container(self, tmp_path, monkeypatch):
        cidfile = tmp_path / "run.cid"
        cidfile.write_text("abc123def")

        def fake_mkstemp(*args, **kwargs):
            fd = os.open(str(cidfile), os.O_CREAT | os.O_RDWR)
            return (fd, str(cidfile))

        monkeypatch.setattr(sandbox_mod.tempfile, "mkstemp", fake_mkstemp)
        calls = []

        def fake_run(cmd, *, timeout, cwd=None):
            calls.append(cmd)
            if cmd[1] == "run":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(sandbox_mod, "_run_subprocess", fake_run)
        sb = make_sandbox(tmp_path)
        with pytest.raises(SandboxTimeoutError) as ei:
            sb.run("sleep 100")
        assert "force-removed" in str(ei.value)
        rm_calls = [c for c in calls if c[:2] == ["docker", "rm"]]
        assert rm_calls and rm_calls[0][2:] == ["-f", "abc123def"]
        assert not cidfile.exists()  # cidfile cleaned up after teardown

    def test_error_path_cleans_cidfile(self, tmp_path, monkeypatch):
        cidfile = tmp_path / "run.cid"
        cidfile.write_text("")

        def fake_mkstemp(*args, **kwargs):
            fd = os.open(str(cidfile), os.O_CREAT | os.O_RDWR)
            return (fd, str(cidfile))

        monkeypatch.setattr(sandbox_mod.tempfile, "mkstemp", fake_mkstemp)

        def fake_run(cmd, *, timeout, cwd=None):
            raise OSError("boom")

        monkeypatch.setattr(sandbox_mod, "_run_subprocess", fake_run)
        sb = make_sandbox(tmp_path)
        with pytest.raises(SandboxError, match="failed to run docker"):
            sb.run("echo hi")
        assert not cidfile.exists()

    def test_sandbox_context_manager_closes(self, tmp_path):
        sb = make_sandbox(tmp_path)
        with sb as entered:
            assert entered is sb
        # close() is a no-op for non-cloned checkouts — still callable
        sb.close()


# ── PR checkout cloning (host side) ──────────────────────────────────────


class TestClonePrCheckout:
    def test_clones_then_checks_out_pr(self, tmp_path, monkeypatch):
        dest = str(tmp_path / "pr")
        calls = []

        def fake_run(cmd, *, timeout, cwd=None):
            calls.append((cmd, cwd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(sandbox_mod, "_run_subprocess", fake_run)
        out = clone_pr_checkout("acme", "widgets", 42, dest=dest)
        assert out == dest
        clone_cmd, clone_cwd = calls[0]
        assert clone_cmd[:3] == ["gh", "repo", "clone"]
        assert "acme/widgets" in clone_cmd
        checkout_cmd, checkout_cwd = calls[1]
        assert checkout_cmd[:3] == ["gh", "pr", "checkout"]
        assert "42" in checkout_cmd
        assert checkout_cwd == dest  # gh pr checkout runs inside the clone

    def test_clone_failure_raises_sandbox_error(self, monkeypatch):
        def fake_run(cmd, *, timeout, cwd=None):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="gh: not authenticated"
            )

        monkeypatch.setattr(sandbox_mod, "_run_subprocess", fake_run)
        with pytest.raises(SandboxError, match="not authenticated"):
            clone_pr_checkout("acme", "widgets", 42, dest="/tmp/never")

    def test_from_pull_request_owns_its_temp_checkout(self, tmp_path, monkeypatch):
        def fake_clone(owner, repo, pr_number, *, dest=None, gh_binary="gh", timeout=120.0):
            path = dest or str(tmp_path / "auto-pr")
            os.makedirs(path, exist_ok=True)
            return path

        monkeypatch.setattr(sandbox_mod, "clone_pr_checkout", fake_clone)
        explicit = DockerSandbox.from_pull_request("acme", "widgets", 42, dest=str(tmp_path / "pr"))
        assert explicit.checkout_dir == str(tmp_path / "pr")
        assert explicit._cleanup_checkout is False

        auto = DockerSandbox.from_pull_request("acme", "widgets", 42)
        assert auto._cleanup_checkout is True
        assert os.path.isdir(auto.checkout_dir)
        auto.close()
        assert not os.path.exists(auto.checkout_dir)  # close() removed it


# ── Verifier-in-sandbox routing (R2.11) ──────────────────────────────────


class TestVerifierInSandbox:
    def test_run_command_routed_through_sandbox(self, tmp_workdir, monkeypatch):
        fake = FakeSandbox()
        stub = StubLLM(
            [
                LLMResponse(
                    content="running",
                    tool_calls=[
                        ToolCall(id="t0", name="run_command", arguments={"cmd": "pytest -x"})
                    ],
                ),
                content_response(CONFIRMED_JSON),
            ]
        )
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir, sandbox=fake)
        result = agent.run(make_candidate())
        assert isinstance(result, VerifierFindings)
        assert result.findings[0].verdict == "CONFIRMED"
        assert stub.calls == 2  # tool turn + final answer
        assert len(fake.runs) == 1
        run = fake.runs[0]
        assert run["command"] == "pytest -x"
        assert run["cwd"] == "/work"  # container-side path, not the host workdir
        assert run["timeout"] == 30  # VerifierAgent default command_timeout
        assert agent.exec_sandbox is fake

    def test_sandboxed_run_never_touches_host_subprocess(self, tmp_workdir, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("host subprocess must not run in sandbox mode")

        monkeypatch.setattr("engine.review.verifier.subprocess.run", boom)
        fake = FakeSandbox()
        stub = StubLLM(
            [
                LLMResponse(
                    content="running",
                    tool_calls=[
                        ToolCall(id="t0", name="run_command", arguments={"cmd": "echo hi"})
                    ],
                ),
                content_response(CONFIRMED_JSON),
            ]
        )
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir, sandbox=fake)
        agent.run(make_candidate())  # would raise if host subprocess were used
        assert fake.runs[0]["command"] == "echo hi"

    def test_sandbox_error_maps_to_tool_error(self, tmp_workdir):
        from engine.review.verifier import _make_run_command_tool

        fake = FakeSandbox(error=SandboxUnavailableError("docker daemon unavailable"))
        tool = _make_run_command_tool(tmp_workdir, sandbox=fake)
        out = tool.fn(cmd="echo hi")
        assert "docker daemon unavailable" in out["error"]
        assert "exit_code" not in out

    def test_sandbox_timeout_maps_to_tool_error(self, tmp_workdir):
        from engine.review.verifier import _make_run_command_tool

        fake = FakeSandbox(error=SandboxTimeoutError("timed out"))
        tool = _make_run_command_tool(tmp_workdir, sandbox=fake)
        out = tool.fn(cmd="sleep 100")
        assert "timed out" in out["error"]

    def test_host_path_unchanged_without_sandbox(self, tmp_workdir):
        """Local mode (sandbox=None) still runs run_command on the host."""
        from engine.review.verifier import _make_run_command_tool

        tool = _make_run_command_tool(tmp_workdir)
        out = tool.fn(cmd="echo hello")
        assert out["exit_code"] == 0
        assert "hello" in out["output"]

    def test_agent_without_sandbox_keeps_host_default(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        agent.run(make_candidate())
        assert agent.exec_sandbox is None
        # AgentRunner.sandbox (the scratch-state property) is untouched:
        assert agent.sandbox == {}

    def test_sandboxed_run_still_injects_scratch_tools(self, tmp_workdir):
        fake = FakeSandbox()
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir, sandbox=fake)
        agent.run(make_candidate())
        names = {t["function"]["name"] for t in stub.tools_seen[0]}
        assert {"run_command", "read_file", "search_pattern"} <= names
        assert {"sandbox_write", "sandbox_read"} <= names


# ── Real-docker smoke (skips gracefully when docker is unavailable) ──────

SMOKE_IMAGE = "busybox:latest"


def _smoke_ready() -> bool:
    """True when docker + a shell-capable image are usable on this host."""
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "version"], capture_output=True, text=True, timeout=10
        )
        if probe.returncode != 0:
            return False
    except Exception:  # noqa: BLE001 — any failure means "skip"
        return False
    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", SMOKE_IMAGE],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if inspect.returncode != 0:
            pull = subprocess.run(
                ["docker", "pull", SMOKE_IMAGE],
                capture_output=True,
                text=True,
                timeout=60,
            )
            return pull.returncode == 0
        return True
    except Exception:  # noqa: BLE001 — any failure means "skip"
        return False


_SMOKE_REASON = f"docker daemon or {SMOKE_IMAGE} unavailable — skipping real-container smoke test"


class TestDockerSmoke:
    @pytest.mark.skipif(not _smoke_ready(), reason=_SMOKE_REASON)
    def test_ephemeral_container_run(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "hello.txt").write_text("sandbox says hi")
        sb = DockerSandbox(str(checkout))
        try:
            ok = sb.run("echo hello")
            assert ok.exit_code == 0
            assert "hello" in ok.stdout

            cat = sb.run("cat /work/hello.txt")
            assert cat.exit_code == 0
            assert "sandbox says hi" in cat.stdout  # checkout is mounted

            fail = sb.run("exit 3")
            assert fail.exit_code == 3  # container exit code passes through

            # the docker socket is NOT exposed to the sandboxed process
            sock = sb.run("ls /var/run/docker.sock 2>&1 || true")
            assert "No such file" in sock.stdout + sock.stderr

            # read-only rootfs, writable tmpfs /tmp
            tmp_write = sb.run("touch /tmp/ok.txt && echo writable")
            assert tmp_write.exit_code == 0
            ro = sb.run("touch /etc/blocked.txt 2>&1 || true")
            assert "Read-only" in ro.stdout + ro.stderr
        finally:
            sb.close()

    @pytest.mark.skipif(not _smoke_ready(), reason=_SMOKE_REASON)
    def test_env_scrub_in_real_container(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        sb = DockerSandbox(str(checkout), env_allowlist={"SANDBOX_OK"})
        try:
            res = sb.run(
                "echo SANDBOX_OK=$SANDBOX_OK GITHUB_TOKEN=${GITHUB_TOKEN:-unset}",
                env={"SANDBOX_OK": "yes", "GITHUB_TOKEN": "super-secret"},
            )
            assert res.exit_code == 0
            assert "SANDBOX_OK=yes" in res.stdout
            assert "GITHUB_TOKEN=unset" in res.stdout  # credential never injected
            assert "super-secret" not in res.stdout + res.stderr
        finally:
            sb.close()
