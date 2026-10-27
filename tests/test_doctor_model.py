"""EmbeddingModel: whether the model cache needs the home directory to be found."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from quarry.doctor_model import EmbeddingModel
from quarry.doctor_preconditions import Need


@pytest.mark.parametrize(
    ("cache", "expected"),
    [
        ("/srv/hf/hub", Need.NONE),
        ("~/.cache/huggingface/hub", Need.HOME),
        (".cache/huggingface/hub", Need.HOME),
    ],
)
def test_the_cache_needs_the_home_only_when_it_is_not_an_absolute_path(
    cache: str, expected: Need
) -> None:
    with patch("huggingface_hub.constants.HF_HUB_CACHE", cache):
        assert EmbeddingModel.needs() == expected
