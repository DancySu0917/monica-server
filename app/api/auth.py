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
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.config import settings
from app.utils.rate_limit import limiter

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
@limiter.limit("10/minute")   # 防暴力枚举：每 IP 每分钟最多 10 次登录
async def wx_login(request: Request, body: WxLoginRequest):
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

    开发模式绕过（两种方式）：
    1. code == "monica-code"：固定测试账号，openid=monica_test_user，免去每次获取微信 code 的流程
    2. code 以 "dev_" 开头：自定义 openid，如 dev_alice → openid=alice
    以上两种方式均不调用微信接口，仅用于本地开发测试。
    """
    # ── 开发模式绕过（仅在 DEV_MODE=true 时生效，生产环境必须关闭）────
    if settings.DEV_MODE:
        if code == "monica-code":
            logger.info("[Auth] 开发模式：使用固定测试 code 登录，openid=monica_test_user")
            return "monica_test_user"
        if code.startswith("dev_"):
            dev_openid = code[4:] or "dev_test_openid"
            logger.info(f"[Auth] 开发模式：自定义 openid={dev_openid}")
            return dev_openid

    if not settings.WX_APPID or not settings.WX_SECRET:
        raise HTTPException(status_code=503, detail="微信登录未配置，请联系管理员")

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
