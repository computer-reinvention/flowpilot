"""Declared reflex inputs: bounded schema, argument binding and typed errors.

A saved reflex may declare the inputs it reads (`inputs.<name>` in its script).
The model passes values it resolved from the user's request when it calls the
reflex; they are validated here BEFORE any browser effect. Missing or invalid
inputs are a caller error, never a broken script: they must not consume an
action attempt or enter the repair pipeline.

Reflexes saved before declarations existed (no schema) keep their historical
behaviour: they receive the task inputs, overlaid with any call inputs.
"""

import json
import re

from .task_contracts import ErrorCode, bounded_values

INPUT_TYPES = ('string', 'number', 'integer', 'boolean')
MAX_FIELDS = 32
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
_INTEGER = re.compile(r'[+-]?\d{1,18}')
_NUMBER = re.compile(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?')
_REFERENCE = re.compile(r'''inputs(?:\?)?(?:\.([A-Za-z_$][\w$]*)|\[\s*['"]([^'"\]]{1,64})['"]\s*\])''')

# Tool-schema fragment for save_reflex. Arrays of flat objects are accepted by
# every supported provider; defaults travel as strings and are coerced on save.
FIELDS_SCHEMA = {
    'type': 'array', 'maxItems': MAX_FIELDS,
    'description': ('Declared inputs the script reads as inputs.<name>. Every task-varying value '
                    '(route, date, passenger count, query, filters) must be declared here.'),
    'items': {'type': 'object', 'additionalProperties': False, 'required': ['name', 'type'], 'properties': {
        'name': {'type': 'string', 'description': 'Identifier used as inputs.<name>'},
        'type': {'type': 'string', 'enum': list(INPUT_TYPES)},
        'description': {'type': 'string', 'description': 'Meaning and format, e.g. absolute date YYYY-MM-DD'},
        'required': {'type': 'boolean', 'description': 'Default true unless a default is given'},
        'default': {'type': 'string', 'description': 'Documented semantic default, coerced to type'},
        'enum': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Allowed values'},
    }},
}

CALL_SCHEMA = {
    'type': 'object',
    'description': ("Values for the stage's declared inputs, keyed by input name, resolved from the "
                    "user's request (e.g. {\"origin\": \"Berlin\", \"date\": \"2026-11-02\"}). Names are descriptive "
                    "words of 3+ letters (pickup, drop, query), never single letters. "
                    'Declared inputs not given here fall back to task input values, then defaults.'),
}


class ReflexInputError(ValueError):
    """Typed, model-readable rejection. The script did not run."""

    def __init__(self, reflex, message, *, missing=(), invalid=(), unknown=(), parameters=None):
        super().__init__(message)
        self.reflex = reflex
        self.missing = list(missing)
        self.invalid = list(invalid)
        self.unknown = list(unknown)
        self.parameters = parameters

    def payload(self):
        result = {
            'success': False,
            'error': f"Reflex '{self.reflex}' was not run: {self}. Supply the missing/invalid inputs "
                     '(resolve them from the request or ask with request_input_values) and call run_reflex again.',
            'error_code': ErrorCode.INVALID_INPUT.value,
            'reflex': self.reflex,
            'script_ran': False,
            'repair_attempted': False,
        }
        for key in ('missing', 'invalid', 'unknown'):
            if getattr(self, key):
                result[key] = getattr(self, key)
        if self.parameters is not None:
            result['parameters'] = self.parameters
        return result

    def to_text(self):
        return json.dumps(self.payload(), ensure_ascii=False)


def _coerce(field, value):
    """Return (value, None) or (None, reason). Never echoes the rejected value."""
    kind = field['type']
    if value is None:
        return None, 'is null'
    if kind == 'string':
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None, 'must be a string'
        value = value if isinstance(value, str) else json.dumps(value)
    elif kind == 'boolean':
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            value = value.strip().lower() == 'true'
        if not isinstance(value, bool):
            return None, 'must be a boolean'
    elif kind == 'integer':
        if isinstance(value, str) and _INTEGER.fullmatch(value.strip()):
            value = int(value.strip())
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            return None, 'must be an integer'
    elif kind == 'number':
        if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
            text = value.strip()
            value = int(text) if _INTEGER.fullmatch(text) else float(text)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return None, 'must be a number'
    if field.get('enum') is not None and str(value) not in field['enum']:
        return None, 'must be one of ' + ', '.join(field['enum'])
    return value, None


def normalize_fields(raw):
    """Validate a declared schema for persistence. None means undeclared (legacy)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):  # convenience: {"name": {"type": ...}}
        raw = [{'name': key, **(spec if isinstance(spec, dict) else {'type': spec})} for key, spec in raw.items()]
    if not isinstance(raw, list) or len(raw) > MAX_FIELDS:
        raise ValueError(f'inputs must be an array of at most {MAX_FIELDS} declared fields')
    fields, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('Each declared input must be an object with name and type')
        unknown = set(item) - {'name', 'type', 'description', 'required', 'default', 'enum'}
        if unknown:
            raise ValueError('Unsupported declared input keys: ' + ', '.join(sorted(unknown)))
        name, kind = item.get('name'), item.get('type', 'string')
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError('Declared input names must be identifiers of at most 64 characters')
        if name in seen:
            raise ValueError(f'Duplicate declared input {name}')
        if kind not in INPUT_TYPES:
            raise ValueError(f'Declared input {name} type must be one of ' + ', '.join(INPUT_TYPES))
        seen.add(name)
        field = {'name': name, 'type': kind}
        description = item.get('description')
        if description is not None:
            if not isinstance(description, str) or len(description) > 300:
                raise ValueError(f'Declared input {name} description must be text of at most 300 characters')
            field['description'] = description
        if item.get('enum') is not None:
            enum = item['enum']
            if not isinstance(enum, list) or not enum or len(enum) > 50 or any(
                    isinstance(v, (dict, list)) or v is None or len(str(v)) > 200 for v in enum):
                raise ValueError(f'Declared input {name} enum must list 1-50 short values')
            field['enum'] = [v if isinstance(v, str) else json.dumps(v) for v in enum]
        if item.get('default') is not None:
            default, reason = _coerce(field, item['default'])
            if reason:
                raise ValueError(f'Declared input {name} default {reason}')
            field['default'] = default
        required = item.get('required')
        if required is not None and not isinstance(required, bool):
            raise ValueError(f'Declared input {name} required must be a boolean')
        field['required'] = ('default' not in field) if required is None else required
        fields.append(field)
    return fields


def referenced_inputs(script):
    """Best-effort names a legacy (undeclared) script reads; catalog hint only."""
    names = []
    for match in _REFERENCE.finditer(script or ''):
        name = match.group(1) or match.group(2)
        if name not in names:
            names.append(name)
        if len(names) >= MAX_FIELDS:
            break
    return names


def catalog_entry(fields, script):
    """Model-visible parameter description for a saved reflex."""
    if fields is None:
        return {'inputs': None, 'referenced_inputs': referenced_inputs(script)}
    return {'inputs': fields}


def bind_inputs(reflex, fields, call_inputs, task_inputs):
    """Return the exact inputs object the script receives, or raise ReflexInputError."""
    task_inputs = task_inputs or {}
    if call_inputs is None:
        call_inputs = {}
    if isinstance(call_inputs, str):  # some models serialize object arguments
        try:
            call_inputs = json.loads(call_inputs) if call_inputs.strip() else {}
        except ValueError:
            pass
    if not isinstance(call_inputs, dict):
        raise ReflexInputError(reflex, 'inputs must be an object keyed by input name', parameters=fields)
    try:
        bounded_values(call_inputs)
    except (TypeError, ValueError):
        raise ReflexInputError(reflex, 'inputs exceed the allowed size', parameters=fields) from None
    if fields is None:
        # Legacy undeclared reflex: historical task inputs plus explicit call values.
        return {**task_inputs, **call_inputs}
    names = {f['name'] for f in fields}
    unknown = sorted(k for k in call_inputs if k not in names)
    missing, invalid, bound = [], [], {}
    for field in fields:
        name = field['name']
        if name in call_inputs:
            value = call_inputs[name]
        elif name in task_inputs:
            value = task_inputs[name]
        elif 'default' in field:
            bound[name] = field['default']
            continue
        else:
            if field['required']:
                missing.append(name)
            continue
        if isinstance(value, str) and not value.strip() and field['type'] == 'string':
            if field['required']:
                missing.append(name)
            continue
        coerced, reason = _coerce(field, value)
        if reason:
            invalid.append({'name': name, 'reason': reason})
        else:
            bound[name] = coerced
    if missing or invalid or unknown:
        parts = []
        if missing:
            parts.append('missing required ' + ', '.join(missing))
        if invalid:
            parts.append('invalid ' + ', '.join(f"{i['name']} ({i['reason']})" for i in invalid))
        if unknown:
            parts.append('undeclared ' + ', '.join(unknown))
        raise ReflexInputError(reflex, '; '.join(parts), missing=missing, invalid=invalid,
                               unknown=unknown, parameters=fields)
    return bound
