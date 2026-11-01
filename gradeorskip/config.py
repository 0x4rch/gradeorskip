import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # never committed; see README
DATA_DIR = Path(os.environ.get("GRADEORSKIP_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BRAND = os.environ.get("GRADEORSKIP_BRAND", "Grade or Skip")
# TAG is off until a source covers TAG 9 and below: sold data only reached TAG 10 on real cards.
# The AGPL asks anyone running a modified copy to offer its source; point this at yours.
SOURCE_URL = os.environ.get("GRADEORSKIP_SOURCE_URL", "https://github.com/0x4rch/gradeorskip")
TAG_ENABLED = os.environ.get("GRADEORSKIP_TAG") == "1"
