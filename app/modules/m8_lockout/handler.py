from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.modules.m8_lockout.ledger_service import inspect, submit_resample
from app.quality import token
from app.errors import BusinessError

router = APIRouter(prefix="/api/lockouts", tags=["复采闭锁"])


class ResampleSubmission(BaseModel):
    source: str = ""
    operator: str = ""
    readings: list = []
    calibrations: list | None = None


@router.get("/{source}")
def overview(request: Request, source: str):
    return inspect(request.app.state.ledger, source)


@router.post("/{source}/resample")
def resample(request: Request, source: str, data: ResampleSubmission):
    try:
        operator = token(data.operator)
        if len(operator) > 32:
            raise ValueError
    except ValueError as error:
        raise BusinessError("OPERATOR_REQUIRED", "请输入 1 至 32 字的复采操作人") from error
    payload = {"readings": data.readings}
    if data.calibrations is not None:
        payload["calibrations"] = data.calibrations
    return submit_resample(request.app.state.ledger, source, payload, operator)
