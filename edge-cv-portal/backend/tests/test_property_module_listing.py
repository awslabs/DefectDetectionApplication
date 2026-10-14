"""Property test for Module_Listing parsing (task 4.5).

**Feature: custom-node-designer, Property 5: Module listing parse covers every module**

For all synthetic Module_Listing documents generated from a random set
of module entries, parsing produces exactly that set of modules, each
with its name and repository location (`module_repo_url`) and the
classification `classify_plugin_set` derives from them; documents that
contain no parseable module table raise ModuleListingParseError.

**Validates: Requirements 6.1**

The parse under test (`parse_module_listing`) is a pure function over
the fetched page content, so it is exercised directly with no AWS or
network involvement. The module is imported through the shared
moto-backed session fixture only so the real `shared_utils` layer (not
a test fake) backs the import.

Generator notes: synthetic pages embed the module rows in an HTML
table whose header row starts with a "module" column, exactly as the
official listing page does, surrounded by random layout/navigation
tables (which carry no such header) and free text noise. Module names
mix random identifiers with the official plugin-set names
(gst-plugins-good/bad/ugly) so classified and unclassified entries
both occur. Cell text is HTML-escaped on render; the parser
whitespace-normalizes cell text, so expected descriptions are compared
whitespace-normalized.
"""

from __future__ import annotations

import html

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.fixture(scope="session")
def importer(aws_stack):
    """The real plugin_importer module, imported via the session stack."""
    return aws_stack.plugin_importer


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

OFFICIAL_SET_NAMES = ("gst-plugins-good", "gst-plugins-bad", "gst-plugins-ugly")

#: Random module identifiers (no whitespace, so the parser's cell-text
#: normalization leaves them unchanged), mixed with the official names.
_random_name = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-._", min_size=1, max_size=24)
_module_name = st.one_of(_random_name, st.sampled_from(OFFICIAL_SET_NAMES))

#: Descriptions: free text including HTML-significant characters (the
#: renderer escapes them; convert_charrefs on the parser unescapes).
_description = st.text(
    alphabet=" abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "0123456789,.&<>'\"()-/",
    max_size=60)

_module_entry = st.tuples(_module_name, _description)

#: Free-text noise placed between tables (data outside cells is ignored
#: by the parser; kept tag-free so it cannot open a stray element).
_free_text = st.text(
    alphabet=" abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,",
    max_size=40)

#: Cell words for layout/navigation tables. Never equal to "module"
#: (case-insensitively), so a noise table can never carry the module
#: header the parse keys on.
_noise_word = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12,
).filter(lambda w: w != "module")


@st.composite
def _noise_table(draw) -> str:
    """A layout/navigation table: either header-less (td-only rows) or
    headed by a non-"module" column, so the parse must ignore it."""
    rows = []
    if draw(st.booleans()):
        header = draw(st.lists(_noise_word, min_size=1, max_size=4))
        rows.append("<tr>" + "".join(f"<th>{w}</th>" for w in header) + "</tr>")
    for cells in draw(st.lists(
            st.lists(_noise_word, min_size=1, max_size=4), max_size=4)):
        rows.append("<tr>" + "".join(f"<td>{w}</td>" for w in cells) + "</tr>")
    return '<table class="layout">' + "".join(rows) + "</table>"


def _render_module_table(importer, entries, header_case: str,
                         extra_cell: bool) -> str:
    """The module index table exactly as the listing page shapes it: a
    header row starting with a "module" column, then one row per module
    whose first cell links the module page and whose second cell
    carries the description."""
    header = (f"<tr><th>{header_case}</th><th>Description</th>"
              + ("<th>Version</th>" if extra_cell else "") + "</tr>")
    rows = []
    for name, description in entries:
        cells = (
            f'<td><a href="{importer.module_repo_url(name)}">'
            f'{html.escape(name)}</a></td>'
            f'<td>{html.escape(description)}</td>'
        )
        if extra_cell:
            cells += "<td>1.0</td>"
        rows.append(f"<tr>{cells}</tr>")
    return '<table class="modules">' + header + "".join(rows) + "</table>"


# ---------------------------------------------------------------------------
# Property: parse covers every module, exactly
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(
    entries=st.lists(_module_entry, min_size=1, max_size=15),
    header_case=st.sampled_from(("Module", "module", "MODULE")),
    extra_cell=st.booleans(),
    before=st.lists(st.one_of(_noise_table(), _free_text), max_size=3),
    after=st.lists(st.one_of(_noise_table(), _free_text), max_size=3),
)
def test_parse_yields_exactly_the_generated_modules(
        importer, entries, header_case, extra_cell, before, after):
    """**Feature: custom-node-designer, Property 5: Module listing parse
    covers every module**

    For all synthetic Module_Listing documents generated from a random
    set of module entries, parsing produces exactly that set of
    modules — one entry per generated row, in order, each with its
    name, whitespace-normalized description, published repository
    location, and classification.

    **Validates: Requirements 6.1**
    """
    page = (
        "<html><body>"
        + "".join(before)
        + _render_module_table(importer, entries, header_case, extra_cell)
        + "".join(after)
        + "</body></html>"
    )

    parsed = importer.parse_module_listing(page)

    expected = []
    for name, description in entries:
        repo_url = importer.module_repo_url(name)
        expected.append({
            "name": name,
            "description": " ".join(description.split()),
            "repoUrl": repo_url,
            "classification": importer.classify_plugin_set(name, repo_url),
        })
    assert parsed == expected


# ---------------------------------------------------------------------------
# Property: pages without a module table raise ModuleListingParseError
# ---------------------------------------------------------------------------

@settings(max_examples=25, deadline=None)
@given(
    chunks=st.lists(st.one_of(_noise_table(), _free_text), max_size=5),
    empty_module_table=st.booleans(),
)
def test_pages_without_module_table_raise(
        importer, chunks, empty_module_table):
    """**Feature: custom-node-designer, Property 5: Module listing parse
    covers every module**

    For all documents containing no parseable module table — layout
    tables, free text, and at most a module-headed table with zero
    module rows — the parse raises ModuleListingParseError.

    **Validates: Requirements 6.1**
    """
    body = "".join(chunks)
    if empty_module_table:
        body += ('<table class="modules">'
                 "<tr><th>Module</th><th>Description</th></tr></table>")
    page = f"<html><body>{body}</body></html>"

    with pytest.raises(importer.ModuleListingParseError):
        importer.parse_module_listing(page)
