"""Put the shared-module folder on sys.path so tests can import its scripts by name."""

import sys
from pathlib import Path

SHARED_MODULE = Path(__file__).resolve().parent.parent / "3-shared-module"
if str(SHARED_MODULE) not in sys.path:
    sys.path.insert(0, str(SHARED_MODULE))
