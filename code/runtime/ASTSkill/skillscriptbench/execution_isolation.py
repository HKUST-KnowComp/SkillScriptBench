from __future__ import annotations

import shutil
from pathlib import Path


def isolated_command(
    command: list[str],
    *,
    mode: str,
    temporary_root: Path,
) -> list[str]:
    if mode == "none":
        return command
    if mode != "sandbox-exec":
        raise ValueError(f"unsupported_sandbox_mode:{mode}")
    executable = shutil.which("sandbox-exec")
    if executable is None:
        raise RuntimeError("sandbox-exec_not_available")
    profile = (
        '(version 1) (allow default) (deny network*) (deny file-write*) '
        f'(allow file-write* (subpath "{temporary_root}"))'
    )
    return [executable, "-p", profile, *command]
