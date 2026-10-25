"""Exceptions for the ChmlFrp async SDK."""


class ChmlFrpError(Exception):
    """Base error. Raised when the API returns state != 'success'
    or when the HTTP layer fails."""

    def __init__(self, message: str, *, code=None, data=None, status=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data
        self.status = status

    def __repr__(self):
        return f"ChmlFrpError({self.message!r}, code={self.code}, status={self.status})"


class AuthError(ChmlFrpError):
    """Login/refresh failed or the session is invalid."""


class CaptchaRequired(ChmlFrpError):
    """The endpoint (e.g. /qiandao sign-in) needs behavioral-captcha
    fields that the SDK cannot obtain by itself."""
