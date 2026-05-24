import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env first so live tests (e.g. M4 smoke against the real router) pick
# up real credentials. Then setdefault placeholders for any var still missing,
# so non-LLM tests (M1/M2/M3) keep working without secrets.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

os.environ.setdefault("API_ROUTER_BASE", "https://api.anthropic.com")
os.environ.setdefault("API_ROUTER_KEY", "test-placeholder-key")
