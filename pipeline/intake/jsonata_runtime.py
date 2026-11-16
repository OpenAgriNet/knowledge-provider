"""Evaluate source mappings in a separate, bounded process."""

import json
import os
import subprocess
import sys
import threading

_MAX_BYTES = 2 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(2)


def evaluate(expression: str, payload: object) -> object:
    data = json.dumps([expression, payload], ensure_ascii=False, allow_nan=False).encode()
    if len(data) > _MAX_BYTES + 32768:
        raise ValueError("JSONata input is too large")
    if not _SLOTS.acquire(timeout=2):
        raise ValueError("JSONata evaluation is busy")
    try:
        result = subprocess.run(
            [sys.executable, "-I", os.path.abspath(__file__)],
            input=data, capture_output=True, timeout=3, env={}, check=False,
        )
        if result.returncode or len(result.stdout) > _MAX_BYTES:
            raise ValueError("JSONata mapping failed or exceeded its limits")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("JSONata mapping could not finish") from error
    finally:
        _SLOTS.release()


def _main() -> None:
    import resource

    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))

    import jsonata
    import re2

    def linear_regex(pattern, flags):
        options = re2.Options()
        options.case_sensitive = not flags.case_insensitive
        options.one_line = not flags.multiline
        options.log_errors = False
        return re2.compile(pattern, options)

    expression, payload = json.load(sys.stdin)
    result = jsonata.Jsonata(expression, linear_regex, timeout=1000, stack=200).evaluate(payload)
    # Write only valid bounded JSON; never put sender input or engine errors in server logs.
    output = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
    if len(output) > _MAX_BYTES:
        raise ValueError("JSONata output is too large")
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    try:
        _main()
    except Exception:
        sys.exit(1)
