"""Domain errors and the statuses they map to."""

from app.exceptions import (
    ContentTooLargeError,
    DocumentNotFoundError,
    ExtractProtocolError,
    ExtractTimeoutError,
    ExtractUnavailableError,
    InvalidPdfError,
    PayloadTooLargeError,
    RetryableError,
    UnprocessablePdfError,
    UploadSlotsFullError,
)


def test_each_error_carries_its_http_status():
    assert InvalidPdfError().status_code == 400
    assert PayloadTooLargeError().status_code == 413
    assert ContentTooLargeError().status_code == 422
    assert UnprocessablePdfError().status_code == 422
    assert DocumentNotFoundError().status_code == 404
    assert ExtractProtocolError().status_code == 502
    assert ExtractUnavailableError().status_code == 503
    assert UploadSlotsFullError().status_code == 503
    assert ExtractTimeoutError().status_code == 504


def test_our_own_load_shedding_is_not_an_upstream_failure():
    """Running out of upload slots is our own decision, not extract's fault,
    but it still tells the client when to come back."""
    assert not isinstance(UploadSlotsFullError(), ExtractUnavailableError)
    assert isinstance(UploadSlotsFullError(), RetryableError)
    assert isinstance(ExtractUnavailableError(), RetryableError)


def test_retry_after_travels_with_the_error():
    assert ExtractUnavailableError(retry_after=7.5).retry_after == 7.5
    assert ExtractUnavailableError().retry_after is None


def test_an_unprocessable_pdf_is_not_a_malformed_request():
    """400: documents rechazo el pedido. 422: parece PDF, pero extract no pudo."""
    assert not isinstance(UnprocessablePdfError(), InvalidPdfError)
    assert not isinstance(UnprocessablePdfError(), RetryableError)
