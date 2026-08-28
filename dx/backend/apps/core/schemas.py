"""Schema bases for the API (ninja = pydantic).

`StrictSchema` is the base for every *input* schema (`XIn`, `XPatch`): unknown fields are a 422
instead of being silently ignored. The generated client cannot send a misspelled field, but
curl, CI scripts and other consumers can — and a typo in a PATCH would otherwise look like a
successful no-op. Output schemas keep ninja's plain `Schema`/`ModelSchema`.
"""

from ninja import Schema
from pydantic import ConfigDict


class StrictSchema(Schema):
    model_config = ConfigDict(extra="forbid")
