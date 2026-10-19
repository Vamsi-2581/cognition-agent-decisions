"""F027 P2: Bridge abstraction strategies.

Two approaches for generating abstract bridge-definitions:
1. Rule-based: Strip specifics (numbers, paths, names) and generalize.
2. LLM-assisted: Use Gemini Flash for genuine abstraction.

Both take a RecordDecisionRequest and return a BridgeDefinition.
"""

import logging
import os
import re
from typing import Any

from .decision_service import BridgeDefinition, RecordDecisionRequest

logger = logging.getLogger(__name__)

# Environment config
BRIDGE_MODE = os.getenv("BRIDGE_MODE", "rule")  # rule | llm | both
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_BRIDGE_MODEL", "gemini-2.0-flash")


# ── Rule-Based Abstractor ──────────────────────────────────────────────

# Patterns to strip from text for abstraction
_STRIP_PATTERNS = [
    (r"PR #?\d+", "a PR"),
    (r"#\d+", ""),
    (r"\b\d+(\.\d+)?\s*(s|ms|seconds|minutes|hours)\b", "N time-units"),
    (r"\b\d+(\.\d+)?\s*(MB|GB|KB|bytes)\b", "N size-units"),
    (r"\bv?\d+\.\d+(\.\d+)?\b", "vX.Y"),
    (r"\b\d{4}-\d{2}-\d{2}\b", "DATE"),
    (r"\b\d+\b", "N"),
    # File paths and module names. The lookbehind stops the engine from retrying
    # at every character inside a long path-like run: `[a-z_]+/[a-z_/]+\.[a-z]+`
    # rescanned the rest of the run from each start, quadratic in its length.
    (r"(?<![a-z_/])/?[a-z_]+(?:/[a-z_]+)+\.[a-z]+", "a file"),
    (r"`[^`]+`", "a component"),
    # Specific project names (common patterns)
    (r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", "a component"),  # CamelCase
]

# Generalization mappings for common operational verbs
_VERB_GENERALIZATIONS = {
    "increased": "adjusted",
    "decreased": "adjusted",
    "changed": "modified",
    "switched": "replaced",
    "migrated": "transitioned",
    "upgraded": "updated",
    "downgraded": "reverted",
    "fixed": "corrected",
    "patched": "corrected",
    "deployed": "released",
    "shipped": "released",
    "merged": "integrated",
    "added": "introduced",
    "removed": "eliminated",
    "deleted": "eliminated",
    "refactored": "restructured",
    "extracted": "separated",
    "moved": "relocated",
    "renamed": "relabeled",
}


def _strip_specifics(text: str) -> str:
    """Strip specific details from text, leaving abstract structure."""
    result = text
    for pattern, replacement in _STRIP_PATTERNS:
        result = re.sub(pattern, replacement, result)
    # Clean up repeated spaces and artifacts
    result = re.sub(r"\s+", " ", result).strip()
    # Remove empty parens/brackets
    result = re.sub(r"\(\s*\)|\[\s*\]", "", result).strip()
    return result


def _generalize_verbs(text: str) -> str:
    """Replace specific verbs with general ones."""
    words = text.split()
    result = []
    for word in words:
        lower = word.lower().rstrip(".,;:!?")
        if lower in _VERB_GENERALIZATIONS:
            # Preserve original casing style
            replacement = _VERB_GENERALIZATIONS[lower]
            if word[0].isupper():
                replacement = replacement.capitalize()
            # Preserve trailing punctuation
            trailing = word[len(lower):]
            result.append(replacement + trailing)
        else:
            result.append(word)
    return " ".join(result)


def rule_based_bridge(request: RecordDecisionRequest) -> BridgeDefinition | None:
    """Generate abstract bridge using rule-based stripping.

    Strategy:
    - Structure: abstract the decision text (what was done)
    - Function: abstract the pattern field, or best reason, or context
    """
    # Structure: from decision text or pattern
    structure = ""
    if request.decision:
        abstracted = _generalize_verbs(_strip_specifics(request.decision))
        if len(abstracted) > 10:
            structure = abstracted

    # Function: prefer pattern field (already abstract), then reasons
    function = ""
    if request.pattern:
        function = request.pattern  # Pattern is already abstract
    elif request.reasons:
        # Pick best reason for function
        for r in request.reasons:
            if r.type in ("analysis", "constraint", "pattern"):
                candidate = _strip_specifics(r.text)
                if len(candidate) > 10:
                    function = _generalize_verbs(candidate)
                    break
    if not function and request.context:
        # Fall back to abstracting first sentence of context
        sentences = re.split(r"[.!]\s+", request.context)
        if sentences:
            candidate = _strip_specifics(sentences[0])
            if len(candidate) > 10:
                function = _generalize_verbs(candidate)

    if structure or function:
        return BridgeDefinition(
            structure=structure or function,
            function=function or structure,
        )
    return None


# ── LLM Abstractor ─────────────────────────────────────────────────────

_LLM_PROMPT = """Given this decision record, generate an abstract bridge-definition.

Decision: {decision}
Context: {context}
Reasons: {reasons}
Pattern: {pattern}

Generate TWO fields:
1. STRUCTURE: What does this decision look like as an abstract pattern? Strip all specific names, numbers, and project details. Describe the recognizable form.
2. FUNCTION: What problem does this abstract pattern solve? Why would someone use this approach?

Keep each to 1-2 sentences. Be abstract - this should match similar decisions across different projects.

Reply in this exact format:
STRUCTURE: <your answer>
FUNCTION: <your answer>"""


async def llm_bridge(request: RecordDecisionRequest) -> BridgeDefinition | None:
    """Generate abstract bridge using Gemini Flash.

    Makes a single API call to generate structure and function descriptions.
    Returns None on any error (LLM bridge is best-effort).
    """
    if not GEMINI_API_KEY:
        logger.debug("No GEMINI_API_KEY set, skipping LLM bridge")
        return None

    reasons_text = " | ".join(
        f"{r.type}: {r.text}" for r in (request.reasons or [])
    )

    prompt = _LLM_PROMPT.format(
        decision=request.decision or "",
        context=request.context or "",
        reasons=reasons_text or "none",
        pattern=request.pattern or "none",
    )

    try:
        import httpx

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{GEMINI_MODEL}:generateContent"
        )
        headers = {"x-goog-api-key": GEMINI_API_KEY}
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 256,
            },
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Parse response
        candidates = data.get("candidates")
        if not candidates:
            logger.debug("LLM returned no candidates (safety filter?)")
            return None
        text = (
            candidates[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )

        structure = ""
        function = ""
        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("STRUCTURE:"):
                structure = line[len("STRUCTURE:"):].strip()
            elif line.upper().startswith("FUNCTION:"):
                function = line[len("FUNCTION:"):].strip()

        if structure or function:
            return BridgeDefinition(
                structure=structure or function,
                function=function or structure,
            )

    except Exception:
        logger.debug("LLM bridge extraction failed", exc_info=True)

    return None


# ── Unified Entry Point ─────────────────────────────────────────────────

async def smart_extract_bridge(
    request: RecordDecisionRequest,
    mode: str | None = None,
) -> tuple[BridgeDefinition | None, str]:
    """Extract bridge using configured strategy.

    Args:
        request: The decision being recorded.
        mode: Override BRIDGE_MODE env var (rule | llm | both).

    Returns:
        Tuple of (bridge, method_used) where method_used is
        "rule", "llm", "both", or "none".
    """
    effective_mode = mode or BRIDGE_MODE

    if effective_mode == "llm":
        bridge = await llm_bridge(request)
        return (bridge, "llm") if bridge else (None, "none")

    if effective_mode == "both":
        llm_result = await llm_bridge(request)
        rule_result = rule_based_bridge(request)

        if llm_result:
            # LLM is primary; log rule-based for comparison
            if rule_result:
                logger.info(
                    "Bridge comparison - LLM: %s | Rule: %s",
                    llm_result.structure[:80],
                    rule_result.structure[:80],
                )
            return (llm_result, "both")
        if rule_result:
            return (rule_result, "rule")
        return (None, "none")

    # Default: rule-based
    bridge = rule_based_bridge(request)
    return (bridge, "rule") if bridge else (None, "none")
