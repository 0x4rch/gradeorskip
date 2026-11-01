import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Keep tests away from the real data dir, network LLMs and any keys in .env.
os.environ["GRADEORSKIP_DATA_DIR"] = tempfile.mkdtemp(prefix="gradeorskip-test-")
os.environ["GRADEORSKIP_LLM_PROVIDER"] = "none"
os.environ["GRADEORSKIP_SCRAPE_PRICECHARTING"] = "0"  # no network in tests
FIXTURES = ROOT / "tests" / "fixtures" / "synthetic"
