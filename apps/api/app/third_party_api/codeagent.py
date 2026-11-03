"""Opt-in Claude-compatible CodeAgent CLI; proposals never execute Skill code."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.third_party_api.codeagent_process import WindowsJob
from app.third_party_api.skill_model import (
    AdapterError,
    StructuredSkillModel,
    _strict_json,
    model_prompt,
)


class CodeAgentModel(StructuredSkillModel):
    """Run stage-scoped CLI calls with one trusted submission tool and isolated settings."""

    integration_revision = "codeagent-submit-v4"

    def __init__(
        self,
        executable: str,
        run_root: Path,
        *,
        enabled: bool = False,
        model: str | None = None,
        effort: str | None = None,
        timeout: float = 300,
        max_output_bytes: int = 10_000_000,
        env_names: tuple[str, ...] = (),
        script_path: Path | None = None,
    ) -> None:
        """Bind only trusted deployment configuration, never request-level commands."""
        if not math.isfinite(timeout) or timeout <= 0 or max_output_bytes <= 0:
            raise ValueError("Invalid CodeAgent bounds")
        if effort not in (None, "low", "medium", "high"):
            raise ValueError("Invalid CodeAgent effort")
        forbidden = {"HOME", "PATH", "PWD", "USERPROFILE", "TMPDIR", "TMP", "TEMP"}
        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name.upper() in forbidden
            for name in env_names
        ):
            raise ValueError("Invalid CodeAgent environment name")
        resolved = shutil.which(executable)
        if not resolved or Path(resolved).suffix.lower() in {".bat", ".cmd"}:
            raise ValueError("CodeAgent requires an installed native executable")
        if script_path is not None and (not script_path.is_absolute() or not script_path.is_file()):
            raise ValueError("CodeAgent entry script must be an existing absolute path")
        self.executable = str(Path(resolved).resolve())
        self.run_root = run_root.absolute()
        self.enabled = enabled
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.env_names = env_names
        self.script_path = script_path
        self.last_call: dict[str, Any] | None = None
        # Resume identity includes the transport binary and the configured endpoint.
        identity = [
            self.integration_revision,
            hashlib.sha256(Path(resolved).read_bytes()).hexdigest(),
        ]
        if script_path:
            identity.append(hashlib.sha256(script_path.read_bytes()).hexdigest())
        identity.append(effort or "default")
        self.integration_revision = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.base_url = os.environ.get("ANTHROPIC_BASE_URL")

    async def _generate(self, operation: str, data: dict[str, Any]) -> Any:
        """Request a structured result; persist bounded metadata without provider logs."""
        if not self.enabled:
            raise AdapterError("CodeAgent is disabled")
        from app.service.evolution.skill_safety import reject_sensitive_payload

        reject_sensitive_payload(data)
        from app.third_party_api.skill_submission import TOOLS, submission_input

        name, submission_type = TOOLS[operation]
        tool_name = "mcp__skill__" + name
        schema_json = json.dumps(submission_type.model_json_schema(), ensure_ascii=False)
        prompt = (
            model_prompt(operation, name)
            + "\n"
            + json.dumps(submission_input(operation, data), ensure_ascii=False, allow_nan=False)
        )
        # Keep business input out of Windows argv length limits and process listings.
        payload = prompt.encode("utf-8")
        if len(payload) > 8_000_000:
            raise AdapterError("CodeAgent input exceeds size limit")
        self.run_root.mkdir(parents=True, exist_ok=True)
        if self.run_root.is_symlink() or self.run_root.resolve() != self.run_root:
            raise AdapterError("CodeAgent artifact root must not be a symlink")
        call_id = uuid.uuid4().hex
        cwd = self.run_root / call_id
        cwd.mkdir(mode=0o700)
        (cwd / "input.json").write_text(
            json.dumps(
                {
                    "operation": operation,
                    "data": data,
                    "prompt_file": "prompt.txt",
                    "response_schema_file": "response-schema.json",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # Preserve the actual stdin bytes, including LF on Windows, for local diagnosis.
        (cwd / "prompt.txt").write_bytes(payload)
        (cwd / "response-schema.json").write_text(schema_json, encoding="utf-8")
        server = Path(__file__).with_name("submission_server.py").resolve()
        config = {
            "mcpServers": {
                "skill": {"command": sys.executable, "args": ["-I", str(server), str(cwd)]}
            }
        }
        config_path = cwd / "mcp.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        args = [
            self.executable,
            *([str(self.script_path)] if self.script_path else []),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--restricted",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--strict-mcp-config",
            "--mcp-config",
            str(config_path),
            "--allowedTools",
            tool_name,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        env = {
            "PATH": os.defpath,
            "HOME": str(cwd),
            "USERPROFILE": str(cwd),
            "TMPDIR": str(cwd),
            "TEMP": str(cwd),
            "TMP": str(cwd),
            "PYTHONIOENCODING": "utf-8",
        }
        env.update({k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ})
        env.update({k: os.environ[k] for k in self.env_names if k in os.environ})
        process: asyncio.subprocess.Process | None = None
        job = WindowsJob()
        started = time.monotonic()
        metadata: dict[str, Any] = {
            "call_id": call_id,
            "operation": operation,
            "backend": "codeagent",
            "model": self.model,
            "integration_revision": self.integration_revision,
            "status": "failed",
        }
        self.last_call = metadata
        try:
            options: dict[str, Any] = {"start_new_session": True}
            if sys.platform == "win32":
                options = {"creationflags": subprocess.CREATE_NO_WINDOW | 0x00000004}
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **options,
            )
            if sys.platform == "win32":
                job.attach(process.pid)
                job.resume(process.pid)
            output = await self._exchange(process, payload)
            metadata["exit_code"] = process.returncode
            events = [_strict_json(line) for line in output.splitlines() if line.strip()]
            results = [e for e in events if isinstance(e, dict) and e.get("type") == "result"]
            if len(results) == 1:
                subtype = results[0].get("subtype")
                if isinstance(subtype, str) and re.fullmatch(r"[a-z_]{1,80}", subtype):
                    metadata["result_subtype"] = subtype
            if process.returncode != 0:
                raise AdapterError("CodeAgent exited unsuccessfully")
            if (
                len(results) != 1
                or results[0].get("is_error")
                or results[0].get("subtype") != "success"
            ):
                raise AdapterError("CodeAgent did not complete successfully")
            if (cwd / "unsafe").exists():
                raise AdapterError("Unsafe submission")
            attempts = sorted(cwd.glob("submission-*.json"))
            metadata["submissions"] = len(attempts)
            if not (cwd / "accepted.json").exists():
                detail = (
                    json.loads(attempts[-1].read_text(encoding="utf-8")).get(
                        "error", "invalid submission"
                    )
                    if attempts
                    else "tool not called"
                )
                raise AdapterError(
                    f"No accepted submission after {len(attempts)} attempts: {detail}"
                )
            result = {"result": json.loads((cwd / "accepted.json").read_text(encoding="utf-8"))}
            reject_sensitive_payload(result)
            serialized = json.dumps(result, ensure_ascii=False)
            for name in self.env_names:
                secret = env.get(name, "")
                if (
                    any(word in name.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
                    and secret
                    and secret in serialized
                ):
                    raise AdapterError("CodeAgent result contains a credential")
            usage = results[0].get("usage", {})
            metadata.update(
                status="succeeded",
                usage={
                    key: value
                    for key, value in usage.items()
                    if key
                    in {
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    }
                    and type(value) is int
                    and value >= 0
                }
                if isinstance(usage, dict)
                else {},
            )
            (cwd / "result.json").write_text(serialized, encoding="utf-8")
            return result["result"]
        except TimeoutError:
            raise AdapterError("CodeAgent timed out") from None
        except (OSError, UnicodeError):
            raise AdapterError("CodeAgent execution failed") from None
        except asyncio.CancelledError:
            metadata["status"] = "cancelled"
            raise
        finally:
            job.close()
            if process is not None:
                if sys.platform != "win32":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
                await process.wait()
            # Keep only the checked result artifact, including on transport failure.
            (cwd / "accepted.json").unlink(missing_ok=True)
            metadata["elapsed_seconds"] = round(time.monotonic() - started, 3)
            (cwd / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    async def _exchange(self, process: asyncio.subprocess.Process, payload: bytes) -> str:
        """Bound both output streams and clean up reader tasks on timeout or cancellation."""
        size = 0

        async def read(stream: asyncio.StreamReader | None, keep: bool) -> bytes:
            nonlocal size
            if stream is None:
                raise AdapterError("CodeAgent output pipe is unavailable")
            chunks = []
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > self.max_output_bytes:
                    raise AdapterError("CodeAgent output exceeds size limit")
                if keep:
                    chunks.append(chunk)
            return b"".join(chunks)

        async def send() -> bytes:
            if process.stdin is None:
                raise AdapterError("CodeAgent input pipe is unavailable")
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            await process.wait()
            return b""

        tasks = [
            asyncio.create_task(read(process.stdout, True)),
            asyncio.create_task(read(process.stderr, False)),
            asyncio.create_task(send()),
        ]
        try:
            async with asyncio.timeout(self.timeout):
                parts = await asyncio.gather(*tasks)
            return parts[0].decode("utf-8")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def codeagent_from_environment() -> CodeAgentModel:
    """Resolve the explicitly enabled CLI from deployment variables, never task input."""
    return CodeAgentModel(
        executable=os.environ.get("SKILL_EVOLUTION_CODEAGENT_CMD", "codeagent"),
        run_root=Path(os.environ.get("SKILL_EVOLUTION_CODEAGENT_CWD", "data/codeagent")),
        enabled=os.environ.get("SKILL_EVOLUTION_MODEL_ENABLED") == "1"
        and os.environ.get("SKILL_EVOLUTION_CODEAGENT_ENABLED") == "1",
        model=os.environ.get("SKILL_EVOLUTION_CODEAGENT_MODEL") or None,
        effort=os.environ.get("SKILL_EVOLUTION_CODEAGENT_EFFORT") or None,
        timeout=float(os.environ.get("SKILL_EVOLUTION_CODEAGENT_TIMEOUT", "300")),
        max_output_bytes=int(os.environ.get("SKILL_EVOLUTION_CODEAGENT_MAX_LOG_BYTES", "10000000")),
        env_names=tuple(
            name.strip()
            for name in os.environ.get("SKILL_EVOLUTION_CODEAGENT_ENV_NAMES", "").split(",")
            if name.strip()
        ),
    )
