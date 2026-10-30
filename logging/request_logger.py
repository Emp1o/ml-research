# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""SafeBoard replacement for vLLM's RequestLogger.

Records that a model call happened. Prompt text, prompt token ids, request
bodies, and generated text are not written to the log.
"""

import logging
from collections.abc import Sequence

import torch

from vllm.entrypoints.pooling.typing import AnyPoolingRequest
from vllm.entrypoints.serve.engine.typing import AnyRequest
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.pooling_params import PoolingParams
from vllm.sampling_params import BeamSearchParams, SamplingParams

logger = init_logger(__name__)


class RequestLogger:
    def __init__(self, *, max_log_len: int | None) -> None:
        self.max_log_len = max_log_len

        if not logger.isEnabledFor(logging.INFO):
            logger.warning_once(
                "Request logging is enabled, but the log level is above INFO. "
                "The fact of a model call will not be logged."
            )

    def log_inputs(
        self,
        request_id: str,
        prompt: str | None,
        prompt_token_ids: list[int] | None,
        prompt_embeds: torch.Tensor | None,
        params: SamplingParams | PoolingParams | BeamSearchParams | None,
        lora_request: LoRARequest | None,
    ) -> None:
        del prompt, prompt_embeds, params, lora_request
        prompt_tokens = len(prompt_token_ids) if prompt_token_ids is not None else 0
        logger.info(
            "Received request %s: model call, prompt_tokens=%s",
            request_id,
            prompt_tokens,
        )

    def log_request_body(self, request: AnyRequest | AnyPoolingRequest) -> None:
        logger.info(
            "Received request %s: body omitted, type=%s",
            getattr(request, "request_id", "N/A"),
            type(request).__name__,
        )

    def log_outputs(
        self,
        request_id: str,
        outputs: str,
        output_token_ids: Sequence[int] | None,
        finish_reason: str | None = None,
        is_streaming: bool = False,
        delta: bool = False,
    ) -> None:
        del outputs
        if is_streaming:
            stream_info = " streaming-delta" if delta else " streaming-complete"
        else:
            stream_info = ""
        output_tokens = len(output_token_ids) if output_token_ids is not None else 0
        logger.info(
            "Generated response %s:%s output omitted, output_tokens=%s, finish_reason=%s",
            request_id,
            stream_info,
            output_tokens,
            finish_reason,
        )
