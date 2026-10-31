import yaml
from fastapi import APIRouter, HTTPException

from app.config import MAP_IMAGE, MAP_YAML
from app.schemas import MapInfo

router = APIRouter(prefix="/map", tags=["map"])


@router.get("", response_model=MapInfo)
def get_map() -> MapInfo:
    if not MAP_IMAGE.exists() or not MAP_YAML.exists():
        raise HTTPException(status_code=404, detail="map not found")
    meta = yaml.safe_load(MAP_YAML.read_text(encoding="utf-8"))
    origin_x, origin_y, origin_yaw = (list(meta["origin"]) + [0.0, 0.0, 0.0])[:3]
    version = int(MAP_IMAGE.stat().st_mtime)
    return MapInfo(
        image_url=f"/maps/{MAP_IMAGE.name}?v={version}",
        resolution=meta["resolution"],
        origin_x=origin_x,
        origin_y=origin_y,
        origin_yaw=origin_yaw,
    )
