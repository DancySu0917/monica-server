"""
FastAPI 依赖注入：JWT 鉴权、数据库 session
"""
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings
from app.database import SessionFactory

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """解析 Bearer JWT，返回 payload（含 user_id 等）"""
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token 无效：缺少 sub 字段",
            )
        return {"user_id": user_id, **payload}
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 已过期，请重新登录",
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token 验证失败: {str(e)}",
        )


def get_db():
    """SQLAlchemy session 生成器（普通 HTTP 路由使用）"""
    db = SessionFactory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
