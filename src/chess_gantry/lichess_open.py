from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

from .errors import ConfigurationError, ValidationError


@dataclass(frozen=True)
class OpenChallenge:
    challenge_id: str
    white_url: str
    black_url: str
    challenge_url: str


def create_open_challenge(
    *,
    name: str = "Chess Gantry Station",
    timeout_s: float = 15.0,
    opener: Callable[..., Any] = urlopen,
) -> OpenChallenge:
    body = urlencode(
        {
            "rated": "false",
            "variant": "standard",
            "name": name,
            "rules": "noRematch,noGiveTime,noClaimWin,noEarlyDraw",
        }
    ).encode("ascii")
    request = Request(
        "https://lichess.org/api/challenge/open",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Relay-Chess-Gantry/0.2",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_s) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ConfigurationError(
            f"could not create token-free Lichess station game: {exc}"
        ) from exc
    challenge_id = value.get("id")
    white_url = value.get("urlWhite") or value.get("whiteUrl")
    black_url = value.get("urlBlack") or value.get("blackUrl")
    challenge_url = value.get("url")
    if not all(
        isinstance(item, str) and item.startswith("https://lichess.org/")
        for item in (challenge_id, white_url, black_url, challenge_url)
    ):
        if not isinstance(challenge_id, str) or not challenge_id:
            raise ValidationError("Lichess open challenge response has no ID")
        for label, item in (
            ("white URL", white_url),
            ("black URL", black_url),
            ("challenge URL", challenge_url),
        ):
            if not isinstance(item, str) or not item.startswith("https://lichess.org/"):
                raise ValidationError(
                    f"Lichess open challenge response has invalid {label}"
                )
    return OpenChallenge(challenge_id, white_url, black_url, challenge_url)


def challenge_game_id(challenge_id: str, *, client: Any) -> Optional[str]:
    try:
        value = client.challenges.show(challenge_id)
    except Exception as exc:
        raise ConfigurationError(
            f"could not inspect Lichess challenge {challenge_id!r}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ValidationError("Lichess returned an invalid challenge status")
    game = value.get("game")
    if isinstance(game, dict):
        game_id = game.get("id") or game.get("gameId")
        if isinstance(game_id, str) and game_id:
            return game_id
    destination = value.get("url")
    status = str(value.get("status", ""))
    if status in {"accepted", "started"} and isinstance(destination, str):
        candidate = destination.rstrip("/").rsplit("/", 1)[-1]
        if 8 <= len(candidate) <= 12 and candidate.isalnum():
            return candidate
    return None
