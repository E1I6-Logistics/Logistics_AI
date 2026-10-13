from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..services.map_service import get_public_map_info, pgm_to_png_bytes, world_to_pixel
from ..services.route_graph import list_nodes, load_route_graph


router = APIRouter(prefix="/api", tags=["map-route"])


# 관제 지도 정보를 반환한다.
@router.get("/map/info")
async def get_map_info():
    try:
        return get_public_map_info()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# 지도 이미지를 조회한다.
@router.get("/map/image")
async def get_map_image():
    try:
        return Response(content=pgm_to_png_bytes(), media_type="image/png")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/route/graph")
async def get_route_graph(raw: bool = Query(default=True)):
    """Return the static graph. `raw` remains for the frontend URL contract."""
    del raw
    try:
        return load_route_graph()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# 경로 노드를 조회한다.
@router.get("/route/nodes")
async def get_route_nodes(include_pixel: bool = Query(default=True)):
    try:
        nodes = list_nodes()
        if include_pixel:
            for node in nodes:
                node["pixel_x"], node["pixel_y"] = world_to_pixel(node["x"], node["y"])
        return nodes
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
