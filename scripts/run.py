"""
Convenience runner script — identical to calling app/main.py directly.

Usage:
    python scripts/run.py
    python scripts/run.py --symbol ETHUSDT --dry-run
    python scripts/run.py --backtest --bars 1000
"""

import sys
from pathlib import Path

# Ensure project root is on path when running from scripts/
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.main import main

if __name__ == "__main__":
    main()
