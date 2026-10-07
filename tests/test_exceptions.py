"""Domain errors and the statuses they map to."""

from app.exceptions import (
    BulkheadFullError,
    CircuitOpenError,
    DocumentNotFoundError,
    ExtractProtocolError,
    ExtractTimeoutError,
    ExtractUnavailableError,
    InvalidPdfError,
    PayloadTooLargeError,
    RetryableError,
)


def test_each_error_carries_its_http_status():
    assert InvalidPdfError().status_code == 400
    assert PayloadTooLargeError().status_code == 413
    assert DocumentNotFoundError().status_code == 404
    assert ExtractProtocolError().status_code == 502
    assert ExtractUnavailableError().status_code == 503
    assert CircuitOpenError().status_code == 503
    assert BulkheadFullError().status_code == 503
    assert ExtractTimeoutError().status_code == 504


def test_our_own_load_shedding_is_not_an_upstream_failure():
    """If these were ExtractUnavailableError the breaker would count its own
    rejections and never close again."""
    assert not isinstance(CircuitOpenError(), ExtractUnavailableError)
    assert not isinstance(BulkheadFullError(), ExtractUnavailableError)
    assert isinstance(CircuitOpenError(), RetryableError)
    assert isinstance(BulkheadFullError(), RetryableError)


def test_retry_after_travels_with_the_error():
    assert CircuitOpenError(retry_after=7.5).retry_after == 7.5
    assert ExtractUnavailableError().retry_after is None
