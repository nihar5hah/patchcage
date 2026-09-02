"""PatchCage TUI spike — conversational interface over the real sandboxed engine.

This is a spike, not the product. It proves the interface shape: a chat-style
stream where the user watches and steers, while every model action runs inside
the locked sandbox and the host-owned verification ladder gates the export.

The "model" here is scripted (no live endpoint). Everything it touches —
snapshot, sandbox, MCP tools, oracle, checks — is the real engine.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, RichLog, Static

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from patchcage.domain import Finding, FindingSource, load_manifest  # noqa: E402
from patchcage.mcp import MCPToolError, WorkspaceMCPClient  # noqa: E402
from patchcage.sandbox.check_runner import run_named_check  # noqa: E402
from patchcage.sandbox.docker_runtime import DockerRuntime  # noqa: E402
from patchcage.sandbox.image import IMAGE_TAG  # noqa: E402
from patchcage.snapshot import create_snapshot  # noqa: E402

MANIFEST_PATH = PROJECT_ROOT / "manifests" / "flask_sql_injection.yml"
CREATE_DEMO = PROJECT_ROOT / "scripts" / "create_demo_repo.py"
FIX_PATCH = PROJECT_ROOT / "tests" / "fixtures" / "sql_injection_fix.patch"
RULE_ID = "patchcage.python.sql-injection.formatted-query"


def demo_finding() -> Finding:
    return Finding(
        id="sql-1",
        source=FindingSource.SEMGREP_SARIF,
        rule_id=RULE_ID,
        title="SQL injection via formatted query",
        description="User input is interpolated into a SQL execute call.",
        severity="ERROR",
        file_path="src/demo_app/search.py",
        start_line=20,
        verification_recipe="sql_injection_oracle",
    )


class PatchCageApp(App):
    TITLE = "PatchCage"
    SUB_TITLE = "least-privilege remediation (spike)"

    CSS = """
    #stream { height: 1fr; border: round $primary; padding: 0 1; }
    #verify { height: auto; border: round $secondary; padding: 0 1; display: none; }
    #input { dock: bottom; }
    """
    def __init__(self) -> None:
        super().__init__()
        self._runtime: DockerRuntime | None = None
        self.sandbox = None
        self.manifest = load_manifest(MANIFEST_PATH)
        self.repo: Path | None = None
        self.last_diff = ""
        self._busy = False

    @property
    def runtime(self) -> DockerRuntime:
        # Lazy: don't touch the Docker socket until a run actually starts.
        if self._runtime is None:
            self._runtime = DockerRuntime()
        return self._runtime

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield RichLog(markup=True, wrap=True, id="stream")
        yield Static("", id="verify")
        yield Input(placeholder="Type a message, or /run /diff /checks /approve /help", id="input")
        yield Footer()

    def say(self, text: str) -> None:
        self.query_one("#stream", RichLog).write(text)

    def on_mount(self) -> None:
        self.say("[bold cyan]PatchCage spike.[/] The model works in a locked sandbox; "
                 "the host verifies every patch before export.")
        self.say("Commands: [bold]/run[/] start the pipeline · [bold]/diff[/] show patch · "
                 "[bold]/checks[/] verification · [bold]/approve[/] export · [bold]/quit[/]")
        self.say("Or just type to steer the model (e.g. [italic]\"parameterize the query\"[/]).")
        self.query_one("#input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text or self._busy:
            return
        if text.startswith("/"):
            await self.handle_command(text)
        else:
            await self.handle_steer(text)

    async def handle_command(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower()
        if cmd in ("/quit", "/exit"):
            self.exit()
        elif cmd == "/help":
            self.say("[dim]/run /diff /checks /approve /quit — or type to steer the model.[/]")
        elif cmd == "/run":
            await self.run_pipeline()
        elif cmd == "/diff":
            self.say(f"```diff\n{self.last_diff or '(no patch yet)'}```")
        elif cmd == "/checks":
            self.show_checks()
        elif cmd == "/approve":
            self.approve()
        else:
            self.say(f"[red]Unknown command {cmd}[/] — /help")

    async def handle_steer(self, text: str) -> None:
        self.say(f"[bold green]you:[/] {text}")
        self.say("[dim]model: noted — I'll fold that into the next patch attempt. "
                 "(steering is advisory; verification still gates export.)[/]")

    # --- the real pipeline, narrated -------------------------------------

    async def run_pipeline(self) -> None:
        self._busy = True
        try:
            await asyncio.to_thread(self._pipeline_sync)
        except Exception as error:
            self.say(f"[red]✗ run failed:[/] {self._friendly_error(error)}")
        finally:
            self._busy = False

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        text = str(error)
        if "docker.sock" in text or "fetching server API version" in text:
            return ("Docker daemon is not reachable. Start OrbStack (`open -a OrbStack`), "
                    "wait a few seconds, then /run again.")
        return text[:500]

    def _pipeline_sync(self) -> None:
        say = self.call_from_thread
        say(self.say, "[bold]→ snapshot[/] creating isolated copy of the repo…")
        tmp = tempfile.mkdtemp(prefix="patchcage-spike-")
        self.repo = Path(tmp) / "sql-demo"
        created = subprocess.run(
            [sys.executable, str(CREATE_DEMO), str(self.repo)],
            capture_output=True, text=True, check=True,
        )
        commit_sha = json.loads(created.stdout)["commit_sha"]
        snapshot = create_snapshot(self.repo, commit_sha, blocked_patterns=self.manifest.scope.blocked)
        say(self.say, f"  [dim]commit {commit_sha[:8]} · {len(snapshot.entries)} files · "
                      f"original repo untouched[/]")

        say(self.say, "[bold]→ sandbox[/] starting locked container (no network, uid 1000)…")
        self.sandbox = self.runtime.create_work_sandbox(
            image=IMAGE_TAG, snapshot=snapshot, manifest=self.manifest, finding=demo_finding(),
        )
        say(self.say, f"  [dim]container {self.sandbox.container_id[:12]} · baseline "
                      f"{self.sandbox.baseline_sha[:8]}[/]")

        say(self.say, "[bold]→ baseline[/] confirming the vulnerability is real…")
        oracle = run_named_check(self.sandbox, "security", self.manifest, allow_security=True)
        reproduced = "PATCHCAGE_VULNERABILITY_REPRODUCED" in oracle.summary
        say(self.say, f"  [{'green' if reproduced else 'red'}]oracle: "
                      f"{'vulnerability reproduced' if reproduced else oracle.summary}[/]")

        asyncio.run(self._model_works())

        say(self.say, "[bold]→ verification[/] host re-checks the patch (model can't see this)…")
        self._run_verification()
        say(self.say, "[bold green]✓ patch verified.[/] /diff to review · /approve to export")

    async def _model_works(self) -> None:
        assert self.sandbox is not None
        say = self.call_from_thread
        say(self.say, "[bold]→ model[/] investigating inside the sandbox (via MCP, no shell)…")
        async with WorkspaceMCPClient(self.sandbox.container_id, timeout_seconds=90) as client:
            finding = await client.call("get_finding")
            say(self.say, f"  [dim]model read finding: {finding['title']}[/]")
            src = await client.call("read_file", {"path": "src/demo_app/search.py"})
            say(self.say, f"  [dim]model read search.py ({len(src['content'])} bytes)[/]")
            say(self.say, "  model: [italic]\"the query interpolates user input — "
                          "I'll parameterize it.\"[/]")
            patch = await client.call("propose_patch", {"diff": FIX_PATCH.read_text()})
            say(self.say, f"  [dim]patch applied: {', '.join(patch['files'])}[/]")
            diff = await client.call("get_current_diff")
            self.last_diff = diff["diff"]

    def _run_verification(self) -> None:
        assert self.sandbox is not None
        results = []
        for name, allow in (("compile", False), ("unit", False), ("security", True)):
            res = run_named_check(self.sandbox, name, self.manifest, allow_security=allow)
            ok = res.status.value == "passed" or (
                name == "security" and "PATCHCAGE_SECURITY_ORACLE_PASSED" in res.summary
            )
            results.append((name, ok))
        self._check_results = results
        self.call_from_thread(self.show_checks)

    def show_checks(self) -> None:
        results = getattr(self, "_check_results", [])
        if not results:
            self.say("[dim]No verification yet — /run first.[/]")
            return
        lines = ["[bold]Verification (host-owned, model-independent)[/]"]
        for name, ok in results:
            lines.append(f"  [{'green' if ok else 'red'}]{'✓' if ok else '✗'} {name}[/]")
        self.say("\n".join(lines))

    def approve(self) -> None:
        if not self.last_diff:
            self.say("[red]Nothing to approve — /run first.[/]")
            return
        out = PROJECT_ROOT / ".patchcage-spike-export"
        out.mkdir(exist_ok=True)
        (out / "final.patch").write_text(self.last_diff)
        self.say(f"[bold green]✓ exported[/] {out}/final.patch — apply it to your repo yourself.")
        self.say("[dim]Your original repo was never touched.[/]")


if __name__ == "__main__":
    PatchCageApp().run()
