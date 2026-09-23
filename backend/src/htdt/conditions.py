from __future__ import annotations

from typing import Any


def _speaker_map(value: Any) -> dict[str, Any] | Any:
    if not isinstance(value, list):
        return value
    mapped: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, dict) or 'speaker_id' not in item:
            return value
        mapped[str(item['speaker_id'])] = item
    return mapped


def _flatten(value: Any, prefix: str = '') -> dict[str, Any]:
    if prefix == 'speakers':
        value = _speaker_map(value)
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key in sorted(value):
            if key == 'parent_context_id':
                continue
            child_prefix = f'{prefix}.{key}' if prefix else str(key)
            output.update(_flatten(value[key], child_prefix))
        return output
    return {prefix: value}


def context_differences(payload_a: dict[str, Any], payload_b: dict[str, Any]) -> list[dict[str, Any]]:
    relevant_a = {key: payload_a.get(key) for key in ('room', 'speakers', 'measurement_point', 'microphone', 'avr')}
    relevant_b = {key: payload_b.get(key) for key in ('room', 'speakers', 'measurement_point', 'microphone', 'avr')}
    flat_a = _flatten(relevant_a)
    flat_b = _flatten(relevant_b)
    differences: list[dict[str, Any]] = []
    for path in sorted(set(flat_a) | set(flat_b)):
        left = flat_a.get(path)
        right = flat_b.get(path)
        if left != right:
            differences.append({'path': path, 'a': left, 'b': right})
    return differences


def classify_differences(differences: list[dict[str, Any]], expected_change_paths: list[str]) -> dict[str, list[dict[str, Any]]]:
    expected = [path.strip() for path in expected_change_paths if path.strip()]

    def is_expected(path: str) -> bool:
        return any(path == item or path.startswith(item + '.') for item in expected)

    intended = [item for item in differences if is_expected(str(item['path']))]
    confounders = [item for item in differences if not is_expected(str(item['path']))]
    return {'intended': intended, 'confounders': confounders}
