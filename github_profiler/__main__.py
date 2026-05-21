"""GitHub Developer Intelligence System - Entry Point"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from github_profiler.cli import main

if __name__ == "__main__":
    main()
