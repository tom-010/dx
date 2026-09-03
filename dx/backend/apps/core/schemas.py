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


class SourceRef(Schema):
    """A reference to any row, for a client that has its own routing table.

    `type` is a lower-cased model label ("datasets.dataset"), `id` that row's UUID as a string.
    The registries in the SPA (`features/timeline/registry.ts`,
    `features/notifications/registry.ts`) key on `type` to decide the icon and where a click
    goes, so the backend never has to know that `/datasets/$datasetId` exists.

    It lives here rather than in each app's `api.py` because two apps hand out the same pair,
    and pydantic names a component after its class: two identically named schemas in different
    modules collapse into one entry in `openschema.json` — silently, and with whichever
    docstring loaded first. One concept, one name, one definition.
    """

    type: str
    id: str
