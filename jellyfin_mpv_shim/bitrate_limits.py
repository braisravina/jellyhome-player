"""The one route from a stored bitrate setting to the number Jellyfin sees.

``local_kbps``/``remote_kbps`` (and a quality-menu override) become
``DeviceProfile.MaxStreamingBitrate``/``MaxStaticBitrate`` in bits per second.
Jellyfin does not reject the values a person might reasonably type for "no
limit". It acts on them, and badly. Measured against 10.11.8 (Phase 2A,
2026-11-25):

- **0 or negative** means no limit. Because the server combines it with the
  administrator's ``RemoteClientBitrateLimit`` as ``min(client, admin)``, it
  also **defeats that limit**. A forced transcode at 0 is encoded at 64 kbps.
- **null** means no client limit, and no video bitrate at all on a transcode.
  An **omitted** field means the server's own 8 Mbps default.
- **Above 2,147,483,647** (int32) fails the whole PlaybackInfo with HTTP 400.

So every value is normalised to a positive integer number of kbps in
``[MIN_KBPS, UNLIMITED_KBPS]``, and only then multiplied by 1000. Unlimited is
the ceiling, ``2147483`` kbps (2,147,483,000 bps): the value local sessions
always sent. It measured as Direct Play of everything, including a file of
unknown bitrate (which the server assumes is 40 Mbps), while every
administrator limit still applies.

Approved rules (2026-11-25):

- A valid value is kept exactly.
- Zero or below becomes Unlimited. The server already treated it as no limit,
  so playback stays as the user saw it, but the administrator's limits apply
  again.
- Above the ceiling becomes Unlimited.
- Anything malformed takes the caller's default: a bool, a non-integral
  number, NaN/inf, text that is not an integer, null, or a container.
"""

import math
import re
from typing import Optional

#: The ceiling, and the explicit spelling of Unlimited. 2147483 * 1000 is the
#: largest multiple of 1000 that fits in a signed 32-bit integer.
UNLIMITED_KBPS = 2147483
#: The smallest positive value. Jellyfin accepts it (its own floor for a
#: transcode is 64 kbps of video), and any positive value a user stored is
#: kept rather than second-guessed.
MIN_KBPS = 1

_INT_TEXT = re.compile(r"[+-]?[0-9]+")


def parse_kbps(value) -> Optional[int]:
    """``value`` as an integer, or None when it is not one.

    Range is not checked here. ``bool`` is refused even though it is an
    ``int``: a JSON ``true`` would otherwise be 1 kbps.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        text = value.strip()
        if _INT_TEXT.fullmatch(text):
            return int(text)
    return None


def normalize_kbps(value, default) -> int:
    """A stored or requested bitrate as a valid kbps value.

    ``default`` is used when ``value`` is malformed, and is normalised the
    same way. A malformed default is a programming error (ValueError).
    """
    kbps = parse_kbps(value)
    if kbps is None:
        kbps = parse_kbps(default)
        if kbps is None:
            raise ValueError("invalid default bitrate %r" % (default,))
    if kbps <= 0 or kbps > UNLIMITED_KBPS:
        return UNLIMITED_KBPS
    return kbps


def wire_bps(value, default) -> int:
    """What goes into the device profile: the normalised kbps times 1000.

    Always in ``[1000, 2147483000]``: positive and within int32.
    """
    return normalize_kbps(value, default) * 1000


def is_valid_input(value) -> bool:
    """Whether the Settings UI may store ``value`` as typed.

    Stricter than :func:`normalize_kbps`: an entry that would be rewritten
    (0, negative, too large) is refused, so the field reports it.
    """
    kbps = parse_kbps(value)
    return kbps is not None and MIN_KBPS <= kbps <= UNLIMITED_KBPS
