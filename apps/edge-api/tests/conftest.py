from pathlib import Path
import sys


EDGE_API_ROOT = Path(__file__).resolve().parents[1]

if str(EDGE_API_ROOT) not in sys.path:
    sys.path.insert(0, str(EDGE_API_ROOT))

