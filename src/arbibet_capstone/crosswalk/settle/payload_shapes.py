"""The one constant `settlement.py` imports from silver's survey layer.

Vendored as a two-line module rather than dragging in `survey/payload_shapes`,
which exists to profile bronze payloads and has no business in a settlement
path. Copied verbatim from arbibet-silver so the value cannot drift by
paraphrase.
"""

from typing import Final

PLAYER_ID_PREFIXES: Final = ("sr:player:", "pre:playerprops:")
