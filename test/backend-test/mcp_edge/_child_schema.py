# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Hypothesis strategies for schema-valid edge tool arguments (test-only).

`arguments(spec)` draws an argument object that `Draft202012Validator`
accepts for `spec.input_schema`. It covers the constructs the catalog
helpers emit (`child.catalog.identifier`, `text`, `enum`, `integer`,
`boolean`, `array`, `json_object`, `bare_name`, `confirm_arg`, the
top-level `oneOf`/`anyOf` required-groups, `allOf` patterns and
`not: {const}`) and filters every draw through the validator, so a
construct it renders imprecisely costs a rejected example, never an
invalid one.
"""
import re

import jsonschema
from hypothesis import assume
from hypothesis import strategies as st

_SCALARS = st.one_of(st.none(), st.booleans(), st.integers(-1000, 1000),
                     st.text(max_size=12))


def _string(prop: dict):
    max_length = min(prop.get('maxLength', 40), 40)
    min_length = prop.get('minLength', 0)
    if 'pattern' in prop:
        strategy = st.from_regex(prop['pattern'], fullmatch=True).filter(
            lambda s: min_length <= len(s) <= prop.get('maxLength', 10_000))
    else:
        strategy = st.text(min_size=min_length, max_size=max(max_length, min_length))
    for extra in prop.get('allOf', ()):
        if 'pattern' in extra:
            pattern = re.compile(extra['pattern'])
            strategy = strategy.filter(lambda s, p=pattern: p.search(s) is not None)
    if 'not' in prop and 'const' in prop['not']:
        strategy = strategy.filter(lambda s, c=prop['not']['const']: s != c)
    return strategy


def value(prop: dict):
    """A strategy for one property schema."""
    if 'const' in prop:
        return st.just(prop['const'])
    if 'enum' in prop:
        return st.sampled_from(prop['enum'])
    kind = prop.get('type')
    if isinstance(kind, list):
        return st.one_of(*[value(dict(prop, type=k)) for k in kind])
    if kind == 'string':
        return _string(prop)
    if kind == 'integer':
        return st.integers(prop.get('minimum', -1000), prop.get('maximum', 10_000))
    if kind == 'number':
        return st.floats(prop.get('minimum', -1000.0), prop.get('maximum', 10_000.0),
                         allow_nan=False, allow_infinity=False)
    if kind == 'boolean':
        return st.booleans()
    if kind == 'null':
        return st.none()
    if kind == 'array':
        items = value(prop.get('items', {}))
        min_items = prop.get('minItems', 0)
        return st.lists(items, min_size=min_items,
                        max_size=max(min_items, min(prop.get('maxItems', 3), 3)))
    if kind == 'object':
        if 'properties' in prop:
            return _object(prop)
        extra = prop.get('additionalProperties', True)
        values = value(extra) if isinstance(extra, dict) else _SCALARS
        min_properties = prop.get('minProperties', 0)
        return st.dictionaries(st.from_regex(r'[a-z][a-z0-9_]{0,10}', fullmatch=True),
                               values, min_size=min_properties,
                               max_size=max(min_properties, 3))
    return _SCALARS


@st.composite
def _object(draw, schema: dict):
    properties = schema.get('properties', {})
    required = set(schema.get('required', ()))
    excluded = set()
    for group in ('oneOf', 'anyOf'):
        branches = schema.get(group)
        if not branches:
            continue
        chosen = draw(st.sampled_from(branches))
        required |= set(chosen.get('required', ()))
        if group == 'oneOf':
            for branch in branches:
                if branch is not chosen:
                    excluded |= set(branch.get('required', ())) - required
    out = {}
    for name, prop in properties.items():
        if name in excluded:
            continue
        if name in required or draw(st.booleans()):
            out[name] = draw(value(prop))
    return out


@st.composite
def arguments(draw, spec):
    """A schema-valid argument object of `spec`."""
    args = draw(_object(spec.input_schema))
    assume(jsonschema.Draft202012Validator(spec.input_schema).is_valid(args))
    return args
