"""Validate /v2 contract responses against the filtered contract document (06.2 RESEARCH F2).

``v2-contract-openapi.json`` beside this module is ``scripts/filter-deploy-openapi.py`` applied to
the committed 06.1 spliced spec, the same document the dashboard's generated client comes from.
The suite reads it from here, not from ``.planning/``, so a milestone cleanup cannot remove it.
``CONTRACT_DOCUMENT_SHA256`` pins its exact bytes.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

CONTRACT_DOCUMENT_PATH = Path(__file__).resolve().parent / "v2-contract-openapi.json"
CONTRACT_DOCUMENT_SHA256 = "da6d0c955a005f3c87b7f1fe008714b22cd342c29e13108d9a66e63daca2de82"


@cache
def load_contract_document() -> dict[str, Any]:
    """The filtered contract document, parsed once per process."""
    return json.loads(CONTRACT_DOCUMENT_PATH.read_text(encoding="utf-8"))


def validate_component(name: str, payload: Any) -> None:
    """Fail the calling test unless ``payload`` is a valid ``#/components/schemas/<name>``.

    Every validation error is reported, each with the JSON path it occurred at.
    """
    document = load_contract_document()
    schemas = document["components"]["schemas"]
    assert name in schemas, f"{name} is not a component schema of the contract document"
    validator = Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": document["components"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    errors = sorted(validator.iter_errors(payload), key=lambda error: error.json_path)
    if errors:
        details = "\n".join(f"  {error.json_path}: {error.message}" for error in errors)
        raise AssertionError(f"response is not a valid {name}:\n{details}")
