"""Running model-written code.

This is process-level isolation only: a subprocess with a scrubbed environment (no API keys),
CPU/memory/file-size limits and a timeout. It is NOT a security boundary. Run sweeps inside a
container or VM if you are worried about what the models might execute.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

MAX_OUTPUT = 6000


def sandbox_env(workspace: Path, home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(workspace),
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LANG": "C.UTF-8",
    }


def set_limits(timeout: float):
    def _apply():
        try:
            import resource

            cpu = int(timeout) + 2
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            mem = 2 * 1024**3
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            fsize = 20 * 1024**2
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        except Exception:
            pass
        os.setsid()

    return _apply


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} chars truncated] ...\n" + text[-half:]


async def run_python(code: str, workspace: Path, timeout: float = 20) -> str:
    """Run a snippet with the workspace as cwd and on sys.path. Returns combined output."""
    with tempfile.TemporaryDirectory(prefix="orgbench-run-") as tmp:
        script = Path(tmp) / f"snippet_{uuid.uuid4().hex[:8]}.py"
        script.write_text(code)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            cwd=workspace,
            env=sandbox_env(workspace, Path(tmp)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=set_limits(timeout),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                proc.kill()
            await proc.wait()
            return f"[timed out after {timeout:.0f}s]"
        text = out.decode(errors="replace")
        return truncate(text) + f"\n[exit code {proc.returncode}]"
