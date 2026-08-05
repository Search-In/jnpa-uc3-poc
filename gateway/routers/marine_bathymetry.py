"""/api/marine/bathymetry — UC-I Marine bathymetry soundings (read-only, additive).

A thin router over :class:`services.marine.bathymetry.BathymetryService`, in the same mould
as gateway/routers/marine_sea_channel.py. Serves the depth soundings ingested through the
SHARED marine upload endpoints (chart PDF or canonical JSON, ``document_type=BATHYMETRY``) —
there is NO separate bathymetry upload endpoint.

    GET /api/marine/bathymetry/surveys                  -> list + filter surveys
    GET /api/marine/bathymetry/surveys/{id}             -> one survey
    GET /api/marine/bathymetry/surveys/{id}/stats       -> counts + depth extents + bbox
    GET /api/marine/bathymetry/soundings                -> paginated soundings (survey_id REQUIRED)

Reads ONLY core.bathymetry_survey / core.bathymetry_sounding. Adds no write path and
changes no existing endpoint. RBAC: covered by the existing ("/api/marine", …) policy.

WHY ``survey_id`` IS REQUIRED ON /soundings
-------------------------------------------
Unlike every other marine entity, this table is huge: one chart is 15k-30k soundings and
the reference corpus is ~190k. An unscoped list would be a trivially-issued full scan and
could return a payload no browser can hold, so the survey scope is mandatory and ``limit``
is capped at _MAX_SOUNDING_LIMIT. Counting is done in SQL via /stats — a caller never needs
to page through soundings to learn how many there are.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..data_mode import data_mode
from ..metrics import REQUESTS
from services.marine.bathymetry import BathymetryService

router = APIRouter(prefix="/api/marine/bathymetry", tags=["marine"])

_API = "marine_bathymetry"
#: Hard ceiling on one sounding page. Deliberately below the 500-1000 used elsewhere in
#: this module family: a sounding row is small but the table is 100x larger.
_MAX_SOUNDING_LIMIT = 500

_service: Optional[BathymetryService] = None


def get_service(request: Request) -> BathymetryService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = BathymetryService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


# --------------------------------------------------------------------------- DTOs
class SurveyOut(BaseModel):
    """core.bathymetry_survey. Every field Optional — a chart's title block may omit any
    of them, and the survey row is created from whatever the parser could read."""
    model_config = ConfigDict(extra="ignore")
    survey_id: Optional[int] = None
    drawing_no: Optional[str] = None
    section_label: Optional[str] = None
    design_depth_m: Optional[float] = None
    survey_start: Optional[str] = None
    survey_end: Optional[str] = None
    survey_vessel: Optional[str] = None
    file_path: Optional[str] = None
    sounding_count: Optional[int] = None


class SurveyListResponse(BaseModel):
    items: List[SurveyOut]
    total: int
    limit: int
    offset: int
    count: int


class BBoxOut(BaseModel):
    min_easting_m: Optional[float] = None
    max_easting_m: Optional[float] = None
    min_northing_m: Optional[float] = None
    max_northing_m: Optional[float] = None


class SurveyStatsOut(BaseModel):
    survey_id: int
    drawing_no: Optional[str] = None
    design_depth_m: Optional[float] = None
    sounding_count: int
    above_design_count: int
    georeferenced_count: int
    min_depth_m: Optional[float] = None
    max_depth_m: Optional[float] = None
    avg_depth_m: Optional[float] = None
    #: Null when the chart carries no grid ticks — 3 of the reference charts are
    #: page-space only, which is valid data, not an error.
    bbox: Optional[BBoxOut] = None


class SoundingOut(BaseModel):
    """core.bathymetry_sounding. easting/northing/lat/lon are Optional BY DESIGN: an
    ungeoreferenced chart yields page coordinates only."""
    model_config = ConfigDict(extra="ignore")
    sounding_id: Optional[int] = None
    survey_id: Optional[int] = None
    easting_m: Optional[float] = None
    northing_m: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    depth_m: Optional[float] = None
    above_design: Optional[bool] = None
    page_x_pt: Optional[float] = None
    page_y_pt: Optional[float] = None
    import_file_id: Optional[int] = None


class SoundingListResponse(BaseModel):
    items: List[SoundingOut]
    total: int
    limit: int
    offset: int
    count: int


def _survey_filters(drawing_no, section, vessel, data_origin=None) -> Dict[str, Any]:
    return {"drawing_no": drawing_no, "section": section, "vessel": vessel,
            "data_origin": data_origin}


# --------------------------------------------------------------------------- endpoints
@router.get("/surveys", response_model=SurveyListResponse,
            summary="List / filter UC-I bathymetry surveys")
async def list_surveys(
    drawing_no: Optional[str] = Query(default=None, description="drawing number contains"),
    section: Optional[str] = Query(default=None, description="section label contains"),
    vessel: Optional[str] = Query(default=None, description="survey vessel contains"),
    sort: str = Query(default="drawing_no"),
    direction: str = Query(default="asc"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    mode: Optional[str] = Depends(data_mode),
    service: BathymetryService = Depends(get_service),
) -> SurveyListResponse:
    res = await service.list_surveys(_survey_filters(drawing_no, section, vessel, mode),
                                     sort=sort, direction=direction,
                                     limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return SurveyListResponse(**res)


@router.get("/surveys/{survey_id}/stats", response_model=SurveyStatsOut,
            summary="Sounding count, depth extents and above-design count for one survey")
async def survey_stats(survey_id: int,
                       mode: Optional[str] = Depends(data_mode),
                       service: BathymetryService = Depends(get_service)) -> SurveyStatsOut:
    res = await service.survey_stats(survey_id, data_origin=mode)
    if res is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "bathymetry_survey_not_found",
                                    "survey_id": survey_id})
    REQUESTS.labels(_API, "ok").inc()
    return SurveyStatsOut(**res)


@router.get("/surveys/{survey_id}", response_model=SurveyOut, summary="One bathymetry survey")
async def get_survey(survey_id: int,
                     mode: Optional[str] = Depends(data_mode),
                     service: BathymetryService = Depends(get_service)) -> SurveyOut:
    res = await service.get_survey(survey_id, data_origin=mode)
    if res is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "bathymetry_survey_not_found",
                                    "survey_id": survey_id})
    REQUESTS.labels(_API, "ok").inc()
    return SurveyOut(**res)


@router.get("/soundings", response_model=SoundingListResponse,
            summary="Paginated soundings for ONE survey (survey_id required)")
async def list_soundings(
    survey_id: int = Query(..., description="REQUIRED — soundings are never listed unscoped"),
    above_design: Optional[bool] = Query(default=None, description="only shoal / only normal"),
    min_depth: Optional[float] = Query(default=None, ge=0),
    max_depth: Optional[float] = Query(default=None, ge=0),
    georeferenced: Optional[bool] = Query(
        default=None, description="true = has easting/northing; false = page-space only"),
    sort: str = Query(default="sounding_id"),
    direction: str = Query(default="asc"),
    limit: int = Query(default=100, ge=1, le=_MAX_SOUNDING_LIMIT),
    offset: int = Query(default=0, ge=0),
    mode: Optional[str] = Depends(data_mode),
    service: BathymetryService = Depends(get_service),
) -> SoundingListResponse:
    if (min_depth is not None and max_depth is not None and min_depth > max_depth):
        REQUESTS.labels(_API, "error").inc()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_depth_range",
                                    "detail": "min_depth must not exceed max_depth"})
    filters = {"above_design": above_design, "min_depth": min_depth,
               "max_depth": max_depth, "georeferenced": georeferenced,
               "data_origin": mode}
    res = await service.list_soundings(survey_id, filters, sort=sort, direction=direction,
                                       limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return SoundingListResponse(**res)
