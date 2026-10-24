"""Home Assistant 2026.3+ serves the integration's own icon and logo from brand/."""

from pathlib import Path

import pytest

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

BRAND = Path(__file__).resolve().parents[2] / "custom_components" / "xeno_saber_ble" / "brand"
IMAGES = sorted(p.name for p in BRAND.glob("*.png"))

pytestmark = pytest.mark.skipif(
    tuple(int(x) for x in HA_VERSION.split(".")[:2]) < (2026, 3),
    reason="local brand images need Home Assistant 2026.3+",
)


def test_all_brand_images_present() -> None:
    expected = {
        f"{dark}{kind}{size}.png"
        for dark in ("", "dark_")
        for kind in ("icon", "logo")
        for size in ("", "@2x")
    }
    assert set(IMAGES) == expected


@pytest.mark.parametrize("image", IMAGES)
async def test_brand_image_served_locally(hass: HomeAssistant, hass_client, image) -> None:
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()
    resp = await client.get(f"/api/brands/integration/xeno_saber_ble/{image}")
    assert resp.status == 200
    assert await resp.read() == (BRAND / image).read_bytes()
