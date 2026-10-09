"""Minimal settings for the embedded JioSaavn client (copied from the
standalone jiosaavn-api project so Echo no longer needs a second service)."""
import os


class _Settings:
    SAAVN_BASE_URL: str = os.getenv("SAAVN_BASE_URL", "https://www.jiosaavn.com/api.php")
    REQUEST_TIMEOUT: int = int(os.getenv("SAAVN_REQUEST_TIMEOUT", "10"))


settings = _Settings()
