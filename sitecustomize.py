"""Compatibility shims for third-party dependencies during tests.

`pytest-aiohttp` imports `aiodns`, which annotates resolver methods with
`pycares.ares_query_a_result`. The current `pycares` wheel on Python 3.13
omits that attribute, causing class creation to fail before tests import the
integration. Provide a lightweight placeholder so the annotations resolve.
"""

try:
    import pycares
except ModuleNotFoundError:
    # pycares is not installed (e.g. mypy-only CI run); nothing to patch.
    pycares = None  # type: ignore[assignment]

_ARES_RESULT_TYPES = {
    "ares_query_a_result",
    "ares_query_aaaa_result",
    "ares_query_caa_result",
    "ares_query_cname_result",
    "ares_query_mx_result",
    "ares_query_naptr_result",
    "ares_query_ns_result",
    "ares_query_ptr_result",
    "ares_query_soa_result",
    "ares_query_srv_result",
    "ares_query_txt_result",
    "ares_host_result",
    "ares_addrinfo_result",
    "ares_nameinfo_result",
}

if pycares is not None:
    for _result_name in _ARES_RESULT_TYPES:
        if not hasattr(pycares, _result_name):
            placeholder = type(_result_name, (), {})
            setattr(pycares, _result_name, placeholder)
