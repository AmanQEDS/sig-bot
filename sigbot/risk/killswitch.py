"""
sigbot.risk.killswitch
----------------------
Emergency Kill Switch:
- File flag check (e.g. KILL_SWITCH or stop.flag)
- Environment variable check (SIG_KILL_SWITCH=1)
- Triggering cancel-all and halting execution immediately across the process.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("sigbot.killswitch")

KILL_SWITCH_FILENAME = "KILL_SWITCH"


def is_kill_switch_active(flag_dir: Path | str = ".") -> bool:
    """Check if the global kill switch is engaged via env var or file flag."""
    if os.environ.get("SIG_KILL_SWITCH", "").strip() in ("1", "true", "TRUE", "yes", "YES"):
        return True
    flag_file = Path(flag_dir) / KILL_SWITCH_FILENAME
    if flag_file.exists():
        return True
    alt_flag = Path(flag_dir) / "stop.flag"
    if alt_flag.exists():
        return True
    return False


def engage_kill_switch(client=None, flag_dir: Path | str = ".") -> None:
    """Engage kill switch: drop file flag, call cancel_all on client if provided."""
    flag_file = Path(flag_dir) / KILL_SWITCH_FILENAME
    flag_file.touch()
    log.warning("KILL SWITCH ENGAGED! Flag created at %s", flag_file.absolute())

    if client:
        try:
            log.warning("Invoking emergency cancel_all on API client...")
            client.cancel_all()
            log.info("Cancel-all order sweep completed.")
        except Exception as e:
            log.error("Failed to execute cancel_all during kill switch engagement: %s", e)


def disengage_kill_switch(flag_dir: Path | str = ".") -> None:
    """Disengage kill switch: remove file flags."""
    for name in (KILL_SWITCH_FILENAME, "stop.flag"):
        f = Path(flag_dir) / name
        if f.exists():
            f.unlink()
            log.info("Removed kill switch flag: %s", f)
