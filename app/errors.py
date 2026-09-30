"""Domain errors raised by services and mapped to HTTP responses in one place."""


class DomainError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class Conflict(DomainError):
    """The request is valid but conflicts with the current state (e.g. an invalid transition)."""

    status_code = 409
    code = "conflict"


class Invalid(DomainError):
    status_code = 422
    code = "invalid"
