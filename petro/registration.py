"""Compatibility entry points for integer translation estimation.

The implementation lives in :mod:`petro.alignment`; this module exposes the
same routine under the registration vocabulary used elsewhere in the project.
Coordinate convention: ``reference_xy = primary_xy + (dx, dy)``.
"""
from __future__ import annotations

from .alignment import (  # noqa: F401
    DEFAULT_MAX_SHIFT,
    adopted_shift,
    counterpart,
    overlap_slices,
    suggest_alignment,
    suggest_translation,
)
from .raster import shifted_reference  # noqa: F401

estimate_translation = suggest_translation
register_pair = suggest_translation
align_images = suggest_translation


def _state_or_default(primary, state):
    if state is not None:
        return state
    from .domain import default_state
    return default_state((primary.shape[1], primary.shape[0]))


def estimate_shift(primary, reference, state=None, max_shift=DEFAULT_MAX_SHIFT, **kwargs):
    """Return just the best integer ``(dx, dy)`` mapping pair."""
    result = suggest_translation(
        primary, reference, _state_or_default(primary, state), max_shift, **kwargs
    )
    return result["dx"], result["dy"]


def best_shift(primary, reference, state=None, max_shift=DEFAULT_MAX_SHIFT, **kwargs):
    """Return the full suggestion record (alias of :func:`suggest_translation`)."""
    return suggest_translation(
        primary, reference, _state_or_default(primary, state), max_shift, **kwargs
    )
