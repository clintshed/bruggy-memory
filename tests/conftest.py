import os
import sys

# Ensure the app source (src/) and the shared stub module are importable
# regardless of how pytest is invoked. The pyproject `pythonpath` ini option
# also covers this, but this keeps `python -m pytest tests/` working directly.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
