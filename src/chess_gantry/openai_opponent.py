from __future__ import annotations

import os
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .errors import ConfigurationError, ValidationError


MODEL = "gpt-5.6-sol"


class OpponentMove(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    uci: str = Field(
        min_length=4, max_length=5, pattern=r"^[a-h][1-8][a-h][1-8][qrbn]?$"
    )
    rationale: str = Field(min_length=1, max_length=240)
    plan: str = Field(min_length=1, max_length=160)


class SolChessOpponent:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: str = MODEL,
        client: Any = None,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        if client is None and not key:
            raise ConfigurationError("OPENAI_API_KEY is required for Play vs OpenAI")
        if client is None:
            import httpx
            from openai import OpenAI

            client = OpenAI(
                api_key=key,
                timeout=httpx.Timeout(
                    40.0, connect=5.0, read=35.0, write=30.0, pool=5.0
                ),
                max_retries=1,
            )
        self.client = client
        self.model = model

    def choose_move(
        self,
        board: Any,
        *,
        style: Literal["balanced", "aggressive", "positional", "creative"] = "balanced",
    ) -> OpponentMove:
        legal = tuple(move.uci() for move in board.legal_moves)
        if not legal:
            raise ValidationError("OpenAI cannot move because the game is over")
        history = []
        replay = board.copy()
        while replay.move_stack:
            move = replay.pop()
            history.append(move.uci())
        history.reverse()
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {
                    "role": "developer",
                    "content": (
                        "You are the chess opponent controlling one side of a physical board. "
                        "Choose exactly one move from the supplied legal UCI allowlist. Never "
                        "return a move outside the allowlist. Prefer sound practical chess and "
                        "respect the requested style. Keep rationale and plan concise."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"FEN: {board.fen()}\n"
                        f"Style: {style}\n"
                        f"Move history UCI: {' '.join(history) or '(none)'}\n"
                        f"Legal UCI allowlist: {' '.join(legal)}"
                    ),
                },
            ],
            text_format=OpponentMove,
            reasoning={"effort": "medium"},
            max_output_tokens=350,
            store=False,
        )
        result = response.output_parsed
        if response.status != "completed" or result is None:
            raise ValidationError(
                f"OpenAI opponent did not complete: {response.status}"
            )
        if result.uci not in legal:
            raise ValidationError(
                f"OpenAI returned {result.uci}, which is not in the server legal-move allowlist"
            )
        return result
