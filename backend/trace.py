"""Lightweight function-call tracer for debugging reflex pipelines.

Writes JSON trace files to TRACES_DIR (default: /tmp/flowpilot-traces/).
Each trace captures the function name, inputs, outputs, and timing.

Enable: set FLOWPILOT_TRACE=1 in the environment.
Disable: unset or set to 0 (default — zero overhead).
"""

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TRACES_DIR = Path(os.environ.get("FLOWPILOT_TRACES_DIR", "/tmp/flowpilot-traces"))
ENABLED = os.environ.get("FLOWPILOT_TRACE", "0") == "1"

_seq = 0


def trace(func_name: str, **data) -> None:
    """Write a single trace entry as a JSON file."""
    if not ENABLED:
        return
    global _seq
    _seq += 1
    TRACES_DIR.mkdir(parents=True, exist_ok=True)

    entry = {
        "seq": _seq,
        "ts": time.time(),
        "func": func_name,
        **data,
    }

    fname = f"{_seq:04d}_{func_name}.json"
    path = TRACES_DIR / fname
    try:
        path.write_text(json.dumps(entry, indent=2, default=str))
    except Exception as e:
        logger.debug("Trace write failed: %s", e)
