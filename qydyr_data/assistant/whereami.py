"""'Фото вместо адреса': the person sends a photo, Claude (vision) names the landmark, the Locator finds
it in the database, and the assistant answers from there. EXIF (GPS, camera, time) is stripped
before the image leaves the server, and the photo itself is never stored."""
from __future__ import annotations

import base64
import io
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..events.venues import PlaceLite

log = logging.getLogger(__name__)

SYSTEM = (
    "Определи по фотографии, у какого места в Астане (Казахстан) стоит человек. Назови достопримечательность, "
    "здание или ТРЦ так, как его называют местные: Байтерек, Хан Шатыр, Дворец мира и согласия (Пирамида), "
    "Нур Алем (сфера EXPO), Mega Silk Way, Astana Opera, мечеть Хазрет Султан, Национальный музей, Ак Орда, "
    "Монумент «Казак Ели» и т. п. Если не уверен — confidence=low и пустой landmark. Ничего не выдумывай: "
    "лучше честно не узнать, чем назвать не то место."
)


class Landmark(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_astana: bool = Field(description="looks like Astana")
    landmark: str = Field(description='the place as locals name it, or ""')
    alternatives: list[str] = Field(description="other possible names of the same place or close guesses")
    confidence: Literal["high", "medium", "low"]
    seen: str = Field(description="what is visible, a few words")


def clean_image(image: bytes, max_side: int = 1568) -> bytes:
    """Re-encode as JPEG without EXIF (no GPS, no camera data), downscaled for the vision model."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(image))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)  # no exif= argument: metadata is dropped
    return out.getvalue()


def identify(image_jpeg: bytes, llm) -> Landmark:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(image_jpeg).decode()}},
        {"type": "text", "text": "Где сделано это фото?"},
    ]
    return llm.structured(system=SYSTEM, user=content, schema=Landmark, max_tokens=600)


def locate(lm: Landmark, locator) -> PlaceLite | None:
    if not lm.in_astana or lm.confidence == "low":
        return None
    for name in [lm.landmark, *lm.alternatives]:
        if name and (p := locator.find_place(name)):
            return p
    return None
