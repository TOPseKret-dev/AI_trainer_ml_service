class MLServiceError(Exception):
    """Only fixed, public-safe details belong in these errors."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ProviderUnavailable(MLServiceError):
    def __init__(self) -> None:
        super().__init__(503, "ML provider is unavailable")


class ProviderNotConfigured(MLServiceError):
    def __init__(self) -> None:
        super().__init__(503, "OPENROUTER_API_KEY is not configured")


class ProviderTimeout(MLServiceError):
    def __init__(self) -> None:
        super().__init__(504, "ML provider request timed out")


class InvalidProviderResponse(MLServiceError):
    def __init__(self) -> None:
        super().__init__(502, "ML provider returned an invalid response")
