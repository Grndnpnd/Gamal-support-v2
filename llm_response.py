"""
llm_response.py
---------------
A str subclass that carries LLM call metadata (token usage, which provider
answered) alongside the answer text.

Why a str subclass: the bot's LLM calls historically return a plain `str`,
and there are call sites all over bot.py and api_server.py that treat the
result as a string — slicing it, checking length, concatenating footers.
Changing the return type to a tuple or dataclass would break every one of
them. An LLMResponse IS a str — it passes isinstance(x, str), supports
slicing, concatenation, everything — so all existing call sites keep working
untouched. The only new thing is the .usage / .provider attributes that the
stats logging reads.

Concatenation caveat: `LLMResponse("hi") + " there"` produces a plain str,
not an LLMResponse — Python's str.__add__ returns str. That's fine: the bot
captures token data from the LLMResponse immediately after the call, before
any footer/greeting concatenation happens. The metadata is read off the
fresh return value, not off a later derived string.
"""

from typing import Optional


class LLMResponse(str):
    """
    str subclass carrying optional LLM-call metadata.

    Attributes (all optional, default None/0):
      provider    - 'bankr' | 'ollama_cloud' | None
      tokens_in   - prompt tokens, int
      tokens_out  - completion tokens, int
      ok          - False if this represents a provider failure rather than
                    a real answer (the router uses this internally)
    """

    # __new__ because str is immutable — the string value must be set at
    # construction time, before __init__ would run.
    def __new__(
        cls,
        text: str,
        provider: Optional[str] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        ok: bool = True,
    ):
        obj = super().__new__(cls, text)
        obj.provider   = provider
        obj.tokens_in  = tokens_in
        obj.tokens_out = tokens_out
        obj.ok         = ok
        return obj

    @property
    def usage(self) -> dict:
        """Token usage as a dict, convenient for logging."""
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out}
