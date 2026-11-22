"""Bounds shared by the onboard assistant's paired P1 tool commands."""

import json


AGENT_CALLBACK_TOOLS = frozenset({'device_status', 'list_role_actions'})
AGENT_CALLBACK_RESULT_BYTES = 800


def valid_callback_id(value):
    return type(value) is int and 0 < value <= 0xffffffff


def valid_callback_result(value):
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return False
    try:
        # Match Channel's wire encoding and leave room for the request envelope.
        encoded = json.dumps(value, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return len(encoded) <= AGENT_CALLBACK_RESULT_BYTES


def fit_callback_result(name, outcome):
    if valid_callback_result(outcome.value):
        return outcome.ok, outcome.value
    if name == 'list_role_actions' and outcome.ok:
        shortened = {'status': 'ok', 'actions': [], 'truncated': True}
        for action in outcome.value['actions']:
            shortened['actions'].append(action)
            if not valid_callback_result(shortened):
                shortened['actions'].pop()
                break
        return True, shortened
    return False, {'status': 'result_too_large'}
