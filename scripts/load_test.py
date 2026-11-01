#!/usr/bin/env python3
"""Load-test the SafeBoard vLLM OpenAI-compatible endpoint.

Stdlib only. Reads the Bearer token from the API_KEY environment variable
and POSTs streaming chat completions so TTFT is the arrival of the first
generated token, not the first SSE frame.

Metrics (also printed above the table):

- TTFT: time from just before the HTTP call until the first SSE event whose
  choices[].delta.content is a non-empty string. A chunk that only carries
  role is not the first token.
- Latency: time from that same start until the body ends (data: [DONE] or
  EOF). Both intervals include server queueing. They do not include time
  spent waiting for a free client worker.
- Throughput, req/s: successful requests in the scenario divided by the
  scenario wall time (pool start until the last request finishes). Failures
  stay in the denominator.
- Throughput, output tok/s: completion tokens of successful requests over
  that same wall time. Token counts prefer usage.completion_tokens from
  stream_options.include_usage. If the server sends no usage, the count is
  the number of SSE events with non-empty delta.content.
- Throughput, total tok/s: prompt_tokens + completion_tokens from usage,
  over the same wall time. Without usage the cell is left empty.

The long prompt must fit in the server's max model length (this stack uses
512) together with the chat template and max_tokens. A 400 from the context
limit means lower --long-repeats or --max-tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
SHORT_PROMPT = "Reply with one short sentence: what is 2+2?"
LONG_PREFIX = "Summarize the following text in one short sentence.\n\n"
LONG_UNIT = "The quick brown fox jumps over the lazy dog. "
CHAT_PATH = "/v1/chat/completions"
EMPTY = "—"


@dataclass(frozen=True)
class CallResult:
    ok: bool
    ttft_s: float | None
    latency_s: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    error: str | None


@dataclass(frozen=True)
class ScenarioSpec:
    key: str
    title: str
    prompt: str
    max_tokens: int


@dataclass(frozen=True)
class ScenarioSummary:
    spec: ScenarioSpec
    results: tuple[CallResult, ...]
    wall_s: float
    concurrency: int

    @property
    def successes(self) -> list[CallResult]:
        return [item for item in self.results if item.ok]

    @property
    def failures(self) -> list[CallResult]:
        return [item for item in self.results if not item.ok]


def build_long_prompt(repeats: int) -> str:
    if repeats < 1:
        raise ValueError("long repeats must be >= 1")
    return LONG_PREFIX + (LONG_UNIT * repeats)


def content_from_event(event: dict) -> str:
    """Return generated text from one chat completion chunk.

    An empty string and a role-only delta are not a token.
    """
    parts: list[str] = []
    for choice in event.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        text = delta.get("content")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def usage_from_event(event: dict) -> tuple[int | None, int | None]:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    prompt_tokens = int(prompt) if isinstance(prompt, int) else None
    completion_tokens = int(completion) if isinstance(completion, int) else None
    return prompt_tokens, completion_tokens


def _clip(text: str, limit: int = 300) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "..."


def _apply_usage(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    event: dict,
) -> tuple[int | None, int | None]:
    seen_prompt, seen_completion = usage_from_event(event)
    if seen_prompt is not None:
        prompt_tokens = seen_prompt
    if seen_completion is not None:
        completion_tokens = seen_completion
    return prompt_tokens, completion_tokens


def consume_sse(response, t0: float) -> CallResult:
    """Read an SSE body. response is a binary file object."""
    ttft_s: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    content_events = 0
    saw_data = False

    while True:
        raw = response.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            saw_data = True
            break
        saw_data = True
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if "error" in event and not event.get("choices"):
            message = event.get("error")
            return CallResult(
                ok=False,
                ttft_s=None,
                latency_s=time.perf_counter() - t0,
                prompt_tokens=None,
                completion_tokens=None,
                error=_clip(f"stream error: {message}"),
            )
        prompt_tokens, completion_tokens = _apply_usage(
            prompt_tokens, completion_tokens, event
        )
        text = content_from_event(event)
        if text:
            content_events += 1
            if ttft_s is None:
                ttft_s = time.perf_counter() - t0

    latency_s = time.perf_counter() - t0
    if not saw_data:
        return CallResult(
            ok=False,
            ttft_s=None,
            latency_s=latency_s,
            prompt_tokens=None,
            completion_tokens=None,
            error="empty stream",
        )
    if completion_tokens is None:
        completion_tokens = content_events
    return CallResult(
        ok=True,
        ttft_s=ttft_s,
        latency_s=latency_s,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        error=None,
    )


def post_chat_stream(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
) -> CallResult:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + CHAT_PATH,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return consume_sse(response, t0)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return CallResult(
            ok=False,
            ttft_s=None,
            latency_s=time.perf_counter() - t0,
            prompt_tokens=None,
            completion_tokens=None,
            error=_clip(f"HTTP {exc.code}: {detail}"),
        )
    except Exception as exc:  # noqa: BLE001 — one failed call must not kill the pool
        return CallResult(
            ok=False,
            ttft_s=None,
            latency_s=time.perf_counter() - t0,
            prompt_tokens=None,
            completion_tokens=None,
            error=_clip(f"{type(exc).__name__}: {exc}"),
        )


def run_scenario(
    spec: ScenarioSpec,
    *,
    base_url: str,
    api_key: str,
    model: str,
    requests: int,
    concurrency: int,
    timeout: float,
) -> ScenarioSummary:
    wall0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(
                post_chat_stream,
                base_url,
                api_key,
                model,
                spec.prompt,
                spec.max_tokens,
                timeout,
            )
            for _ in range(requests)
        ]
        results = tuple(future.result() for future in futures)
    return ScenarioSummary(
        spec=spec,
        results=results,
        wall_s=time.perf_counter() - wall0,
        concurrency=concurrency,
    )


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.fmean(values)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def fmt_ms(seconds: float | None) -> str:
    if seconds is None:
        return EMPTY
    return f"{seconds * 1000:.1f}"


def fmt_rate(value: float | None) -> str:
    if value is None:
        return EMPTY
    return f"{value:.3f}"


def fmt_tokens(value: float | None) -> str:
    if value is None:
        return EMPTY
    return f"{value:.1f}"


def scenario_rates(summary: ScenarioSummary) -> dict[str, float | None]:
    ok = summary.successes
    wall = summary.wall_s
    ttft = [item.ttft_s for item in ok if item.ttft_s is not None]
    latency = [item.latency_s for item in ok if item.latency_s is not None]
    prompt_values = [item.prompt_tokens for item in ok if item.prompt_tokens is not None]
    completion_values = [
        item.completion_tokens for item in ok if item.completion_tokens is not None
    ]
    req_s = None
    output_tok_s = None
    total_tok_s = None
    if wall > 0 and ok:
        req_s = len(ok) / wall
        if completion_values and len(completion_values) == len(ok):
            output_tok_s = sum(completion_values) / wall
        if (
            prompt_values
            and completion_values
            and len(prompt_values) == len(ok)
            and len(completion_values) == len(ok)
        ):
            total_tok_s = (sum(prompt_values) + sum(completion_values)) / wall
    return {
        "ttft_p50": _median(ttft),
        "ttft_mean": _mean(ttft),
        "latency_p50": _median(latency),
        "latency_mean": _mean(latency),
        "req_s": req_s,
        "output_tok_s": output_tok_s,
        "total_tok_s": total_tok_s,
        "prompt_mean": _mean([float(value) for value in prompt_values]),
        "completion_mean": _mean([float(value) for value in completion_values]),
    }


def render_markdown(summaries: list[ScenarioSummary], preamble: str) -> str:
    metric_header = (
        "| Сценарий | TTFT p50, мс | TTFT среднее, мс | Latency p50, мс | "
        "Latency среднее, мс | Throughput, req/s | Throughput, output tok/s | "
        "Throughput, total tok/s |"
    )
    metric_sep = "|---|---:|---:|---:|---:|---:|---:|---:|"
    shape_header = (
        "| Сценарий | Символов в промпте | max_tokens | N | Параллельность | "
        "Успешно | Prompt tokens, среднее | Output tokens, среднее |"
    )
    shape_sep = "|---|---:|---:|---:|---:|---:|---:|---:|"
    metric_rows = [metric_header, metric_sep]
    shape_rows = [shape_header, shape_sep]
    error_lines: list[str] = []
    for summary in summaries:
        rates = scenario_rates(summary)
        title = summary.spec.title
        metric_rows.append(
            "| {title} | {ttft_p50} | {ttft_mean} | {lat_p50} | {lat_mean} | "
            "{req_s} | {out_s} | {total_s} |".format(
                title=title,
                ttft_p50=fmt_ms(rates["ttft_p50"]),
                ttft_mean=fmt_ms(rates["ttft_mean"]),
                lat_p50=fmt_ms(rates["latency_p50"]),
                lat_mean=fmt_ms(rates["latency_mean"]),
                req_s=fmt_rate(rates["req_s"]),
                out_s=fmt_rate(rates["output_tok_s"]),
                total_s=fmt_rate(rates["total_tok_s"]),
            )
        )
        shape_rows.append(
            "| {title} | {chars} | {max_tokens} | {n} | {conc} | {ok}/{total} | "
            "{prompt} | {completion} |".format(
                title=title,
                chars=len(summary.spec.prompt),
                max_tokens=summary.spec.max_tokens,
                n=len(summary.results),
                conc=summary.concurrency,
                ok=len(summary.successes),
                total=len(summary.results),
                prompt=fmt_tokens(rates["prompt_mean"]),
                completion=fmt_tokens(rates["completion_mean"]),
            )
        )
        for index, failure in enumerate(summary.failures, start=1):
            error_lines.append(
                f"- {title} #{index}: {failure.error or 'unknown error'}"
            )
    parts = [
        preamble.rstrip(),
        "",
        "## Метрики",
        "",
        "\n".join(metric_rows),
        "",
        "## Профиль нагрузки",
        "",
        "\n".join(shape_rows),
    ]
    if error_lines:
        parts.extend(["", "## Ошибки", "", "\n".join(error_lines)])
    parts.append("")
    return "\n".join(parts)


DEFINITIONS = """\
Определения:

