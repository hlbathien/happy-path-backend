import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = Path(os.getenv("HAPPY_PATH_DB_PATH", str(PACKAGE_ROOT / "data" / "happy_path.db")))
GOONG_API_KEY = os.getenv("GOONG_API_KEY", "").strip()
GOONG_BASE_URL = os.getenv("GOONG_BASE_URL", "https://rsapi.goong.io").rstrip("/")
GOONG_SPEEDLIMIT_URL = os.getenv("GOONG_SPEEDLIMIT_URL", "https://speedlimit.goong.io/api/v1/speedlimit")
GOONG_TIMEOUT_SEC = float(os.getenv("GOONG_TIMEOUT_MS", "280")) / 1000.0
