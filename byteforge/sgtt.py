"""Qualification status; never substitute another encoder for actual SGTT."""

SOURCE_REVISION = "e202e36e2b33a4768d1122f96880b64416234175"
PAPER_DOI = "10.14778/3836663.3836714"
BLOCKER = (
    "Actual SGTT is unavailable: permissions for token-vldb2026 source and patched "
    "token-table assets are not established; no authorized adapter is integrated. "
    "No test-only encoder substitution is permitted. See docs/sgtt_hdfs_2k_results.md"
)


class SGTTUnavailableError(RuntimeError):
    """Actual research implementation is not qualified for use."""


def status() -> dict:
    return {
        "encoder": "actual-sgtt",
        "available": False,
        "adapter_implemented": False,
        "status": "unsupported",
        "source_revision": SOURCE_REVISION,
        "paper_doi": PAPER_DOI,
        "reason": BLOCKER,
    }


def require_sgtt() -> None:
    raise SGTTUnavailableError(BLOCKER)
