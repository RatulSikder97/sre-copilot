"""Safety primitives: secret redaction, untrusted-output fencing and namespace allowlisting."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

# Ordered: the more specific patterns run first so generic ones do not partially mask them.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        REDACTED,
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), REDACTED),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (
        re.compile(r"(?i)\b([a-z]+://[^\s:/@]+):([^\s@/]+)@"),
        r"\1:" + REDACTED + "@",
    ),
    (
        re.compile(
            r"(?i)(\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|credentials?)[\w.-]*\b[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}&]+)"
        ),
        r"\1" + REDACTED,
    ),
]


def redact(text: str) -> str:
    """Mask credentials and tokens before text is logged, stored or shown to the model."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
)


def redact_obj(value: Any) -> Any:
    """Recursively redact every string inside a JSON-like structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: REDACTED
            if isinstance(k, str) and _SECRET_KEY_RE.search(k) and isinstance(v, str)
            else redact_obj(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value


_FENCE_RE = re.compile(r"</?\s*tool_output[^>]*>", re.IGNORECASE)


def fence_untrusted(tool: str, evidence_id: str, body: str, *, max_chars: int) -> str:
    """Wrap tool output in a delimited, explicitly untrusted envelope.

    Any embedded fence tags are neutralised so content cannot close the envelope early and
    smuggle instructions outside it. Output is truncated to ``max_chars``.
    """
    body = _FENCE_RE.sub("[fence-tag-removed]", body)
    if len(body) > max_chars:
        omitted = len(body) - max_chars
        body = body[:max_chars] + f"\n...[truncated {omitted} chars]"
    return (
        f'<tool_output tool="{tool}" evidence_id="{evidence_id}" trust="untrusted">\n'
        f"{body}\n"
        "</tool_output>"
    )


class NamespaceNotAllowedError(PermissionError):
    pass


def check_namespace(namespace: str, allowed: Iterable[str]) -> str:
    allowed_set = set(allowed)
    if namespace not in allowed_set:
        raise NamespaceNotAllowedError(
            f"namespace {namespace!r} is not in the allowlist {sorted(allowed_set)}"
        )
    return namespace


_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_LABEL_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_./\-=!,() ]{0,256}$")


def check_k8s_name(name: str, what: str = "name") -> str:
    if not _K8S_NAME_RE.match(name):
        raise ValueError(f"invalid Kubernetes {what}: {name!r}")
    return name


def check_label_selector(selector: str) -> str:
    if not _LABEL_SELECTOR_RE.match(selector):
        raise ValueError(f"invalid label selector: {selector!r}")
    return selector
