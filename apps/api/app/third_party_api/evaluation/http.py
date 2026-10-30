"""Opt-in HTTP implementation of the task_template/task evaluation protocol."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit

import httpx

from app.third_party_api.evaluation.models import (
    CaseScore,
    EvaluationError,
    InvalidEvaluationEvidence,
    parse_case_score,
)

EvaluationStatus = Literal["queued", "running", "completed", "failed"]


class HttpEvaluationClient:
    """Submit branch-based jobs without automatic POST retries or redirects."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Bind explicit non-secret configuration and an optional test transport."""
        self._enabled = config.get("enabled") is True
        base_url = config.get("baseUrl", "")
        if not isinstance(base_url, str):
            raise EvaluationError("Evaluation base URL is invalid")
        parsed = urlsplit(base_url)
        schemes = {"https", "http"} if config.get("allowHttp") is True else {"https"}
        if (
            parsed.scheme not in schemes
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise EvaluationError("Evaluation base URL requires HTTPS without credentials")
        self._base_url = base_url.rstrip("/")
        token_env = config.get("tokenEnv", "EVALUATION_TOKEN")
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise EvaluationError("Evaluation token environment variable name is invalid")
        self._token_env = token_env
        timeout = config.get("timeoutSeconds", 30.0)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise EvaluationError("Evaluation timeout must be finite and positive")
        self._timeout = float(timeout)
        expected = config.get("expectedCaseIds")
        if expected is not None and (
            not isinstance(expected, (list, tuple))
            or not expected
            or any(not isinstance(name, str) or not name.strip() for name in expected)
            or len(set(expected)) != len(expected)
        ):
            raise EvaluationError("Expected evaluation case IDs are invalid")
        self._expected = tuple(sorted(expected)) if expected is not None else None
        self._client = client

    @property
    def integration_identity(self) -> str:
        """Bind durable provider IDs to a non-secret endpoint/protocol identity."""
        return hashlib.sha256(
            json.dumps(["case-mean-v1", self._base_url, self._expected]).encode()
        ).hexdigest()

    def validate_submission(self, ref: str, template_id: str, idempotency_key: str) -> None:
        """Reject local configuration failures before recording a remote POST intent."""
        _branch(ref)
        if not isinstance(template_id, str) or not template_id.strip():
            raise EvaluationError("Evaluation template ID is required")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise EvaluationError("Evaluation idempotency key is required")
        if not self._enabled:
            raise EvaluationError("Real evaluation adapter is disabled")
        if not os.environ.get(self._token_env):
            raise EvaluationError("Evaluation token environment variable is not set")

    async def submit(self, ref: str, template_id: str, idempotency_key: str) -> str:
        """Send one POST, without assuming the provider honors idempotency keys."""
        self.validate_submission(ref, template_id, idempotency_key)
        branch = _branch(ref)
        body = await self._request(
            "POST",
            "/trigger/task_template",
            payload={"template_id": template_id, "params": {"commit": branch}},
            idempotency_key=idempotency_key,
        )
        run_id = body.get("task_id")
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 512:
            raise EvaluationError("Evaluation trigger returned an invalid task ID")
        self._safe_identifier(run_id)
        return run_id

    async def get_status(self, eval_id: str) -> EvaluationStatus:
        """Read the provider state without treating malformed or unknown states as done."""
        body = await self._task(eval_id)
        raw = body.get("status")
        statuses = {
            "queued": "queued",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
            "crashed": "failed",
            "cancelled": "failed",
        }
        status = statuses.get(raw.casefold()) if isinstance(raw, str) else None
        if status is None:
            raise EvaluationError("Evaluation returned an invalid status")
        return cast(EvaluationStatus, status)

    async def get_score(self, eval_id: str) -> CaseScore:
        """Read complete case evidence; scalar/global scores cannot replace it."""
        result = parse_case_score(await self._task(eval_id))
        for name in result.case_ids:
            self._safe_identifier(name)
        self._safe_identifier(json.dumps(result.report, ensure_ascii=False))
        if self._expected is not None and result.case_ids != self._expected:
            raise InvalidEvaluationEvidence(
                "Evaluation case set differs from configured expected cases"
            )
        return result

    def _safe_identifier(self, value: str) -> None:
        token = os.environ.get(self._token_env)
        candidates = [token, token.removeprefix("Bearer ")] if token else []
        if any(secret and secret in value for secret in candidates):
            raise InvalidEvaluationEvidence(
                "Evaluation identifier contains sensitive provider data"
            )

    async def _task(self, eval_id: str) -> dict[str, Any]:
        if (
            not isinstance(eval_id, str)
            or not eval_id.strip()
            or eval_id in (".", "..")
            or len(eval_id) > 512
        ):
            raise EvaluationError("Evaluation task ID is invalid")
        return await self._request("GET", "/task/" + quote(eval_id, safe=""))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not self._enabled:
            raise EvaluationError("Real evaluation adapter is disabled")
        token = os.environ.get(self._token_env)
        if not token:
            raise EvaluationError("Evaluation token environment variable is not set")
        headers = {"Authorization": token, "Accept": "application/json"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            if self._client is None:
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.request(
                        method,
                        self._base_url + path,
                        headers=headers,
                        json=payload,
                        timeout=self._timeout,
                        follow_redirects=False,
                    )
            else:
                response = await self._client.request(
                    method,
                    self._base_url + path,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                    follow_redirects=False,
                )
        except httpx.HTTPError:
            raise EvaluationError("Evaluation HTTP request failed") from None
        if not 200 <= response.status_code < 300:
            raise EvaluationError(f"Evaluation HTTP status {response.status_code}")
        if len(response.content) > 2 * 1024 * 1024:
            raise EvaluationError("Evaluation response exceeds size limit")
        try:
            body = json.loads(
                response.text, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant
            )
        except (ValueError, UnicodeError, RecursionError):
            raise EvaluationError("Evaluation returned invalid JSON") from None
        if not isinstance(body, dict):
            raise EvaluationError("Evaluation response must be an object")
        return body


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def _branch(ref: str) -> str:
    if not isinstance(ref, str):
        raise EvaluationError("Evaluation requires a Git branch")
    branch = ref.removeprefix("refs/heads/")
    if (
        not branch
        or branch.startswith(("refs/", "-", "/"))
        or branch.endswith(("/", "."))
        or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", branch)
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in branch)
        or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        or any(char in branch for char in "~^:?*[\\")
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or branch == "@"
    ):
        raise EvaluationError("Evaluation requires a valid Git branch, not a tag or commit")
    return branch
