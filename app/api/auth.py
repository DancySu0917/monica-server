"""
微信小程序登录 + JWT 签发

流程：
  1. 前端调用 wx.login() 获取 code
  2. 调用本接口 POST /auth/wx_login {code}
  3. 换取 openid → 签发 JWT
"""
import datetime
import logging

import httpx
import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.config import settings
from app.database import SessionLocal
from app.models.task import Task  # 仅用于 health check

router = APIRouter(prefix="/auth", tags=["Auth"])
logger = logging.getLogger(__name__)


# ── 请求/响应 Schema ───────────────────────────────────────────────

class WxLoginRequest(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("code 不能为空")
        return v.strip()


class WxLoginResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    expires_in:   int = 7 * 24 * 3600   # 7 天，单位秒


# ── 路由 ──────────────────────────────────────────────────────────

@router.post("/wx_login", response_model=WxLoginResponse, summary="微信小程序登录")
async def wx_login(body: WxLoginRequest):
    """
    凭 wx.login() 返回的临时 code 换取 openid，然后签发 JWT。
    """
    openid = await _exchange_openid(body.code)

    # 签发 JWT（使用 timezone-aware datetime 避免 Python 3.12+ 废弃警告）
    now    = datetime.datetime.now(datetime.timezone.utc)
    expire = now + datetime.timedelta(days=7)
    payload = {
        "sub": openid,
        "iat": now,
        "exp": expire,
        "scope": "user",
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

    return WxLoginResponse(access_token=token)


async def _exchange_openid(code: str) -> str:
    """
    调用微信 jscode2session 接口获取 openid。
    在测试/开发环境（无真实 appid）返回 mock openid。
    """
    if not settings.WX_APPID or not settings.WX_SECRET:
        # DEV 模式：直接用 code 作为 openid（方便联调）
        logger.warning("[Auth] WX_APPID/WX_SECRET 未配置，使用开发模式 mock openid")
        return f"dev_{code[:20]}"

    url = (
        f"https://api.weixin.qq.com/sns/jscode2session"
        f"?appid={settings.WX_APPID}"
        f"&secret={settings.WX_SECRET}"
        f"&js_code={code}"
        f"&grant_type=authorization_code"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error(f"[Auth] 微信接口调用失败: {e}")
        raise HTTPException(status_code=502, detail="微信接口暂时不可用，请稍后重试")

    if "errcode" in data and data["errcode"] != 0:
        logger.warning(f"[Auth] 微信登录失败: {data}")
        raise HTTPException(
            status_code=400,
            detail=f"微信登录失败: {data.get('errmsg', '未知错误')}（errcode={data.get('errcode')}）"
        )

    openid = data.get("openid")
    if not openid:
        raise HTTPException(status_code=500, detail="微信接口返回数据异常，缺少 openid")

    return openid