- TTFT — от момента перед HTTP-вызовом до первого SSE-события, в котором `choices[].delta.content` непустая строка. Чанк только с `role` первым токеном не считается. В интервал входят установка соединения и очередь на сервере.
- Latency — от того же момента до конца тела (`data: [DONE]` или EOF).
- Ожидание свободного воркера в клиентском пуле в TTFT и Latency не входит.
- Throughput, req/s — успешные запросы сценария, делённые на время от старта пула до конца последнего запроса. Неуспешные в числитель не входят, их время остаётся в знаменателе.
- Throughput, output tok/s — сумма выходных токенов успешных запросов за то же время. Берётся `usage.completion_tokens`. Если usage в потоке нет, это число SSE-событий с непустым `delta.content`.
- Throughput, total tok/s — сумма `usage.prompt_tokens` и `usage.completion_tokens` за то же время. Без usage ячейка пустая.
"""


def build_preamble(
    *,
    base_url: str,
    model: str,
    started: str,
    concurrency: int,
    requests: int,
    max_tokens: int,
    long_repeats: int,
    warmup: int,
) -> str:
    return (
        "# Нагрузочный прогон vLLM\n\n"
        f"- Время (UTC): {started}\n"
        f"- base_url: {base_url}\n"
        f"- model: {model}\n"
        f"- concurrency: {concurrency}\n"
        f"- requests: {requests}\n"
        f"- max_tokens: {max_tokens}\n"
        f"- long_repeats: {long_repeats}\n"
        f"- warmup: {warmup}\n\n"
        + DEFINITIONS
    )


def parse_scenarios(raw: str, max_tokens: int, long_repeats: int) -> list[ScenarioSpec]:
    specs = {
        "short": ScenarioSpec("short", "короткий", SHORT_PROMPT, max_tokens),
        "long": ScenarioSpec(
            "long", "длинный", build_long_prompt(long_repeats), max_tokens
        ),
    }
    keys = [part.strip() for part in raw.split(",") if part.strip()]
    if not keys:
        raise ValueError("scenarios is empty")
    unknown = [key for key in keys if key not in specs]
    if unknown:
        raise ValueError(
            "unknown scenario {0}; expected short and/or long".format(
                ", ".join(unknown)
            )
        )
    return [specs[key] for key in keys]


def positive_int(name: str, value: int) -> str | None:
    if value < 1:
        return f"{name} must be >= 1"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stream chat completions against a vLLM OpenAI endpoint and "
            "print TTFT, latency, and throughput for short and long prompts."
        )
    )
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("MODEL_NAME", DEFAULT_MODEL))
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--long-repeats", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--scenarios",
        default="short,long",
        help="Comma-separated list: short, long, or short,long.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional path for the same markdown written to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print("API_KEY is required", file=sys.stderr)
        return 2
    problems = [
        positive_int("--concurrency", args.concurrency),
        positive_int("--requests", args.requests),
        positive_int("--max-tokens", args.max_tokens),
        positive_int("--long-repeats", args.long_repeats),
        positive_int("--warmup", args.warmup) if args.warmup != 0 else None,
    ]
    if args.timeout <= 0:
        problems.append("--timeout must be > 0")
    problems = [item for item in problems if item]
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2
    try:
        scenarios = parse_scenarios(args.scenarios, args.max_tokens, args.long_repeats)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    for index in range(args.warmup):
        result = post_chat_stream(
            base_url,
            api_key,
            args.model,
            SHORT_PROMPT,
            min(8, args.max_tokens),
            args.timeout,
        )
        if not result.ok:
            print(f"warmup {index + 1} failed: {result.error}", file=sys.stderr)
            return 1
        print(
            f"warmup {index + 1}/{args.warmup} ok, ttft {fmt_ms(result.ttft_s)} ms",
            file=sys.stderr,
        )

    summaries: list[ScenarioSummary] = []
    for spec in scenarios:
        print(
            f"scenario {spec.key}: n={args.requests} concurrency={args.concurrency} "
            f"chars={len(spec.prompt)}",
            file=sys.stderr,
        )
        summary = run_scenario(
            spec,
            base_url=base_url,
            api_key=api_key,
            model=args.model,
            requests=args.requests,
            concurrency=args.concurrency,
            timeout=args.timeout,
        )
        print(
            f"scenario {spec.key}: {len(summary.successes)}/{len(summary.results)} ok, "
            f"wall {summary.wall_s:.3f} s",
            file=sys.stderr,
        )
        summaries.append(summary)

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report = render_markdown(
        summaries,
        build_preamble(
            base_url=base_url,
            model=args.model,
            started=started,
            concurrency=args.concurrency,
            requests=args.requests,
            max_tokens=args.max_tokens,
            long_repeats=args.long_repeats,
            warmup=args.warmup,
        ),
    )
    sys.stdout.write(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"wrote {args.output}", file=sys.stderr)

    if any(summary.failures for summary in summaries):
        return 1
    if any(not summary.successes for summary in summaries):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
