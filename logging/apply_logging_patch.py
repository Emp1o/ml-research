#!/usr/bin/env python3
"""Patch installed vLLM 0.31 so a model call is logged and prompt text is not."""

from pathlib import Path

import vllm

ROOT = Path(vllm.__file__).resolve().parent

REQUEST_LOGGER_GATE = (
    "    if args.enable_log_requests:\n"
    "        request_logger = RequestLogger(max_log_len=args.max_log_len)\n"
    "    else:\n"
    "        request_logger = None\n"
)
REQUEST_LOGGER_ALWAYS = (
    "    # SafeBoard: always log the fact of a model call. The patched\n"
    "    # RequestLogger does not write prompt text or generated text.\n"
    "    request_logger = RequestLogger(max_log_len=args.max_log_len)\n"
)

PROMPT_LOG = 'logger.debug("Input prompt: %s", text_prompt)'
PROMPT_LOG_REDACTED = 'logger.debug("Input prompt omitted, chars=%s", len(text_prompt))'


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected 1 occurrence, found {count}")
    path.write_text(text.replace(old, new, 1))
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for pyc in cache.glob(path.stem + ".*.pyc"):
            pyc.unlink()


def main() -> None:
    gates = [
        ROOT / "entrypoints/launchers/api_server/app_state.py",
        ROOT / "entrypoints/launchers/render/app_state.py",
    ]
    for path in gates:
        replace_once(path, REQUEST_LOGGER_GATE, REQUEST_LOGGER_ALWAYS)
    replace_once(ROOT / "entrypoints/chat_utils.py", PROMPT_LOG, PROMPT_LOG_REDACTED)
    print("logging patch applied")


if __name__ == "__main__":
    main()
