import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.getenv("HAPPY_PATH_DB_PATH", str(PACKAGE_ROOT / "data" / "happy_path.db")))
GOONG_API_KEY = os.getenv("GOONG_API_KEY", "").strip()
GOONG_BASE_URL = os.getenv("GOONG_BASE_URL", "https://rsapi.goong.io").rstrip("/")
GOONG_SPEEDLIMIT_URL = os.getenv("GOONG_SPEEDLIMIT_URL", "https://speedlimit.goong.io/api/v1/speedlimit")
GOONG_TIMEOUT_SEC = float(os.getenv("GOONG_TIMEOUT_MS", "280")) / 1000.0


def load_goong_key() -> str:
    """Explicit offline-CLI credential loading; do not activate live HTTP decoration implicitly."""
    key = os.getenv("GOONG_API_KEY") or os.getenv("GOONG_REST_KEY") or ""
    if key:
        return key.strip()
    dotenv = PACKAGE_ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() in ("GOONG_API_KEY", "GOONG_REST_KEY"):
                key = value.strip().strip("\"'")
                if key:
                    return key
    return ""
