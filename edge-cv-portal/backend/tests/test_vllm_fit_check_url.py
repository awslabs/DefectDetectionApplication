"""
Pins the control behind the `already-mitigated` B310 entry for the fit
check's Hugging Face fetch (security-scan-remediation-high, design
`Already-mitigated findings`): whatever the model id, the URL that
``_estimate_from_hf`` hands to its fetcher stays on the fixed
``https://huggingface.co/api/models/...?blobs=true`` template.

The fetcher is a recorder that returns ``None``, so nothing is fetched and
the estimate is ``None``. The path prefix is checked on the path as recorded,
not after normalization: ``quote(model_id, safe='/')`` keeps ``/``, so an id
with ``..`` gives a path that would normalize outside ``/api/models/``, while
the scheme, host and query stay fixed.
# Validates: Requirements 9.5, 5.3
"""
from urllib.parse import urlsplit

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vllm_fit_check import _estimate_from_hf

# Characters that would change the URL's structure if they reached it
# unquoted, mixed with arbitrary text.
_PIECES = ("?", "#", "@", ":", "..", " ", "/")
_model_ids = st.lists(
    st.one_of(st.sampled_from(_PIECES), st.text(max_size=8)), max_size=12
).map("".join)


@settings(max_examples=settings.get_profile("default").max_examples, deadline=None)
@given(model_id=_model_ids)
@example(model_id="org/model?blobs=false")
@example(model_id="org/model#fragment")
@example(model_id="someone@other.example/model")
@example(model_id="other.example:8443/model")
@example(model_id="../../../outside/model")
@example(model_id="org/my model")
@example(model_id="")
def test_hf_url_stays_on_the_fixed_template(model_id):
    recorded = []

    def hf_fetch(url):
        recorded.append(url)
        return None

    assert _estimate_from_hf(model_id, {}, hf_fetch) is None
    assert len(recorded) == 1
    parts = urlsplit(recorded[0])
    assert parts.scheme == "https"
    assert parts.netloc == "huggingface.co"
    assert parts.hostname == "huggingface.co"
    assert parts.path.startswith("/api/models/")
    assert parts.query == "blobs=true"
    assert parts.fragment == ""
