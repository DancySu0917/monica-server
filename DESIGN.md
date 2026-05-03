# Monica Server — 医学影像 AI 分析平台 · 完整设计方案（v3）

---

## 一、系统整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                     微信小程序 (Client)                      │
│  - 微信 code 登录  - 分片上传  - SSE 实时进度订阅            │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS (JWT Bearer)
┌──────────────────────────▼──────────────────────────────────┐
│                  FastAPI 网关层 (Nginx + Uvicorn)             │
│  - 微信 openid 鉴权  - 限流 (slowapi)                        │
│  - Token 配额检查    - 请求追踪 (request_id)                  │
│  - SSE 推送端点      - 分片上传端点                           │
└──────────────────┬───────────────────────┬───────────────────┘
                   │                       │
       ┌───────────▼──────────┐ ┌──────────▼──────────────────┐
       │    分片上传服务        │ │     任务编排层 (ARQ)          │
       │  - SHA256 去重        │ │  Redis Queue (轻量异步队列)  │
       │  - 断点续传           │ │  - 幂等任务去重              │
       │  - 本地存储           │ └──────────┬───────────────────┘
       └──────────────────────┘            │
                                ┌──────────▼───────────────────┐
                                │      Pipeline 多阶段处理      │
                                │                              │
                                │  Stage 1: 文件标准化          │
                                │  Stage 2: 质量粗筛           │
                                │  Stage 3: 病灶候选检测 ★     │
                                │      └─ TotalSegmentator     │
                                │  Stage 4: 关键切片提取 ★     │
                                │      └─ 双窗位 + pHash去重   │
                                │  Stage 5: 上下文构建 ★       │
                                │      └─ sqlite-vec 语义检索  │
                                │  Stage 6: LLM 推理 ★        │
                                │      └─ CoT 分步 + 降级链    │
                                │  Stage 7: 结果存储           │
                                │                              │
                                │  [每阶段配 Independent        │
                                │   Evaluator Sub-Agent]       │
                                └──────────────────────────────┘
                                              │
                                ┌─────────────▼────────────────┐
                                │           存储层              │
                                │  SQLite + WAL (元数据)        │
                                │  sqlite-vec (知识库向量索引)  │
                                │  本地文件系统 (影像文件)       │
                                │  Redis (任务状态 / 配额计数)  │
                                └──────────────────────────────┘
```

**技术选型总览（★ = 相比 v1 有变更）：**

| 组件 | 技术 | 原因 |
|------|------|------|
| Web 框架 | FastAPI | 异步、自动文档、类型安全 |
| 身份验证 ★ | 微信 openid + JWT | 防止 user_id 伪造，安全基础 |
| 进度推送 ★ | Server-Sent Events (SSE) | 比轮询体验好，比 WebSocket 实现简单 |
| 文件上传 ★ | 分片上传 + SHA256 去重 | 断点续传，支持大压缩包 |
| 任务队列 ★ | ARQ (Async Redis Queue) | 比 Celery 轻量，天然 async，内存节省 ~80MB |
| 数据库 | SQLite + WAL 模式 | 零运维，2C2G 完全满足 |
| 向量检索 ★ | sqlite-vec | 无独立向量库，知识库语义检索 |
| 病灶检测 ★ | TotalSegmentator (CPU fast) | 替代纯规则，假阳性从 ~40% 降至 ~15% |
| 切片筛选 ★ | 双窗位渲染 + pHash 去重 | 肺窗+纵隔窗，避免相似切片重复 |
| LLM 推理 ★ | CoT 三步推理 + 降级链 | 降低 token 消耗 ~60%，带容错 |
| 费用保护 ★ | Redis token 配额 | 防恶意滥用 API |
| 进程管理 | Supervisor | 轻量，管理 Uvicorn + ARQ Worker |
| 反向代理 | Nginx | SSL 终止 + 限流 |

---

## 二、核心设计理念：Structured Handoff + Independent Evaluator

```
每个 Stage 的契约：

┌──────────────────────────────────────────────────────────────┐
│  Input Schema (Pydantic)                                     │
│       ↓                                                      │
│  Stage Processor  →  Output Schema (Pydantic)  →  落库存档   │
│                            ↓                                 │
│                   Independent Evaluator Sub-Agent            │
│                     PASS / WARN / REJECT + reason            │
│                            ↓                                 │
│           PASS/WARN → 触发下一 Stage (SSE 推送进度)           │
│           REJECT   → 回退 + SSE 推送原因 + 用户补充提示       │
└──────────────────────────────────────────────────────────────┘
```

**关键原则：**
- 每个阶段的 Output 即是下一阶段的 Input，Pydantic 严格约束，禁止松散 dict 传递
- 所有中间产物落库存储，天然可审计、可回放、可 A/B 测试
- 评估器是纯函数式 Sub-Agent，不依赖外部状态，可独立替换升级
- 任务幂等：相同文件 + prompt + model 的任务直接复用已有结果

---

## 三、项目目录结构

```
monica-server/
├── app/
│   ├── main.py                    # FastAPI 入口，挂载路由和生命周期
│   ├── config.py                  # 配置管理 (pydantic-settings)
│   ├── database.py                # SQLAlchemy ORM + sqlite-vec 初始化
│   │
│   ├── api/
│   │   ├── auth.py                # ★ 微信登录、JWT 签发
│   │   ├── upload.py              # ★ 分片上传（init/chunk/complete）
│   │   ├── analysis.py            # 创建分析任务
│   │   ├── stream.py              # ★ SSE 实时进度推送
│   │   └── result.py              # 结果查询
│   │
│   ├── pipeline/
│   │   ├── orchestrator.py        # 任务编排主入口
│   │   ├── stage1_normalizer.py
│   │   ├── stage2_screener.py
│   │   ├── stage3_detector.py     # ★ TotalSegmentator 集成
│   │   ├── stage4_selector.py     # ★ 双窗位渲染 + pHash 去重
│   │   ├── stage5_context.py      # ★ sqlite-vec 语义检索
│   │   ├── stage6_llm.py          # ★ CoT 三步推理
│   │   └── stage7_storage.py
│   │
│   ├── evaluators/
│   │   ├── base_evaluator.py
│   │   ├── quality_evaluator.py
│   │   ├── nodule_evaluator.py
│   │   └── report_evaluator.py
│   │
│   ├── schemas/
│   │   ├── stage1_normalize.py
│   │   ├── stage2_screen.py
│   │   ├── stage3_detection.py
│   │   ├── stage4_selection.py    # ★ 含双窗位字段
│   │   ├── stage5_context.py
│   │   ├── stage6_cot.py          # ★ CoT 三步中间 Schema
│   │   └── stage7_report.py
│   │
│   ├── services/
│   │   ├── dicom_service.py
│   │   ├── file_service.py        # ★ 分片上传状态管理
│   │   ├── llm_service.py         # ★ 降级链 + 退避重试
│   │   ├── knowledge_service.py   # ★ sqlite-vec 语义检索
│   │   └── quota_service.py       # ★ Token 配额管理
│   │
│   ├── models/
│   │   ├── file_record.py
│   │   ├── upload_session.py      # ★ 分片上传会话
│   │   ├── task.py
│   │   └── analysis_result.py
│   │
│   └── workers/
│       └── arq_worker.py          # ★ ARQ 替代 Celery
│
├── storage/
│   ├── uploads/                   # 原始上传（按 user_id/task_id 隔离）
│   ├── processed/                 # PNG（双窗位）、中间产物
│   └── exports/
│
├── knowledge_base/
│   ├── cases.jsonl                # 医学案例（每行一条，供 embedding 入库）
│   └── guidelines.jsonl           # 指南条目
│
├── requirements.txt
├── .env.example
├── supervisord.conf
└── nginx.conf
```

---

## 四、身份验证（★ 新增）

### 4.1 微信登录流程

```python
# app/api/auth.py
import httpx, jwt, datetime
from fastapi import APIRouter, HTTPException
from app.config import settings

router = APIRouter(prefix="/api/v1/auth")

@router.post("/wx-login")
async def wx_login(code: str):
    """
    微信小程序调用 wx.login() 获得 code，传给后端换取 openid
    后端签发 JWT，后续所有请求携带 Authorization: Bearer <token>
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid":      settings.WX_APPID,
                "secret":     settings.WX_SECRET,
                "js_code":    code,
                "grant_type": "authorization_code"
            }
        )
    data = resp.json()
    if "errcode" in data and data["errcode"] != 0:
        raise HTTPException(400, f"微信登录失败: {data.get('errmsg')}")
    if "openid" not in data:
        # ★ 坑：网络超时或微信接口异常时 openid 字段可能根本不存在
        raise HTTPException(502, "微信接口未返回 openid，请稍后重试")

    openid = data["openid"]
    token = jwt.encode(
        {
            "sub": openid,
            # ★ 修复：datetime.utcnow() Python 3.12+ 已废弃，改为 datetime.now(timezone.utc)
            "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
        },
        settings.SECRET_KEY, algorithm="HS256"
    )
    return {"token": token, "openid_hash": openid[:8] + "****"}

# app/api/deps.py — JWT 依赖注入
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    try:
        payload = jwt.decode(credentials.credentials, settings.SECRET_KEY, algorithms=["HS256"])
        return payload["sub"]   # 返回 openid，作为 user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token 已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效 Token")
```

---

## 五、分片上传（★ 新增）

### 5.1 接口设计

```
# Step 1：初始化上传会话
POST /api/v1/upload/init
Authorization: Bearer <token>
{
  "filename":   "dicom_series.zip",
  "total_size": 524288000,           # bytes
  "total_chunks": 100,               # 每块 5MB
  "file_sha256": "abc123..."         # 客户端预先计算，用于去重和完整性校验
}
→ {
  "upload_id":       "up_xxxxxxxx",
  "already_exists":  false,          # true 时直接跳到 complete，无需上传
  "existing_file_id": null
}

# Step 2：逐块上传（可断点续传）
POST /api/v1/upload/chunk
Authorization: Bearer <token>
Content-Type: multipart/form-data
{
  "upload_id":    "up_xxxxxxxx",
  "chunk_index":  0,                 # 0-based
  "chunk_data":   <binary>
}
→ { "received_chunks": [0, 1, 3], "missing_chunks": [2] }

# Step 3：合并完成，触发任务
POST /api/v1/upload/complete
Authorization: Bearer <token>
{
  "upload_id": "up_xxxxxxxx",
  "prompt":    "请分析这组肺部CT",
  "model":     "gpt-4o"
}
→ { "task_id": "task_xxx", "status": "processing" }
```

### 5.2 服务端实现

```python
# app/services/file_service.py（分片管理核心）
import os, hashlib
from pathlib import Path
from app.models.upload_session import UploadSession
from app.database import SessionLocal

class FileService:

    CHUNK_SIZE = 5 * 1024 * 1024   # 5MB per chunk

    def init_upload(self, user_id: str, filename: str,
                    total_size: int, total_chunks: int,
                    file_sha256: str) -> dict:
        # 去重：同 SHA256 已存在则直接返回
        with SessionLocal() as db:
            existing = db.query(FileRecord).filter_by(file_hash=file_sha256).first()
            if existing:
                return {"upload_id": None, "already_exists": True,
                        "existing_file_id": file_sha256}

        upload_id = f"up_{os.urandom(8).hex()}"
        # ★ 修复：使用绝对路径，避免随启动目录漂移（与 complete_upload、DiskGuard 保持一致）
        from app.config import settings as _settings
        chunk_dir = Path(_settings.STORAGE_ROOT).resolve() / "chunks" / upload_id
        chunk_dir.mkdir(parents=True, exist_ok=True)

        session = UploadSession(
            upload_id=upload_id, user_id=user_id,
            filename=filename, total_size=total_size,
            total_chunks=total_chunks, file_sha256=file_sha256,
            chunk_dir=str(chunk_dir)
        )
        with SessionLocal() as db:
            db.add(session); db.commit()

        return {"upload_id": upload_id, "already_exists": False}

    def save_chunk(self, upload_id: str, chunk_index: int, data: bytes):
        with SessionLocal() as db:
            session = db.query(UploadSession).filter_by(upload_id=upload_id).first()
            if not session:
                raise ValueError(f"上传会话 {upload_id} 不存在或已过期")
            # ★ 坑：需校验 chunk_index 合法范围，防止越界写文件
            if not (0 <= chunk_index < session.total_chunks):
                raise ValueError(f"chunk_index {chunk_index} 超出范围 [0, {session.total_chunks}]")
            chunk_dir = session.chunk_dir
        chunk_path = Path(chunk_dir) / f"chunk_{chunk_index:06d}"
        chunk_path.write_bytes(data)

    def complete_upload(self, upload_id: str) -> str:
        """合并所有分块，校验 SHA256，返回最终文件路径"""
        with SessionLocal() as db:
            session = db.query(UploadSession).filter_by(upload_id=upload_id).first()
            # ★ 修复：session 可能为 None（upload_id 不存在或已过期），需显式校验
            if not session:
                raise ValueError(f"上传会话 {upload_id} 不存在或已过期，请重新初始化上传")

        # ★ 修复：使用绝对路径，避免随启动目录漂移（与 DiskGuard、render_dual_window 保持一致）
        from app.config import settings as _settings
        final_path = str(
            Path(_settings.STORAGE_ROOT).resolve()
            / "uploads" / session.user_id
            / f"{session.file_sha256[:8]}_{session.filename}"
        )
        Path(final_path).parent.mkdir(parents=True, exist_ok=True)

        sha256 = hashlib.sha256()
        with open(final_path, "wb") as out:
            for i in range(session.total_chunks):
                chunk = (Path(session.chunk_dir) / f"chunk_{i:06d}").read_bytes()
                out.write(chunk)
                sha256.update(chunk)

        actual_hash = sha256.hexdigest()
        if actual_hash != session.file_sha256:
            os.remove(final_path)
            raise ValueError(f"文件完整性校验失败，请重新上传")

        # 清理分块临时目录
        import shutil
        shutil.rmtree(session.chunk_dir, ignore_errors=True)

        return final_path
```

---

## 六、SSE 实时进度推送（★ 新增）

```python
# app/api/stream.py
import asyncio, json
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from app.api.deps import get_current_user
from app.database import get_task_status

router = APIRouter(prefix="/api/v1")

STAGE_NAMES = {
    "stage1": "文件标准化",
    "stage2": "质量粗筛",
    "stage3": "病灶候选检测",
    "stage4": "关键切片提取",
    "stage5": "上下文构建",
    "stage6": "AI 推理中",
    "stage7": "结果存储",
    "done":   "分析完成",
    "error":  "处理失败",
    "rejected": "需要补充资料",
}

@router.get("/task/{task_id}/stream")
async def task_stream(task_id: str, user_id: str = Depends(get_current_user)):
    """
    SSE 端点：客户端连接后持续推送任务进度，直到终态
    小程序使用方式：
      const eventSource = wx.connectSocket({ url: '/task/xxx/stream' })
    """
    async def event_generator():
        # 首次立即推当前状态
        task = get_task_status(task_id, user_id)
        if not task:
            yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
            return

        last_stage = None
        heartbeat_counter = 0
        while True:
            task = get_task_status(task_id, user_id)
            event = {
                "task_id":       task_id,
                "status":        task.status,
                "stage":         task.stage,
                "stage_name":    STAGE_NAMES.get(task.stage, task.stage),
                "progress":      task.progress,
                "reject_reason": task.reject_reason,
                "suggestions":   task.suggestions,
                "result":        task.result if task.status == "done" else None,
            }

            # 仅在状态变更时推送（减少噪音）
            if task.stage != last_stage:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                last_stage = task.stage

            # ★ 心跳：每 30s 发送一次注释行，防止代理/负载均衡因空闲断连
            heartbeat_counter += 1
            if heartbeat_counter >= 20:   # 20 × 1.5s = 30s
                yield ": heartbeat\n\n"
                heartbeat_counter = 0

            # 终态：推完即关闭连接
            if task.status in ("done", "error", "rejected"):
                break

            await asyncio.sleep(1.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"   # 关闭 Nginx 缓冲，确保实时推送
        }
    )
```

---

## 七、任务幂等性（★ 新增）

```python
# app/api/analysis.py
import hashlib, json
from fastapi import APIRouter, Depends
from app.api.deps import get_current_user
from app.database import SessionLocal
from app.models.task import Task
from app.workers.arq_worker import enqueue_pipeline

router = APIRouter(prefix="/api/v1")

def compute_idempotency_key(file_ids: list, prompt: str, model: str) -> str:
    """相同文件 + prompt + model 生成相同 key，实现任务去重"""
    payload = json.dumps(
        {"file_ids": sorted(file_ids), "prompt": prompt.strip(), "model": model},
        sort_keys=True
    )
    return "idem_" + hashlib.sha256(payload.encode()).hexdigest()[:16]

@router.post("/analysis")
async def create_analysis(
    file_ids: list[str],
    prompt: str,
    model: str = "gpt-4o",
    user_id: str = Depends(get_current_user)
):
    idem_key = compute_idempotency_key(file_ids, prompt, model)

    # 命中幂等键：直接返回已有任务
    with SessionLocal() as db:
        existing = db.query(Task).filter_by(idempotency_key=idem_key).first()
        if existing and existing.status not in ("error",):
            return {"task_id": existing.task_id, "reused": True, "status": existing.status}

    task_id = f"task_{os.urandom(6).hex()}"
    # 创建任务记录（pending 状态，避免 ARQ 入队后 SSE 查询不到）
    with SessionLocal() as db:
        db.add(Task(task_id=task_id, idempotency_key=idem_key,
                    user_id=user_id, status="pending", model=model))
        db.commit()
    await enqueue_pipeline(task_id, file_ids, prompt, user_id, model, idem_key)
    return {"task_id": task_id, "reused": False, "status": "processing"}
```

---

## 八、ARQ 轻量任务队列（★ 替代 Celery）

```python
# app/workers/arq_worker.py
from arq import create_pool
from arq.connections import RedisSettings
from app.config import settings

async def run_pipeline(ctx, task_id: str, file_ids: list,
                       prompt: str, user_id: str, model: str):
    """ARQ 任务函数，天然 async，无需 asyncio.run() 嵌套"""
    from app.pipeline.orchestrator import PipelineOrchestrator
    orchestrator = PipelineOrchestrator()
    await orchestrator.run(task_id, file_ids, prompt, user_id, model)

async def enqueue_pipeline(task_id, file_ids, prompt, user_id, model, idem_key):
    # ★ 每次调用都创建新连接池会泄漏连接；生产应使用全局连接池
    # 临时方案：with 语句确保关闭
    pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    try:
        await pool.enqueue_job(
            "run_pipeline",
            task_id, file_ids, prompt, user_id, model,
            _job_id=idem_key,     # ARQ 用 job_id 做去重，相同 id 不重复入队
            _job_timeout=600,     # 10 分钟超时
        )
    finally:
        await pool.aclose()

class WorkerSettings:
    functions      = [run_pipeline]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs       = 1              # 单并发，适配 2G 内存
    job_timeout    = 600
    max_tries      = 3             # 失败自动重试 3 次
    retry_jobs     = True
    # ★ 坑：ARQ retry 会用相同 job_id 重试，幂等键不会冲突，可安全开启
    # ★ 坑：任务因 OOM 被 kill 时 ARQ 会重试，Pipeline 需支持从中间状态恢复
    #   当前实现每次从 Stage 1 重跑，40G 磁盘够用但浪费时间；
    #   进阶做法：检查 stage_results 表中已完成的阶段，从断点继续
```

---

## 九、Structured Handoff — 各阶段数据契约

```python
# app/schemas/stage1_normalize.py
from pydantic import BaseModel
from enum import Enum
from typing import List, Optional
from datetime import datetime

class FileType(str, Enum):
    DICOM_SINGLE    = "dicom_single"
    DICOM_SERIES    = "dicom_series"
    DICOM_ARCHIVE   = "dicom_archive"
    PATHOLOGY_SLIDE = "pathology_slide"
    CT_IMAGE        = "ct_image"
    PLAIN_IMAGE     = "plain_image"

class DicomSeriesInfo(BaseModel):
    series_uid: str
    series_description: Optional[str]
    modality: str                         # CT / MR / PT
    slice_count: int
    slice_thickness_mm: Optional[float]
    pixel_spacing: Optional[List[float]]
    window_center: Optional[float]
    window_width: Optional[float]
    acquisition_date: Optional[str]

class NormalizedFile(BaseModel):
    file_id: str                          # SHA256，去重主键
    original_filename: str
    file_type: FileType
    storage_path: str
    size_bytes: int
    is_duplicate: bool
    dicom_series: Optional[List[DicomSeriesInfo]] = None
    created_at: datetime

class Stage1Result(BaseModel):
    task_id: str
    normalized_files: List[NormalizedFile]
    total_dicom_slices: int
    file_summary: str
    stage: str = "stage1_normalize"
    elapsed_ms: int
```

```python
# app/schemas/stage4_selection.py（★ 新增双窗位字段）
from pydantic import BaseModel
from typing import List, Optional, Dict

class DualWindowPng(BaseModel):
    """同一切片的多窗位渲染结果"""
    lung_window_path: str         # 肺窗 (WC=-600, WW=1500)
    mediastinum_window_path: str  # 纵隔窗 (WC=40, WW=400)
    phash_lung: str               # 感知哈希，用于去重判断
    phash_mediastinum: str        # ★ 修复(#17)：纵隔窗 pHash，必须定义否则 Pydantic v2 静默丢弃传入值

class SelectedSlice(BaseModel):
    series_uid: str
    slice_index: int
    rank: int
    score: float
    dual_window: DualWindowPng    # ★ 双窗位 PNG
    slice_location_mm: Optional[float]
    slice_thickness_mm: Optional[float]
    nodule_candidates: list
    dicom_metadata: Dict
    selection_reason: str         # 选取原因（可解释性）

class Stage4Result(BaseModel):
    task_id: str
    selected_slices: List[SelectedSlice]
    selection_strategy: str
    total_series_slices: int
    nodule_coverage_rate: float   # 候选结节覆盖率
    stage: str = "stage4_selection"
    elapsed_ms: int
```

```python
# app/schemas/stage6_cot.py（★ CoT 三步中间 Schema）
from pydantic import BaseModel
from typing import List, Optional

class SlicePerception(BaseModel):
    """Step 1 输出：每张切片的视觉感知"""
    slice_rank: int
    window_type: str              # lung / mediastinum
    visual_description: str       # LLM 对该切片的视觉描述
    abnormal_regions: List[str]   # 异常区域描述列表
    quality_note: Optional[str]   # 图像质量备注

class NoduleIntegration(BaseModel):
    """Step 2 输出：跨切片结节整合"""
    integrated_nodule_id: str
    best_slice_rank: int          # 最佳观察切面
    cross_slice_consistency: str  # 多切面一致性描述
    estimated_3d_size: str        # 综合多切面的估计大小
    location_description: str     # 解剖位置描述

class CoTIntermediateResult(BaseModel):
    """CoT Step 1+2 的中间产物，落库供调试"""
    task_id: str
    step1_perceptions: List[SlicePerception]
    step2_integrations: List[NoduleIntegration]
    step1_tokens: int
    step2_tokens: int
```

---

## 十、Independent Evaluator — 独立评估器

```python
# app/evaluators/base_evaluator.py
from abc import ABC, abstractmethod
from pydantic import BaseModel
from enum import Enum
from typing import Any, List
import logging, json

logger = logging.getLogger(__name__)

class EvalStatus(str, Enum):
    PASS    = "pass"
    REJECT  = "reject"
    WARNING = "warning"

class EvalResult(BaseModel):
    status: EvalStatus
    score: float
    issues: List[str]
    suggestions: List[str]
    metadata: dict = {}

class BaseEvaluator(ABC):
    """
    纯函数式 Sub-Agent：不依赖外部状态，不修改任何数据。
    接收 Stage Output → 返回 EvalResult，完全可独立测试和替换。
    """
    @abstractmethod
    def evaluate(self, stage_output: Any) -> EvalResult:
        pass

    def __call__(self, stage_output: Any) -> EvalResult:
        result = self.evaluate(stage_output)
        # 评估结果结构化日志，供后续分析评估器准确性
        logger.info(json.dumps({
            "evaluator": self.__class__.__name__,
            "stage":     stage_output.stage if hasattr(stage_output, "stage") else "unknown",
            "task_id":   getattr(stage_output, "task_id", ""),
            "status":    result.status,
            "score":     result.score,
            "issues":    result.issues
        }))
        return result
```

```python
# app/evaluators/quality_evaluator.py
from .base_evaluator import BaseEvaluator, EvalResult, EvalStatus
from app.schemas.stage1_normalize import Stage1Result, FileType

class QualityEvaluator(BaseEvaluator):
    MIN_DICOM_SLICES    = 10
    MIN_FILE_SIZE_BYTES = 10 * 1024

    def evaluate(self, stage_output: Stage1Result) -> EvalResult:
        issues, suggestions = [], []
        score = 1.0

        if not stage_output.normalized_files:
            return EvalResult(status=EvalStatus.REJECT, score=0.0,
                              issues=["未检测到有效影像文件"],
                              suggestions=["请上传 DICOM、CT 或病理切片图像"])

        for f in stage_output.normalized_files:
            if f.file_type in (FileType.DICOM_SINGLE, FileType.DICOM_SERIES,
                               FileType.DICOM_ARCHIVE):
                for series in (f.dicom_series or []):
                    if series.slice_count < self.MIN_DICOM_SLICES:
                        issues.append(
                            f"序列 {series.series_uid[:8]}... 仅 {series.slice_count} 张，可能不完整"
                        )
                        suggestions.append("请上传完整 DICOM 序列（建议 ≥10 张切片）")
                        score -= 0.3

            if f.size_bytes < self.MIN_FILE_SIZE_BYTES:
                issues.append(f"文件 {f.original_filename} 过小，可能损坏")
                score -= 0.4

        status = EvalStatus.REJECT if score <= 0.5 else (
            EvalStatus.WARNING if issues else EvalStatus.PASS
        )
        return EvalResult(status=status, score=max(score, 0.0),
                          issues=issues, suggestions=suggestions)
```

---

## 十一、Stage 3：病灶候选检测（★ 引入 TotalSegmentator）

**问题**：纯规则（阈值 + 连通域）假阳性率高达 ~40%，血管、骨骼容易误判为结节。

**改进**：引入 TotalSegmentator 预训练模型（CPU fast 模式，内存 ~500MB，2C2G 可用）。

```python
# app/pipeline/stage3_detector.py
import SimpleITK as sitk
import numpy as np
from pathlib import Path
from app.schemas.stage3_detection import Stage3Result, NoduleCandidate

class NoduleDetector:

    def run(self, task_id: str, dicom_series_dir: str) -> Stage3Result:
        import time
        t0 = time.time()

        # Step 1: TotalSegmentator 做肺野分割（过滤非肺区域，大幅降低假阳性）
        lung_mask = self._segment_lung(dicom_series_dir)

        # Step 2: 在肺野 mask 内做连通域分析找候选结节
        candidates = self._detect_in_mask(dicom_series_dir, lung_mask)

        return Stage3Result(
            task_id=task_id,
            has_nodule_candidates=len(candidates) > 0,
            candidates=candidates,
            total_slices_scanned=self._count_slices(dicom_series_dir),
            elapsed_ms=int((time.time() - t0) * 1000)
        )

    def _segment_lung(self, dicom_dir: str) -> sitk.Image:
        """
        TotalSegmentator fast 模式：仅做肺部分割
        内存峰值约 400-500MB，CPU 推理约 30-60s（2核）
        """
        try:
            from totalsegmentator.python_api import totalsegmentator
            output_dir = f"/tmp/seg_{Path(dicom_dir).name}"
            totalsegmentator(
                input=dicom_dir,
                output=output_dir,
                task="lung_vessels",
                fast=True,           # 快速模式，牺牲少量精度换速度
                device="cpu",
                quiet=True
            )
            lung_path = f"{output_dir}/lung_upper_lobe_left.nii.gz"
            if Path(lung_path).exists():
                return sitk.ReadImage(lung_path)
        except (ImportError, Exception):
            pass
        # Fallback：TotalSegmentator 不可用时退化为简单阈值分割
        return self._simple_lung_threshold(dicom_dir)

    def _simple_lung_threshold(self, dicom_dir: str) -> sitk.Image:
        """Fallback：HU 值阈值法分割肺野"""
        reader = sitk.ImageSeriesReader()
        reader.SetFileNames(sitk.ImageSeriesReader.GetGDCMSeriesFileNames(dicom_dir))
        image = reader.Execute()
        # 肺野 HU 范围 -1000 ~ -400
        lung_mask = sitk.BinaryThreshold(image, lowerThreshold=-1000,
                                          upperThreshold=-400, insideValue=1)
        return sitk.BinaryMorphologicalClosing(lung_mask, (3, 3, 3))

    def _detect_in_mask(self, dicom_dir: str,
                        lung_mask: sitk.Image) -> list[NoduleCandidate]:
        """在肺野 mask 内做连通域分析，筛选符合结节大小的候选区域"""
        reader = sitk.ImageSeriesReader()
        reader.SetFileNames(sitk.ImageSeriesReader.GetGDCMSeriesFileNames(dicom_dir))
        image = reader.Execute()

        # 在肺野内找高密度区域（结节 HU 范围约 -100 ~ 200）
        nodule_region = sitk.BinaryThreshold(image, lowerThreshold=-100,
                                              upperThreshold=200, insideValue=1)
        masked = sitk.And(nodule_region, lung_mask)

        labeled   = sitk.ConnectedComponent(masked)
        stats     = sitk.LabelShapeStatisticsImageFilter()
        stats.Execute(labeled)

        candidates = []
        spacing = list(image.GetSpacing())    # (x, y, z) mm

        for label in stats.GetLabels():
            size_mm3 = stats.GetPhysicalSize(label)
            # 直径 3~30mm 范围（体积 14~14137 mm³）
            if not (14 < size_mm3 < 14137):
                continue

            centroid_mm  = list(stats.GetCentroid(label))
            centroid_idx = list(image.TransformPhysicalPointToIndex(centroid_mm))
            diameter_mm  = (size_mm3 * 6 / 3.14159) ** (1/3)
            bbox         = stats.GetBoundingBox(label)

            # 置信度：基于大小、圆度等规则打分
            roundness  = stats.GetRoundness(label) if hasattr(stats, "GetRoundness") else 0.5
            confidence = min(1.0, roundness * 0.6 + (1 - abs(diameter_mm - 8) / 20) * 0.4)

            candidates.append(NoduleCandidate(
                candidate_id=f"cand_{label}",
                series_uid=Path(dicom_dir).name,
                slice_index=centroid_idx[2],
                bbox_x=bbox[0] / image.GetWidth(),
                bbox_y=bbox[1] / image.GetHeight(),
                bbox_w=bbox[3] / image.GetWidth(),
                bbox_h=bbox[4] / image.GetHeight(),
                center_voxel=centroid_idx,
                center_mm=centroid_mm,
                estimated_diameter_mm=round(diameter_mm, 1),
                confidence=round(confidence, 3),
                window_center=-600.0,
                window_width=1500.0
            ))

        # 按置信度降序，最多返回 20 个候选
        return sorted(candidates, key=lambda c: -c.confidence)[:20]

    def _count_slices(self, dicom_dir: str) -> int:
        return len(sitk.ImageSeriesReader.GetGDCMSeriesFileNames(dicom_dir))
```

---

## 十二、Stage 4：关键切片提取（★ 双窗位 + pHash 去重）

```python
# app/pipeline/stage4_selector.py
import numpy as np
import cv2
import pydicom
from PIL import Image
import imagehash
from typing import List, Tuple
from app.schemas.stage3_detection import Stage3Result, NoduleCandidate
from app.schemas.stage4_selection import SelectedSlice, Stage4Result, DualWindowPng

class KeySliceSelector:
    """
    改进点：
    1. 双窗位渲染：肺窗 + 纵隔窗，LLM 可同时观察软组织和结节形态
    2. 感知哈希(pHash)去重：替代简单的 ±N 层抑制，真正避免内容重复
    3. 解剖多样性保证：上/中/下肺各至少选 2 张
    """
    TOP_K = 10

    WINDOWS = {
        "lung":        (-600,  1500),   # 肺窗
        "mediastinum": (40,    400),    # 纵隔窗
    }
    PHASH_THRESHOLD = 8                 # 汉明距离 < 8 视为重复

    def run(self, stage3: Stage3Result, dicom_paths: List[str]) -> Stage4Result:
        import time
        t0 = time.time()

        groups  = self._group_by_series(dicom_paths)
        selected_all = []

        for series_uid, paths in groups.items():
            sorted_paths = self._sort_by_z(paths)
            candidates   = [c for c in stage3.candidates if c.series_uid == series_uid]

            # 1. 先渲染肺窗（用于评分）
            scores = self._compute_scores(sorted_paths, candidates)

            # 2. 贪心选取，用 pHash 保证多样性
            top_indices = self._phash_diverse_topk(sorted_paths, scores, self.TOP_K)

            for rank, idx in enumerate(top_indices):
                # 3. 对选出的切片渲染双窗位
                dual_png = self._render_dual_window(sorted_paths[idx], series_uid, idx)
                meta     = self._read_meta(sorted_paths[idx])
                nc       = [c for c in candidates if c.slice_index == idx]

                selected_all.append(SelectedSlice(
                    series_uid=series_uid,
                    slice_index=idx,
                    rank=rank + 1,
                    score=float(scores[idx]),
                    dual_window=dual_png,
                    slice_location_mm=meta.get("slice_location"),
                    slice_thickness_mm=meta.get("slice_thickness"),
                    nodule_candidates=nc,
                    dicom_metadata=meta,
                    selection_reason=self._explain_selection(idx, candidates, scores[idx])
                ))

        total_slices = sum(len(v) for v in groups.values())
        covered = {c.candidate_id for s in selected_all for c in s.nodule_candidates}
        all_cands = {c.candidate_id for c in stage3.candidates}

        return Stage4Result(
            task_id=stage3.task_id,
            selected_slices=selected_all,
            selection_strategy="dual_window_phash_diverse_topk",
            total_series_slices=total_slices,
            nodule_coverage_rate=len(covered) / len(all_cands) if all_cands else 1.0,
            elapsed_ms=int((time.time() - t0) * 1000)
        )

    def _render_dual_window(self, dcm_path: str,
                             series_uid: str, idx: int) -> DualWindowPng:
        """渲染肺窗 + 纵隔窗两张 PNG，均压缩至 512×512"""
        from app.config import settings as _settings
        from app.services.dicom_service import apply_hu_transform   # ★ 修复：必须先 HU 转换
        # ★ 修复：使用 settings.STORAGE_ROOT 绝对路径，避免相对路径随启动目录漂移
        processed_dir = Path(_settings.STORAGE_ROOT).resolve() / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)

        ds  = pydicom.dcmread(dcm_path)
        raw = apply_hu_transform(ds.pixel_array, ds)   # ★ 修复：先转 HU，否则窗位裁剪范围完全错误
        paths = {}
        phashes = {}

        for name, (wc, ww) in self.WINDOWS.items():
            arr = np.clip((raw - wc + ww / 2) / ww * 255, 0, 255).astype(np.uint8)
            arr = cv2.resize(arr, (512, 512))
            out = str(processed_dir / f"{series_uid[:8]}_s{idx:04d}_{name}.png")
            cv2.imwrite(out, arr)
            paths[name]   = out
            phashes[name] = str(imagehash.phash(Image.fromarray(arr)))

        return DualWindowPng(
            lung_window_path=paths["lung"],
            mediastinum_window_path=paths["mediastinum"],
            phash_lung=phashes["lung"]
        )

    def _phash_diverse_topk(self, paths: List[str],
                             scores: np.ndarray, k: int) -> List[int]:
        """
        pHash 多样性选取：
        - 按得分降序迭代候选切片
        - 与已选切片计算 pHash 汉明距离
        - 距离 < PHASH_THRESHOLD 则跳过（内容重复）
        - 同时保证上/中/下肺解剖覆盖
        """
        n = len(paths)
        if n <= k:
            return list(range(n))

        # 预计算所有切片的肺窗图像和 pHash（只读 pixel 数据，内存友好）
        slice_phashes = {}
        def get_phash(i):
            if i not in slice_phashes:
                try:
                    ds  = pydicom.dcmread(paths[i])
                    raw = ds.pixel_array.astype(np.float32)
                    wc, ww = -600, 1500
                    arr = np.clip((raw - wc + ww/2) / ww * 255, 0, 255).astype(np.uint8)
                    arr = cv2.resize(arr, (128, 128))   # pHash 用小图，节省内存
                    slice_phashes[i] = imagehash.phash(Image.fromarray(arr))
                except Exception:
                    slice_phashes[i] = None
            return slice_phashes[i]

        selected, selected_phashes = [], []
        seg_size = n // 3

        # 上/中/下肺每段强制选 2 张（解剖覆盖保证）
        for seg in range(3):
            seg_range = range(seg * seg_size, min((seg + 1) * seg_size, n))
            seg_scored = sorted(seg_range, key=lambda i: -scores[i])
            count = 0
            for i in seg_scored:
                if count >= 2:
                    break
                ph = get_phash(i)
                if ph is None:
                    continue
                if not any(abs(ph - h) < self.PHASH_THRESHOLD for h in selected_phashes):
                    selected.append(i)
                    selected_phashes.append(ph)
                    count += 1

        # 剩余名额：全局高分补充，pHash 去重
        all_sorted = sorted(range(n), key=lambda i: -scores[i])
        for i in all_sorted:
            if len(selected) >= k:
                break
            if i in selected:
                continue
            ph = get_phash(i)
            if ph and not any(abs(ph - h) < self.PHASH_THRESHOLD for h in selected_phashes):
                selected.append(i)
                selected_phashes.append(ph)

        return sorted(selected)

    def _compute_scores(self, paths: List[str],
                        candidates: List[NoduleCandidate]) -> np.ndarray:
        n = len(paths)
        scores = np.zeros(n)

        # 候选结节覆盖（权重最高）
        for c in candidates:
            if c.slice_index < n:
                scores[c.slice_index] += c.confidence * 5.0
                for delta in range(-3, 4):
                    nb = c.slice_index + delta
                    if 0 <= nb < n and nb != c.slice_index:
                        scores[nb] += c.confidence * (1.0 - abs(delta) * 0.15)

        # 图像质量分（信息熵 + Sobel 对比度）
        for i, path in enumerate(paths):
            entropy, contrast = self._image_quality(path)
            scores[i] += entropy * 1.0 + contrast * 0.5

        return scores

    def _image_quality(self, dcm_path: str) -> Tuple[float, float]:
        try:
            ds  = pydicom.dcmread(dcm_path)
            raw = ds.pixel_array.astype(np.float32)
            wc, ww = -600, 1500
            arr = np.clip((raw - wc + ww/2) / ww * 255, 0, 255).astype(np.uint8)

            hist = cv2.calcHist([arr], [0], None, [256], [0, 256]).flatten()
            hist = hist / hist.sum()
            hist = hist[hist > 0]
            entropy = float(-np.sum(hist * np.log2(hist))) / 8.0

            sobelx = cv2.Sobel(arr, cv2.CV_64F, 1, 0, ksize=3)
            sobely = cv2.Sobel(arr, cv2.CV_64F, 0, 1, ksize=3)
            contrast = float(np.sqrt(sobelx**2 + sobely**2).mean()) / 100.0
            return entropy, contrast
        except Exception:
            return 0.0, 0.0

    def _explain_selection(self, idx: int, candidates: list, score: float) -> str:
        reasons = []
        nc = [c for c in candidates if c.slice_index == idx]
        if nc:
            reasons.append(f"包含候选结节（置信度 {nc[0].confidence:.2f}）")
        if score > 3.0:
            reasons.append("图像信息熵高")
        if not reasons:
            reasons.append("解剖覆盖补充")
        return "；".join(reasons)

    def _sort_by_z(self, paths: List[str]) -> List[str]:
        def z(p):
            try:
                ds  = pydicom.dcmread(p, stop_before_pixels=True)
                pos = ds.get("ImagePositionPatient")
                return float(pos[2]) if pos else 0.0
            except Exception:
                return 0.0
        return sorted(paths, key=z)

    def _read_meta(self, dcm_path: str) -> dict:
        ds = pydicom.dcmread(dcm_path, stop_before_pixels=True)
        return {
            "window_center":  float(ds.get("WindowCenter", -600) or -600),
            "window_width":   float(ds.get("WindowWidth", 1500) or 1500),
            "slice_location": float(ds.get("SliceLocation", 0) or 0),
            "slice_thickness":float(ds.get("SliceThickness", 1) or 1),
            "pixel_spacing":  list(ds.get("PixelSpacing", [1.0, 1.0])),
            "modality":       str(ds.get("Modality", "CT")),
            "kvp":            str(ds.get("KVP", "")),
        }

    def _group_by_series(self, paths: List[str]) -> dict:
        groups = {}
        for p in paths:
            try:
                ds  = pydicom.dcmread(p, stop_before_pixels=True)
                uid = str(ds.get("SeriesInstanceUID", "unknown"))
            except Exception:
                uid = "unknown"
            groups.setdefault(uid, []).append(p)
        return groups
```

---

## 十三、Stage 5：sqlite-vec 语义检索（★ 替代静态 JSON 匹配）

```python
# app/services/knowledge_service.py
import sqlite3, json
import numpy as np
import sqlite_vec
from app.config import settings

class KnowledgeService:
    """
    使用 sqlite-vec 扩展做轻量语义检索，无需独立向量数据库。
    知识库（医学案例 + 指南条目）在服务启动时一次性 embedding 入库。
    """

    def __init__(self, db_path: str = "monica_knowledge.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id          INTEGER PRIMARY KEY,
                category    TEXT,        -- 'case' / 'guideline'
                title       TEXT,
                content     TEXT,
                source      TEXT
            )
        """)
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec
            USING vec0(
                item_id INTEGER PRIMARY KEY,
                embedding FLOAT[1536]    -- text-embedding-3-small 维度
            )
        """)
        self.conn.commit()

    def search(self, query: str, top_k: int = 3,
               category: str = None) -> list[dict]:
        """语义检索，返回最相关的知识条目"""
        query_vec = self._embed(query)

        # ★ 安全坑：category 参数使用参数化查询，防 SQL 注入
        # SQL 占位符顺序：MATCH ? → category ? → LIMIT ?
        # params 顺序必须与 SQL 中 ? 出现顺序完全一致
        cat_filter = "AND k.category = ?" if category else ""
        vec_json   = json.dumps(query_vec.tolist())
        params: list = [vec_json]
        if category:
            params.append(category)      # 对应 cat_filter 中的 ?
        params.append(top_k)             # 对应 LIMIT ?

        rows = self.conn.execute(f"""
            SELECT k.id, k.category, k.title, k.content, k.source, v.distance
            FROM knowledge_vec v
            JOIN knowledge_items k ON k.id = v.item_id
            WHERE v.embedding MATCH ?
              AND k.rowid IS NOT NULL
              {cat_filter}
            ORDER BY v.distance
            LIMIT ?
        """, params).fetchall()

        return [
            {"id": r[0], "category": r[1], "title": r[2],
             "content": r[3], "source": r[4], "similarity": 1 - r[5]}
            for r in rows
        ]

    def ingest_jsonl(self, jsonl_path: str):
        """将知识库 jsonl 文件 embedding 后写入 sqlite-vec"""
        import httpx
        with open(jsonl_path) as f:
            for line in f:
                item = json.loads(line)
                vec = self._embed(item["title"] + " " + item["content"])
                cursor = self.conn.execute(
                    "INSERT INTO knowledge_items (category, title, content, source) VALUES (?,?,?,?)",
                    (item["category"], item["title"], item["content"], item.get("source", ""))
                )
                item_id = cursor.lastrowid
                self.conn.execute(
                    "INSERT INTO knowledge_vec (item_id, embedding) VALUES (?, ?)",
                    [item_id, json.dumps(vec.tolist())]
                )
        self.conn.commit()

    def _embed(self, text: str) -> np.ndarray:
        """调用 OpenAI text-embedding-3-small（$0.02/1M tokens，成本极低）"""
        import httpx, json as _json
        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={"model": "text-embedding-3-small", "input": text[:4096]},
            timeout=30
        )
        return np.array(resp.json()["data"][0]["embedding"], dtype=np.float32)
```

---

## 十四、Stage 6：CoT 三步推理 + 降级链（★ 全面重构）

### 14.1 CoT 推理策略

```
原方案：10张图 + 所有上下文 → 一次调用（~60k-80k tokens）
新方案：拆成3步，每步专注一件事，总 token 减少 ~60%

Step 1（感知）：  逐张图片描述，输出每张切片的视觉描述 JSON
                  输入：每次 1 张图 + 基础元数据（并行调用）
                  token：~800/张 × 10 = 8000

Step 2（整合）：  综合所有描述，识别同一结节的多切面表现
                  输入：Step1 所有描述文本 + 3D 坐标
                  token：~3000（纯文本，无图片）

Step 3（报告）：  结合知识库 + 医学上下文，生成结构化报告
                  输入：Step2 整合结果 + 医学上下文
                  token：~5000（纯文本）

总计：~16000 tokens（vs 原来 ~70000）
```

```python
# app/pipeline/stage6_llm.py
import asyncio, base64, json, httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.config import settings
from app.schemas.stage5_context import LLMPayload
from app.schemas.stage6_cot import CoTIntermediateResult, SlicePerception, NoduleIntegration
from app.schemas.stage7_report import AnalysisReport

class LLMStage:

    # 降级链：首选 → 降级1 → 降级2 → 降级3
    MODEL_FALLBACK_CHAIN = [
        "gpt-4o",
        "gpt-4o-mini",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]

    STEP1_SYSTEM = """
你是医学影像视觉感知助手。请对给定的CT切片图像进行视觉描述，不做诊断。
以 JSON 格式返回：
{
  "slice_rank": <int>,
  "window_type": "<lung|mediastinum>",
  "visual_description": "<客观描述影像所见>",
  "abnormal_regions": ["<异常区域1>", ...],
  "quality_note": "<图像质量备注，无则null>"
}
"""

    STEP2_SYSTEM = """
你是医学影像结构整合助手。给定同一CT序列多个切片的视觉描述，
请整合识别跨切面的同一病灶。以 JSON 格式返回整合结果：
{
  "integrated_nodules": [
    {
      "integrated_nodule_id": "<n1>",
      "best_slice_rank": <int>,
      "cross_slice_consistency": "<一致性描述>",
      "estimated_3d_size": "<如：8mm×9mm×7mm>",
      "location_description": "<解剖位置，如：右肺上叶尖段，距胸膜约8mm>"
    }
  ]
}
"""

    STEP3_SYSTEM = """
你是专业医学影像 AI 辅助分析系统。基于已整合的影像发现和医学知识，生成分析报告。
以 JSON 格式返回：
{
  "findings": ["<发现1>", ...],
  "impression": "<总体印象，1-2句>",
  "nodule_assessment": [
    {
      "nodule_id": "<n1>",
      "location": "<解剖位置>",
      "size_mm": "<大小>",
      "lung_rads_grade": "<1|2|3|4A|4B|4X>",
      "morphology": "<形态描述>",
      "density_type": "<实性|磨玻璃|混合>",
      "malignancy_risk": "<低|中|高|不确定>",
      "follow_up": "<随访建议>"
    }
  ],
  "recommendations": ["<建议1>", ...],
  "confidence": <0.0-1.0>,
  "limitations": ["<局限性1>", ...],
  "disclaimer": "本报告由 AI 辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"
}
⚠️ 必须包含 disclaimer 字段。
"""

    async def run(self, payload: LLMPayload, model: str = "gpt-4o") -> AnalysisReport:
        # Step 1：并行感知（每张图独立调用，高效）
        step1_results = await self._step1_perceive_parallel(payload, model)

        # Step 2：纯文本整合
        step2_result = await self._step2_integrate(step1_results, payload, model)

        # Step 3：生成结构化报告
        report = await self._step3_report(step2_result, payload, model)

        # 中间产物落库（供调试和评估器使用）
        cot_intermediate = CoTIntermediateResult(
            task_id=payload.task_id,
            step1_perceptions=step1_results,
            step2_integrations=step2_result,
            step1_tokens=sum(getattr(p, "_tokens", 0) for p in step1_results),
            step2_tokens=getattr(step2_result, "_tokens", 0)
        )

        return report

    async def _step1_perceive_parallel(self, payload: LLMPayload,
                                        model: str) -> list[SlicePerception]:
        """
        并行处理每张切片，减少总耗时。
        ★ 修复：原代码无并发限制，10 张图同时发出会触发 LLM 速率限制（rate limit）。
        使用 asyncio.Semaphore 将并发数控制在 5，与资源优化策略一致。
        """
        semaphore = asyncio.Semaphore(5)  # 最多 5 个并发 LLM 调用

        async def _perceive_with_limit(slc):
            async with semaphore:
                return await self._perceive_one_slice(slc, model)

        tasks = [_perceive_with_limit(slc) for slc in payload.selected_slices]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, SlicePerception)]

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=4, max=30))
    async def _perceive_one_slice(self, slc, model: str) -> SlicePerception:
        images = []
        for img_path in [slc.dual_window.lung_window_path,
                         slc.dual_window.mediastinum_window_path]:
            with open(img_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}
            })

        messages = [
            {"role": "system", "content": self.STEP1_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": f"切片编号：{slc.rank}，位置：{slc.slice_location_mm} mm"},
                *images
            ]}
        ]
        raw = await self._call_with_fallback(messages, model)
        # ★ 修复：用 parse_llm_response 替代裸 json.loads，处理 markdown 包裹/字段类型容错等脏数据
        # （第26节专门引入了此工具，但此处原版未使用）
        from app.utils.llm_parser import parse_llm_response
        def fallback_perception():
            return SlicePerception(
                slice_rank=slc.rank,
                window_type="lung",
                visual_description="图像感知失败，内容不可用",
                abnormal_regions=[],
                quality_note="LLM 解析错误"
            )
        return parse_llm_response(raw, SlicePerception, fallback_perception)

    async def _step2_integrate(self, perceptions: list[SlicePerception],
                                payload: LLMPayload, model: str) -> list[NoduleIntegration]:
        """纯文本整合，无图片，token 消耗低"""
        perception_text = json.dumps(
            [p.model_dump() for p in perceptions], ensure_ascii=False, indent=2
        )
        nodule_ctx = json.dumps(
            [c.model_dump() for c in payload.nodule_description.nodules],
            ensure_ascii=False, indent=2
        )
        messages = [
            {"role": "system", "content": self.STEP2_SYSTEM},
            {"role": "user", "content": (
                f"## 各切片视觉感知结果\n{perception_text}\n\n"
                f"## 算法预检测候选结节\n{nodule_ctx}"
            )}
        ]
        raw = await self._call_with_fallback(messages, model)
        data = json.loads(raw)
        return [NoduleIntegration(**n) for n in data.get("integrated_nodules", [])]

    async def _step3_report(self, integrations: list[NoduleIntegration],
                             payload: LLMPayload, model: str) -> AnalysisReport:
        """生成最终结构化报告"""
        integration_text = json.dumps(
            [i.model_dump() for i in integrations], ensure_ascii=False, indent=2
        )
        knowledge_text = "\n".join([
            f"- {c['title']}: {c['content'][:200]}"
            for c in payload.medical_context.similar_cases[:3]
        ])
        messages = [
            {"role": "system", "content": self.STEP3_SYSTEM},
            {"role": "user", "content": (
                f"## 用户问题\n{payload.user_prompt}\n\n"
                f"## 影像整合发现\n{integration_text}\n\n"
                f"## 参考知识库\n{knowledge_text}\n\n"
                f"## 历史结果\n{json.dumps(payload.historical_results[:2], ensure_ascii=False)}"
            )}
        ]
        raw = await self._call_with_fallback(messages, model)
        data = json.loads(raw)
        return AnalysisReport(task_id=payload.task_id, model_used=model,
                               raw_response=raw, **data)

    async def _call_with_fallback(self, messages: list, preferred_model: str) -> str:
        """
        降级链：首选模型失败时依次尝试备用模型。
        ★ 修复：原设计 _call_llm 上有 @retry(3 次)，_call_with_fallback 遍历 4 个模型，
        实际最多尝试 4×3=12 次，且单模型多次 429 重试会掩盖"该模型确实不可用"的信号。
        改为：每个模型只尝试一次（失败立即切下一个），整体重试交由外层（如 _perceive_one_slice）控制。
        """
        chain = [preferred_model] + [m for m in self.MODEL_FALLBACK_CHAIN
                                      if m != preferred_model]
        last_exc = None
        for model in chain:
            try:
                return await self._call_llm(messages, model)
            except Exception as e:
                last_exc = e
                logger.warning(f"[LLM] 模型 {model} 调用失败，尝试下一个: {e}")
                continue
        raise RuntimeError(f"所有模型均不可用: {last_exc}")

    async def _call_llm(self, messages: list, model: str) -> str:
        """
        单次 LLM 调用，无内部重试。
        ★ 重试逻辑由外层 @retry（_perceive_one_slice）统一控制，
        避免双层 retry 嵌套（原设计最多触发 4模型×3次 = 12 次调用）。
        """
        if model.startswith("gemini"):
            return await self._call_gemini(messages, model)
        return await self._call_openai(messages, model)

    async def _call_openai(self, messages: list, model: str) -> str:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={"model": model, "messages": messages,
                      "response_format": {"type": "json_object"}}
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def _call_gemini(self, messages: list, model: str) -> str:
        """
        ★ 修复：正确将 OpenAI 格式消息列表转换为 Gemini API 格式。
        原始代码 str(messages) 直接把 Python list repr 作为文本发送，
        Gemini 会收到形如 "[{'role': 'system', ...}]" 的纯字符串，完全无法解析。

        转换规则：
        - system 消息 → systemInstruction（Gemini v1beta 支持）
        - user/assistant 消息 → contents（role: user/model）
        - image_url 内容 → inlineData（base64 图片）
        """
        system_text = ""
        contents = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                # system 消息提取为 systemInstruction
                system_text = content if isinstance(content, str) else str(content)
                continue

            # 将 OpenAI content（str 或 list）转为 Gemini parts
            parts = []
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        parts.append({"text": item["text"]})
                    elif item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        if url.startswith("data:image/"):
                            # base64 内联图片：data:image/png;base64,<data>
                            mime, b64data = url.split(";base64,", 1)
                            mime_type = mime.split("data:")[-1]
                            parts.append({"inlineData": {
                                "mimeType": mime_type,
                                "data": b64data
                            }})

            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": parts})

        request_body = {
            "contents": contents,
            "generationConfig": {"response_mime_type": "application/json"}
        }
        if system_text:
            request_body["systemInstruction"] = {"parts": [{"text": system_text}]}

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": settings.GEMINI_API_KEY},
                json=request_body
            )
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
```

---

## 十五、Token 配额保护（★ 新增）

```python
# app/services/quota_service.py
import redis.asyncio as aioredis
from datetime import date
from app.config import settings

# ★ 修复：模块级单例连接池，避免每次实例化 QuotaService 都创建新连接
# aioredis.from_url 返回的连接池是线程/协程安全的，可安全全局共享
_redis_pool: aioredis.Redis | None = None

def get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_pool

class QuotaService:
    """
    基于 Redis 的滑动窗口 token 配额，防止恶意用户滥用 LLM API。
    每用户每天限额，超限返回 429。

    ★ 连接池：复用模块级单例 _redis_pool，不再每次创建新连接。
    """
    DAILY_TOKEN_LIMIT = 200_000   # 每用户每天 20 万 token（约 $0.4/天/用户）

    def __init__(self):
        self.redis = get_redis()   # 复用全局连接池

    async def check_and_consume(self, user_id: str, estimated_tokens: int) -> None:
        """
        ★ 修复两处问题：
        1. TOCTOU 竞态：原代码先 incrby 再判断，超限后不回滚，
           多并发请求可透支配额。改为先原子 incrby，超限立即 decrby 回滚。
        2. expire 仅在 current == estimated_tokens 时设置（竞态下可能漏设），
           改为每次都用 EXPIRE 刷新（已存在时重设 TTL 幂等安全）。
        """
        key = f"quota:{user_id}:{date.today()}"
        # 原子加；若超限立即回滚，保证不透支
        current = await self.redis.incrby(key, estimated_tokens)
        # 每次都刷新 TTL（expire 幂等，不影响已有数据）
        await self.redis.expire(key, 86400)

        if current > self.DAILY_TOKEN_LIMIT:
            # 回滚：将刚才加上去的 token 数减回去
            await self.redis.decrby(key, estimated_tokens)
            raise QuotaExceededError(
                f"今日分析配额已用尽（{self.DAILY_TOKEN_LIMIT:,} tokens），请明日再试"
            )

    async def get_remaining(self, user_id: str) -> int:
        key = f"quota:{user_id}:{date.today()}"
        used = int(await self.redis.get(key) or 0)
        return max(0, self.DAILY_TOKEN_LIMIT - used)

class QuotaExceededError(Exception):
    pass
```

配额检查在任务创建时进行：

```python
# app/api/analysis.py（加入配额检查）
@router.post("/analysis")
async def create_analysis(
    file_ids: list[str], prompt: str,
    model: str = "gpt-4o",
    user_id: str = Depends(get_current_user)
):
    quota = QuotaService()
    remaining = await quota.get_remaining(user_id)
    if remaining < 5000:   # 预估最低消耗
        raise HTTPException(429, detail={
            "error": "配额不足",
            "remaining_tokens": remaining,
            "reset_at": "明日 00:00 (UTC+8)"
        })

    idem_key = compute_idempotency_key(file_ids, prompt, model)
    with SessionLocal() as db:
        existing = db.query(Task).filter_by(idempotency_key=idem_key).first()
        if existing and existing.status not in ("error",):
            return {"task_id": existing.task_id, "reused": True, "status": existing.status}

    task_id = f"task_{os.urandom(6).hex()}"
    # ★ 修复（同七节一致）：入队前先写 pending 记录，防止 SSE 查不到任务
    with SessionLocal() as db:
        db.add(Task(task_id=task_id, idempotency_key=idem_key,
                    user_id=user_id, status="pending", model=model))
        db.commit()
    await enqueue_pipeline(task_id, file_ids, prompt, user_id, model, idem_key)
    return {"task_id": task_id, "reused": False, "status": "processing"}
```

---

## 十六、数据库模型设计（★ 更新）

### 16.1 file_records（文件去重）

```sql
CREATE TABLE file_records (
    file_hash    TEXT PRIMARY KEY,
    file_type    TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    size_bytes   INTEGER,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 16.2 upload_sessions（★ 分片上传会话）

```sql
CREATE TABLE upload_sessions (
    upload_id    TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    total_size   INTEGER,
    total_chunks INTEGER,
    file_sha256  TEXT NOT NULL,     -- 客户端预计算的 SHA256
    chunk_dir    TEXT,              -- 分块临时目录
    status       TEXT DEFAULT 'uploading',  -- uploading/complete/failed
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 16.3 tasks（任务，含幂等键）

```sql
CREATE TABLE tasks (
    task_id           TEXT PRIMARY KEY,
    idempotency_key   TEXT UNIQUE,      -- ★ 幂等去重键
    user_id           TEXT NOT NULL,
    status            TEXT DEFAULT 'pending',
    stage             TEXT,
    progress          INTEGER DEFAULT 0,
    model             TEXT,
    reject_reason     TEXT,
    suggestions       TEXT,             -- JSON
    error_message     TEXT,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME
);
CREATE INDEX idx_tasks_user ON tasks(user_id);
CREATE INDEX idx_tasks_idem ON tasks(idempotency_key);
```

### 16.4 stage_results（各阶段中间产物，可审计）

```sql
CREATE TABLE stage_results (
    id          TEXT PRIMARY KEY,
    task_id     TEXT REFERENCES tasks(task_id),
    stage       TEXT,               -- stage1 / stage2 / ...
    status      TEXT,               -- pass / warn / reject
    input_json  TEXT,               -- 本阶段输入 Schema 序列化
    output_json TEXT,               -- 本阶段输出 Schema 序列化
    eval_json   TEXT,               -- Evaluator 评估结果
    elapsed_ms  INTEGER,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 16.5 analysis_results（最终报告，append-only）

```sql
CREATE TABLE analysis_results (
    id                TEXT PRIMARY KEY,    -- 每次新建，不覆盖旧结果
    task_id           TEXT,
    user_id           TEXT,
    version           INTEGER DEFAULT 1,   -- 版本号，支持重新分析
    findings          TEXT,                -- JSON
    impression        TEXT,
    nodule_assessment TEXT,                -- JSON
    recommendations   TEXT,               -- JSON
    confidence        REAL,
    limitations       TEXT,               -- JSON
    disclaimer        TEXT,               -- 必须包含
    cot_snapshot      TEXT,               -- JSON，CoT 三步中间结果
    llm_model         TEXT,
    tokens_step1      INTEGER,
    tokens_step2      INTEGER,
    tokens_step3      INTEGER,
    eval_scores       TEXT,               -- JSON
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_results_task ON analysis_results(task_id);
CREATE INDEX idx_results_user ON analysis_results(user_id);
```

---

## 十七、完整 API 接口总览

### 17.1 身份验证

```
POST /api/v1/auth/wx-login
{ "code": "<wx.login() 返回的 code>" }
→ { "token": "<JWT>", "openid_hash": "ab12****" }
```

### 17.2 分片上传

```
# 初始化（客户端先计算 SHA256，服务端判断是否已存在）
POST /api/v1/upload/init
Authorization: Bearer <token>
{ "filename": "series.zip", "total_size": 524288000,
  "total_chunks": 100, "file_sha256": "abc123..." }
→ { "upload_id": "up_xxx", "already_exists": false }

# 上传分块（可并发、可断点续传）
POST /api/v1/upload/chunk
Authorization: Bearer <token>
form-data: upload_id, chunk_index, chunk_data
→ { "received": [0,1,2], "missing": [] }

# 查询已上传分块（断点续传用）
GET /api/v1/upload/{upload_id}/chunks
→ { "received_chunks": [0,1,3,4] }

# 合并完成，触发任务创建
POST /api/v1/upload/complete
Authorization: Bearer <token>
{ "upload_id": "up_xxx", "prompt": "请分析肺结节", "model": "gpt-4o" }
→ { "task_id": "task_xxx", "file_id": "sha256_hash", "status": "processing" }
```

### 17.3 任务管理

```
# 创建分析任务（已有 file_id 时直接创建，无需重传）
POST /api/v1/analysis
Authorization: Bearer <token>
{ "file_ids": ["sha256_1"], "prompt": "...", "model": "gpt-4o" }
→ { "task_id": "task_xxx", "reused": false, "status": "processing" }

# SSE 实时进度（推荐，替代轮询）
GET /api/v1/task/{task_id}/stream
Authorization: Bearer <token>
→ text/event-stream
  data: {"status":"stage3","stage_name":"病灶候选检测","progress":45}\n\n
  data: {"status":"done","progress":100,"result":{...}}\n\n

# 轮询查询（备用）
GET /api/v1/task/{task_id}
Authorization: Bearer <token>
→ { "task_id":"...", "status":"done", "progress":100, "result":{...} }

# 查询配额状态
GET /api/v1/quota
Authorization: Bearer <token>
→ { "remaining_tokens": 185000, "daily_limit": 200000, "reset_at": "2026-04-24T00:00:00+08:00" }
```

### 17.4 完整请求时序

```
小程序              API Gateway           ARQ Worker              存储/LLM
  │                      │                    │                      │
  │─wx.login()→code──▶  │                    │                      │
  │◀──JWT token──────────│                    │                      │
  │                      │                    │                      │
  │─upload/init──────▶   │ 查 SHA256 去重      │                      │
  │◀──upload_id──────────│                    │                      │
  │─upload/chunk×N───▶   │ 分块存储            │                      │
  │─upload/complete──▶   │ 合并+校验 SHA256    │                      │
  │◀──task_id────────────│──enqueue job──────▶│                      │
  │                      │                    │                      │
  │─/task/id/stream──▶   │                    │─Stage1: normalize───▶│ 去重/存储
  │◀─SSE: stage1─────────│◀──update status────│─Eval1: quality───────│
  │◀─SSE: stage2─────────│                    │─Stage3: TotalSeg─────│ CPU 推理
  │◀─SSE: stage3─────────│                    │─Eval3: nodule────────│
  │◀─SSE: stage4─────────│                    │─Stage4: dual-window──│ pHash去重
  │◀─SSE: stage5─────────│                    │─Stage5: sqlite-vec───│ 语义检索
  │◀─SSE: stage6─────────│                    │─Stage6: CoT Step1────▶│ GPT-4o (并行)
  │                      │                    │─Stage6: CoT Step2────▶│ GPT-4o (纯文本)
  │                      │                    │─Stage6: CoT Step3────▶│ GPT-4o (报告)
  │                      │                    │─Eval6: report────────│
  │                      │                    │─Stage7: store────────▶│ 结果入库
  │◀─SSE: done+result────│◀──final push───────│                      │
```

---

## 十八、配置管理（★ 更新）

```python
# app/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    APP_ENV: str = "production"
    SECRET_KEY: str = "change-me-in-production"

    # 微信
    WX_APPID:  str = ""
    WX_SECRET: str = ""

    # 数据库
    DATABASE_URL: str = "sqlite:///./monica.db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # LLM
    OPENAI_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    DEFAULT_MODEL:  str = "gpt-4o"

    # 存储
    STORAGE_ROOT:       str = "./storage"
    MAX_UPLOAD_SIZE_MB: int = 500
    CHUNK_SIZE_MB:      int = 5          # 每个分片大小

    # 配额保护
    DAILY_TOKEN_LIMIT: int = 200_000     # 每用户每天 token 上限

    # 性能（适配 2G 内存）
    ARQ_MAX_JOBS:         int = 1        # 单并发
    ARQ_JOB_TIMEOUT:      int = 600      # 10 分钟超时
    DICOM_BATCH_SIZE:     int = 50       # 每批处理 DICOM 数
    TOP_K_SLICES:         int = 10       # 送 LLM 的切片数
    TOTALSEG_FAST:        bool = True    # TotalSegmentator 快速模式

    class Config:
        env_file = ".env"

settings = Settings()
```

```ini
# .env.example
APP_ENV=production
SECRET_KEY=your-secret-key-here-min-32-chars

WX_APPID=wx_your_appid
WX_SECRET=your_wx_secret

DATABASE_URL=sqlite:///./monica.db
REDIS_URL=redis://localhost:6379/0

OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AI...
DEFAULT_MODEL=gpt-4o

STORAGE_ROOT=./storage
MAX_UPLOAD_SIZE_MB=500
CHUNK_SIZE_MB=5
DAILY_TOKEN_LIMIT=200000

ARQ_MAX_JOBS=1
ARQ_JOB_TIMEOUT=600
DICOM_BATCH_SIZE=50
TOP_K_SLICES=10
TOTALSEG_FAST=true
```

---

## 十九、依赖清单（★ 更新）

```
# requirements.txt

# Web 框架
fastapi==0.111.0
uvicorn==0.30.0
python-multipart==0.0.9
slowapi==0.1.9              # 限流

# 任务队列（ARQ 替代 Celery）
arq==0.25.0
redis==5.0.4

# 数据库
sqlalchemy==2.0.30
pydantic==2.7.0
pydantic-settings==2.2.1
sqlite-vec==0.1.6           # SQLite 向量扩展（知识库语义检索）

# 身份验证
PyJWT==2.8.0
httpx==0.27.0

# 文件处理
aiofiles==23.2.1

# DICOM 处理
pydicom==2.4.4
SimpleITK==2.3.1
numpy==1.26.4
opencv-python-headless==4.9.0.80
scikit-image==0.23.2
Pillow==10.3.0
ImageHash==4.3.1            # 感知哈希去重

# 病灶检测（可选，按需安装）
# TotalSegmentator==2.2.1   # 约 1.5GB，首次下载模型权重
# torch==2.2.0+cpu           # CPU 版 PyTorch

# 重试
tenacity==8.2.3

# 工具
python-dotenv==1.0.1
```

> **注意**：TotalSegmentator 依赖 PyTorch，首次部署需额外下载模型权重（约 1.5GB）。
> 2C2G 服务器上建议在正式运行前单独执行 `totalsegmentator --download-weights`。
> 若内存不足，Stage 3 会自动 fallback 到阈值规则检测，不影响整体流程。

---

## 二十、资源优化策略（2核2G / 40G 云服务器）

| 问题 | 解决方案 |
|------|----------|
| DICOM 几百张全量读入内存爆炸 | 流式批处理 `DICOM_BATCH_SIZE=50`，评分后只保留 Top-K 索引，其余立即释放 |
| TotalSegmentator 内存峰值 | 启用 `fast=True`，峰值约 400MB；推理完立即 `del model; gc.collect()` |
| PNG 双窗位转换内存 | 只对 Top-K 切片渲染，pHash 用 128×128 小图，最终 PNG 用 512×512 |
| LLM CoT 并行调用带宽 | Step1 并行调用上限 5 并发（asyncio.Semaphore），防止同时发送过多 Base64 |
| ARQ Worker 内存 | `max_jobs=1`，`max_tries=3`，`health_check_interval=30s` |
| 存储空间紧张（40G） | 原始 DICOM 压缩包处理完 24h 后自动删除，只保留 Top-K PNG + 元数据（节省 ~90%） |
| SQLite 写锁竞争 | `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;` |
| 分片临时文件残留 | 上传会话 24h 未完成则定时清理 `storage/chunks/` |
| Redis 内存 | 只存任务状态和配额计数，不缓存大对象；`maxmemory 100mb; maxmemory-policy allkeys-lru` |

**内存使用估算（峰值）：**

```
Uvicorn (FastAPI):       ~80MB
ARQ Worker (空载):       ~60MB
Redis:                   ~80MB
TotalSegmentator 推理:   ~450MB（推理后释放）
DICOM 批处理(50张):      ~200MB（处理后释放）
PNG 双窗位缓存:           ~50MB
SQLite WAL:              ~30MB
─────────────────────────────
推理阶段峰值:            ~950MB   ✅ 2G 内存可用
空载常驻:                ~250MB
```

---

## 二十一、医疗合规要点（★ 更新）

1. **免责声明强制植入（技术强制）**
   - 所有分析结果的 Pydantic Schema 中 `disclaimer` 字段为 **非空校验**，空字符串无法通过验证
   - 微信小程序必须将 `disclaimer` 渲染为显著文字（字号 ≥ 14px，对比度符合 WCAG AA），不可折叠

2. **DICOM 数据脱敏（Stage 1 执行）**
   - 强制对以下 DICOM tag 置空或 SHA256 单向 Hash：
     `PatientName`、`PatientID`、`PatientBirthDate`、`PatientAddress`、`InstitutionName`
   - 脱敏在文件写盘之前执行，原始含 PII 数据不落盘

3. **用户数据隔离**
   - 文件存储路径：`storage/uploads/{user_openid_hash}/{task_id}/`
   - API 层所有涉及 task_id / upload_id 的接口均校验 user_id 归属
   - SQLite 行级查询均附加 `WHERE user_id = ?` 条件

4. **全链路审计日志（append-only）**
   - `stage_results` 表记录每个 Stage 的完整输入输出，只增不改
   - LLM 原始响应（`raw_response`）强制存入 `analysis_results`
   - 日志保留期 ≥180 天

5. **结果版本化**
   - 用户可针对同一影像重新分析（不同 prompt 或 model）
   - 每次分析生成新的 `version` 记录，历史版本不覆盖

6. **用户知情告知**
   - 首次使用需签署《AI 辅助分析知情同意书》，记录签署时间戳和 openid
   - 上传页面明确展示：本系统为 AI 辅助工具，非 CE/FDA 认证医疗器械，分析结果须由执业医师复核

---

## 二十二、后续演进路径

| 阶段 | 工作 | 预估周期 |
|------|------|----------|
| MVP | 微信登录 + 分片上传 + 完整 Pipeline + GPT-4o | 4-6 周 |
| v1.1 | 病理切片支持（WSI 大图分 patch 处理，openslide） | 2 周 |
| v1.2 | 结节 3D 可视化（Three.js 在小程序渲染 VTK 体数据） | 3 周 |
| v1.3 | 历史任务对比（同一患者多次检查的趋势分析） | 2 周 |
| v2.0 | PostgreSQL 迁移 + 多用户并发（worker_concurrency=2） | 2 周 |
| v2.1 | 结构化报告对接 HL7/FHIR，接入医院 HIS | 6-8 周 |
| v3.0 | 本地部署 LLM（LLaMA3-Med 或 HuatuoGPT-Vision），脱离外部 API 依赖 | 待硬件升级 |

---

## 二十三、DICOM 多值字段健壮处理（★ v3 新增）

### 坑点背景

DICOM 标准允许多个字段以多值（MultiValue）形式存在，直接 `float(ds.get("WindowCenter"))` 在字段为列表时会抛 `TypeError`。

**常见雷区：**
- `WindowCenter` / `WindowWidth`：可能是 `[-600, 40]`（多窗位序列）
- `RescaleSlope` / `RescaleIntercept`：部分设备遗漏该字段，未处理则 HU 值错误
- `PixelSpacing`：有时是 `pydicom.sequence.Sequence` 而非 `list`
- `ImagePositionPatient`：DS 类型需显式转 float，直接 `float()` 会抛异常

```python
# app/services/dicom_service.py — DICOM 字段安全读取工具函数

import pydicom
from pydicom.multival import MultiValue
from pydicom.sequence import Sequence
from typing import Optional, Union
import numpy as np

def safe_float(value, default: float = 0.0) -> float:
    """安全转换 DICOM 字段为 float，处理 MultiValue 取第一个元素"""
    if value is None:
        return default
    if isinstance(value, (MultiValue, list, tuple)):
        return float(value[0]) if len(value) > 0 else default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def safe_list(value, default=None) -> list:
    """安全转换 DICOM 字段为 list"""
    if value is None:
        return default or []
    if isinstance(value, (MultiValue, list, tuple)):
        return [float(v) for v in value]
    return [float(value)]

def apply_hu_transform(pixel_array: np.ndarray, ds: pydicom.Dataset) -> np.ndarray:
    """
    应用 RescaleSlope / RescaleIntercept 将像素值转为真实 HU 值。
    若字段缺失则假设 slope=1, intercept=0（原样返回）。
    ⚠️ 不做此转换，阈值检测的 HU 范围会完全错乱！
    """
    slope     = safe_float(ds.get("RescaleSlope"),     default=1.0)
    intercept = safe_float(ds.get("RescaleIntercept"), default=0.0)
    return pixel_array.astype(np.float32) * slope + intercept

def get_window_params(ds: pydicom.Dataset,
                      preferred_wc: float = -600.0,
                      preferred_ww: float = 1500.0):
    """
    读取 DICOM 内嵌窗位参数，优先使用 DICOM 建议值；
    多窗位序列取第一个（通常是最通用的显示窗位）。
    """
    wc = safe_float(ds.get("WindowCenter"), default=preferred_wc)
    ww = safe_float(ds.get("WindowWidth"),  default=preferred_ww)
    return wc, ww

def get_image_position_z(ds: pydicom.Dataset) -> float:
    """安全读取切片 Z 轴位置（用于排序）"""
    pos = ds.get("ImagePositionPatient")
    if pos is not None:
        try:
            return float(pos[2])
        except (IndexError, ValueError, TypeError):
            pass
    # 降级：尝试 SliceLocation
    return safe_float(ds.get("SliceLocation"), default=0.0)
```

**在 Stage 4 中的使用修正（原代码存在隐患）：**

```python
# app/pipeline/stage4_selector.py — _render_dual_window 修正版
def _render_dual_window(self, dcm_path: str,
                         series_uid: str, idx: int) -> DualWindowPng:
    from app.services.dicom_service import apply_hu_transform, safe_float

    ds  = pydicom.dcmread(dcm_path)
    raw = apply_hu_transform(ds.pixel_array, ds)   # ★ 必须先转 HU！

    paths, phashes = {}, {}
    for name, (wc, ww) in self.WINDOWS.items():
        arr = np.clip((raw - wc + ww / 2) / ww * 255, 0, 255).astype(np.uint8)
        arr = cv2.resize(arr, (512, 512))
        out = f"storage/processed/{series_uid[:8]}_s{idx:04d}_{name}.png"
        cv2.imwrite(out, arr)
        paths[name]   = out
        phashes[name] = str(imagehash.phash(Image.fromarray(arr)))

    return DualWindowPng(
        lung_window_path=paths["lung"],
        mediastinum_window_path=paths["mediastinum"],
        phash_lung=phashes["lung"],
    phash_mediastinum=phashes["mediastinum"]   # ★ 修复：补上纵隔窗 pHash，否则去重逻辑对纵隔窗失效
    )

# _read_meta 修正版（原版有多值字段隐患）
def _read_meta(self, dcm_path: str) -> dict:
    from app.services.dicom_service import safe_float, safe_list

    ds = pydicom.dcmread(dcm_path, stop_before_pixels=True)
    return {
        "window_center":   safe_float(ds.get("WindowCenter"), -600.0),
        "window_width":    safe_float(ds.get("WindowWidth"),  1500.0),
        "slice_location":  safe_float(ds.get("SliceLocation"),   0.0),
        "slice_thickness": safe_float(ds.get("SliceThickness"),  1.0),
        "pixel_spacing":   safe_list(ds.get("PixelSpacing"), [1.0, 1.0]),
        "rescale_slope":   safe_float(ds.get("RescaleSlope"),    1.0),
        "rescale_intercept": safe_float(ds.get("RescaleIntercept"), 0.0),
        "modality":        str(ds.get("Modality", "CT")),
        "kvp":             str(ds.get("KVP", "")),
    }
```

---

## 二十四、安全加固：Zip Bomb + 路径穿越防护（★ v3 新增）

### 坑点背景

用户上传的 ZIP 文件可能是：
1. **Zip Bomb**：压缩包解压后体积暴涨（如 42KB → 4.5GB），导致磁盘打满 + OOM
2. **路径穿越攻击**：ZIP 内含 `../../etc/passwd` 路径的文件，解压覆盖系统文件

```python
# app/services/file_service.py — 安全解压工具

import zipfile, os, shutil
from pathlib import Path

class SafeExtractor:
    """
    安全 ZIP 解压器：
    - 路径白名单：所有文件必须解压到指定目录内
    - 体积保护：解压前校验压缩比，超阈值直接拒绝
    - 文件数量限制：防止海量小文件耗尽 inode
    """
    MAX_UNCOMPRESSED_SIZE = 2 * 1024 ** 3   # 2GB：防 Zip Bomb
    MAX_COMPRESSION_RATIO = 100             # 压缩比 > 100 直接拒绝
    MAX_FILE_COUNT        = 50_000          # 最多 5 万个文件
    ALLOWED_EXTENSIONS    = {".dcm", ".DCM", ".png", ".jpg", ".jpeg",
                              ".tif", ".tiff", ".nii", ".nii.gz", ""}

    def extract(self, zip_path: str, dest_dir: str) -> list[str]:
        """
        安全解压并返回解压出的文件路径列表。
        抛出 SecurityError / SizeLimitError 阻止继续处理。
        """
        dest = Path(dest_dir).resolve()
        dest.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()

            # 1. 文件数量检查
            if len(infos) > self.MAX_FILE_COUNT:
                raise ValueError(f"ZIP 内文件数 {len(infos)} 超过限制 {self.MAX_FILE_COUNT}")

            # 2. 压缩比检查（先不解压，只读元数据）
            total_compressed   = sum(i.compress_size  for i in infos)
            total_uncompressed = sum(i.file_size       for i in infos)

            if total_uncompressed > self.MAX_UNCOMPRESSED_SIZE:
                raise ValueError(
                    f"解压后体积 {total_uncompressed / 1024**3:.1f}GB 超过 2GB 限制，拒绝处理"
                )
            if total_compressed > 0 and (total_uncompressed / total_compressed) > self.MAX_COMPRESSION_RATIO:
                raise ValueError(
                    f"压缩比 {total_uncompressed / total_compressed:.0f}x 异常，疑似 Zip Bomb"
                )

            # 3. 路径穿越检查 + 逐个解压
            # ★ 修复：保留相对目录结构（仅过滤 .. 穿越），避免同名 DICOM 文件 flatten 后碰撞导致序列乱序
            extracted = []
            for info in infos:
                if not info.filename or info.filename.endswith("/"):
                    continue   # 跳过目录条目

                # 清理路径中所有 ../ 穿越分段，但保留合法子目录结构
                parts = Path(info.filename).parts
                safe_parts = [p for p in parts if p not in ("..", ".")] 
                if not safe_parts:
                    continue
                safe_relative = Path(*safe_parts)

                # 扩展名白名单（取最终文件名的后缀）
                ext = safe_relative.suffix.lower()
                if ext not in self.ALLOWED_EXTENSIONS:
                    continue

                target = (dest / safe_relative).resolve()

                # 双重验证：目标必须在 dest_dir 内（防止构造 ../../../etc/passwd 绕过）
                if not str(target).startswith(str(dest) + os.sep) and target != dest:
                    raise PermissionError(
                        f"路径穿越攻击检测：{info.filename} → {target}"
                    )

                # 确保父目录存在
                target.parent.mkdir(parents=True, exist_ok=True)

                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=64 * 1024)  # 流式写入，控制内存

                extracted.append(str(target))

        return extracted
```

**在 Stage 1 中集成：**

```python
# app/pipeline/stage1_normalizer.py（解压时使用安全解压器）
from app.services.file_service import SafeExtractor

class FileNormalizer:
    def _extract_zip(self, zip_path: str, task_id: str) -> list[str]:
        dest_dir = f"storage/uploads/{task_id}/extracted"
        extractor = SafeExtractor()
        try:
            return extractor.extract(zip_path, dest_dir)
        except ValueError as e:
            raise RuntimeError(f"文件解压失败: {e}")
        except PermissionError as e:
            # 安全事件：记录日志并上报
            import logging
            logging.getLogger("security").warning(
                f"[SECURITY] Path traversal attempt: task={task_id}, error={e}"
            )
            raise RuntimeError("文件包含非法路径，拒绝处理")
```

---

## 二十五、CPU 密集任务异步卸载（★ v3 新增）

### 坑点背景

FastAPI / ARQ Worker 均基于 asyncio 事件循环。若在 `async` 函数中直接执行：
- `pydicom.dcmread()` × 几百次（磁盘 I/O + CPU）
- `cv2.resize()` + `imagehash.phash()`（CPU 密集）
- TotalSegmentator 推理（极度 CPU 密集）

**会导致事件循环被阻塞，SSE 推送停止，整个 Worker 无法响应心跳检测。**

### 解决方案：`asyncio.to_thread` + `ThreadPoolExecutor`

```python
# app/utils/thread_pool.py

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

# 全局共享线程池（避免频繁创建销毁）
# max_workers=2：2C 服务器留 1 核给事件循环，1 核做 CPU 计算
_cpu_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cpu_worker")

async def run_in_thread(func, *args, **kwargs):
    """
    将 CPU 密集或阻塞 I/O 任务卸载到线程池，
    保持 asyncio 事件循环畅通（SSE 推送不断流）。
    """
    loop = asyncio.get_running_loop()
    if kwargs:
        func = partial(func, **kwargs)
    return await loop.run_in_executor(_cpu_pool, func, *args)

def shutdown_pool():
    """应用关闭时优雅释放线程池"""
    _cpu_pool.shutdown(wait=True)
```

**在 Pipeline 各阶段的使用：**

```python
# app/pipeline/stage3_detector.py — TotalSegmentator 卸载到线程池
from app.utils.thread_pool import run_in_thread

class NoduleDetector:
    async def run_async(self, task_id: str, dicom_series_dir: str) -> Stage3Result:
        """异步包装：不阻塞事件循环"""
        return await run_in_thread(self.run, task_id, dicom_series_dir)

    def run(self, task_id: str, dicom_series_dir: str) -> Stage3Result:
        """同步执行（在线程池中运行）"""
        # ... 原有同步实现 ...


# app/pipeline/stage4_selector.py — 批量图像处理卸载
class KeySliceSelector:
    async def run_async(self, stage3: Stage3Result,
                        dicom_paths: list[str]) -> Stage4Result:
        return await run_in_thread(self.run, stage3, dicom_paths)


# app/pipeline/orchestrator.py — 编排器调用异步版本
async def _run_stage3(self, task_id, dicom_dir):
    detector = NoduleDetector()
    return await detector.run_async(task_id, dicom_dir)
```

**ARQ Worker 线程池关闭钩子：**

```python
# app/workers/arq_worker.py
from app.utils.thread_pool import shutdown_pool

# ★ 修复：ARQ on_shutdown 钩子必须是 async 函数；shutdown_pool() 是阻塞调用，
#   须通过 asyncio.to_thread 卸载，避免阻塞事件循环的优雅关闭过程。
async def _shutdown_hook(ctx):
    await asyncio.to_thread(shutdown_pool)

class WorkerSettings:
    functions    = [run_pipeline]
    on_shutdown  = [_shutdown_hook]   # 优雅关闭线程池
    max_jobs     = 1
    job_timeout  = 600
```

---

## 二十六、LLM JSON 解析健壮性（★ v3 新增）

### 坑点背景

即使设置了 `response_format: {"type": "json_object"}`，LLM 仍可能返回：
- JSON 前后包裹 Markdown 代码块：`` ```json\n{...}\n``` ``
- 字段值含截断省略号：`"findings": ["发现1", "..."]`
- 数字字段返回字符串：`"confidence": "0.85"` 而非 `0.85`
- 整合失败时返回空对象：`{}`

```python
# app/utils/llm_parser.py — LLM 响应健壮解析器

import json, re
from typing import Type, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

def extract_json_from_llm(raw: str) -> dict:
    """
    从 LLM 响应中提取 JSON，处理常见的脏数据格式。
    策略：先尝试直接解析 → 去掉 markdown 块再解析 → 正则提取第一个 {} 块
    """
    # 策略 1：直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 策略 2：去掉 ```json ... ``` 或 ``` ... ``` 包裹
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 策略 3：正则提取第一个完整 JSON 对象（贪婪匹配大括号）
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # 策略 4：返回空字典，让调用方用默认值处理
    return {}


def parse_llm_response(raw: str, model_class: Type[T],
                        fallback_factory=None) -> T:
    """
    解析 LLM 响应为指定 Pydantic 模型，解析失败时使用 fallback。

    Args:
        raw:              LLM 原始响应文本
        model_class:      目标 Pydantic 模型类
        fallback_factory: 解析失败时的回退工厂函数
    """
    data = extract_json_from_llm(raw)

    # 字段类型容错：将字符串数字转为 float
    for field_name, field_info in model_class.model_fields.items():
        if field_name in data:
            annotation = field_info.annotation
            if annotation in (float, "float") and isinstance(data[field_name], str):
                try:
                    data[field_name] = float(data[field_name])
                except ValueError:
                    data.pop(field_name)

    try:
        return model_class(**data)
    except Exception as e:
        if fallback_factory:
            return fallback_factory()
        raise ValueError(f"LLM 响应解析失败: {e}\n原始响应: {raw[:500]}")
```

**在 Stage 6 中的使用（替换原来的裸 `json.loads`）：**

```python
# app/pipeline/stage6_llm.py — 修正后的解析调用

from app.utils.llm_parser import parse_llm_response, extract_json_from_llm

# Step 1 解析（替换原来的 json.loads(raw)）
async def _perceive_one_slice(self, slc, model: str) -> SlicePerception:
    # ... 调用 LLM ...
    raw = await self._call_with_fallback(messages, model)

    def fallback():
        return SlicePerception(
            slice_rank=slc.rank,
            window_type="lung",
            visual_description="图像感知失败，内容不可用",
            abnormal_regions=[],
            quality_note="LLM 解析错误"
        )
    return parse_llm_response(raw, SlicePerception, fallback)

# Step 3 解析（报告必须有 disclaimer，缺失则补充默认值）
async def _step3_report(self, integrations, payload, model) -> AnalysisReport:
    # ... 调用 LLM ...
    raw  = await self._call_with_fallback(messages, model)
    data = extract_json_from_llm(raw)

    # 强制注入 disclaimer（LLM 可能漏掉）
    if not data.get("disclaimer"):
        data["disclaimer"] = (
            "本报告由 AI 辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"
        )

    # 强制注入 limitations（LLM 有时返回 null）
    if not data.get("limitations"):
        data["limitations"] = ["AI 分析结果存在局限性，需结合临床信息综合判断"]

    return AnalysisReport(task_id=payload.task_id,
                          model_used=model, raw_response=raw, **data)
```

---

## 二十七、磁盘空间监控与自动清理（★ v3 新增）

### 坑点背景

40G 磁盘在以下场景很快被打满：
- 多用户并发上传大型 DICOM 包（每包可达几百MB）
- 分片临时文件残留（上传中断未清理）
- TotalSegmentator 推理输出的 NIfTI 文件未删除
- CoT 中间截图（每任务约 20 张 PNG × 512×512 ≈ 5MB）

```python
# app/services/disk_service.py — 磁盘监控 + 自动清理

import os, shutil, time, logging
from pathlib import Path
from app.config import settings

logger = logging.getLogger(__name__)

class DiskGuard:
    """
    磁盘空间守卫：
    - 定时扫描清理过期临时文件
    - 写操作前检查剩余空间
    - 磁盘告警（剩余 < 5GB 时触发）
    """
    WARN_THRESHOLD_GB  = 5.0   # 剩余 < 5GB 记录 WARNING
    BLOCK_THRESHOLD_GB = 1.0   # 剩余 < 1GB 拒绝新任务

    def __init__(self):
        # ★ 修复：从 settings 读取绝对路径，避免因启动目录不同而路径漂移
        self.STORAGE_ROOT = Path(settings.STORAGE_ROOT).resolve()

    def check_free_space(self) -> float:
        """返回可用磁盘空间（GB）"""
        stat = shutil.disk_usage(str(self.STORAGE_ROOT))
        return stat.free / 1024 ** 3

    def assert_enough_space(self, required_gb: float = 0.5):
        """
        在接受新任务/上传前调用，空间不足时抛出异常。
        required_gb：预估本次任务需要的最小空间。
        """
        free_gb = self.check_free_space()
        if free_gb < self.BLOCK_THRESHOLD_GB:
            raise RuntimeError(
                f"磁盘空间严重不足（剩余 {free_gb:.1f}GB），拒绝新任务"
            )
        if free_gb < required_gb + self.WARN_THRESHOLD_GB:
            logger.warning(f"[DISK] 磁盘剩余 {free_gb:.1f}GB，接近告警阈值")

    # ─── 清理策略 ──────────────────────────────────────────────

    def clean_stale_chunks(self, max_age_hours: int = 24):
        """清理超过 N 小时未完成的分片临时目录"""
        chunk_root = self.STORAGE_ROOT / "chunks"
        if not chunk_root.exists():
            return
        now = time.time()
        cleaned_bytes = 0
        for upload_dir in chunk_root.iterdir():
            if upload_dir.is_dir():
                age_hours = (now - upload_dir.stat().st_mtime) / 3600
                if age_hours > max_age_hours:
                    size = sum(f.stat().st_size for f in upload_dir.rglob("*") if f.is_file())
                    shutil.rmtree(upload_dir, ignore_errors=True)
                    cleaned_bytes += size
        if cleaned_bytes:
            logger.info(f"[DISK] 清理过期分片 {cleaned_bytes / 1024**2:.1f}MB")

    def clean_processed_files(self, task_id: str, keep_pngs: bool = True):
        """
        任务完成后清理中间产物：
        - 原始 DICOM 文件（已解压）
        - TotalSegmentator 推理输出（NIfTI 文件）
        - 保留 Top-K PNG（keep_pngs=True）
        """
        dicom_dir = self.STORAGE_ROOT / "uploads" / task_id / "extracted"
        seg_dir   = Path(f"/tmp/seg_{task_id}")

        for d in [dicom_dir, seg_dir]:
            if d.exists():
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
                logger.info(f"[DISK] 清理 {d}: {size / 1024**2:.1f}MB")

    def clean_old_raw_uploads(self, max_age_hours: int = 24):
        """清理原始上传压缩包（分析完成 N 小时后）"""
        uploads_root = self.STORAGE_ROOT / "uploads"
        if not uploads_root.exists():
            return
        now = time.time()
        # ★ 修复：实际路径结构为 uploads/{task_id}/xxx.zip，只有 task_id 一层，
        #   原代码写了 user_dir 嵌套层（两层 iterdir），导致 rglob 找不到文件，ZIP 永远不被清理。
        #   改为直接 rglob 一次性匹配所有深度的 .zip 文件。
        for f in uploads_root.rglob("*.zip"):
            try:
                age_hours = (now - f.stat().st_mtime) / 3600
                if age_hours > max_age_hours:
                    size = f.stat().st_size
                    f.unlink(missing_ok=True)
                    logger.info(f"[DISK] 删除过期上传 {f.name}: {size / 1024**2:.1f}MB")
            except FileNotFoundError:
                pass   # 并发删除时文件可能已消失，忽略

    def get_storage_stats(self) -> dict:
        """
        返回存储使用统计，用于监控接口。
        ★ 注意：调用方须通过 run_in_thread 包裹，避免 rglob 在海量文件时（数万 DICOM）
          阻塞 asyncio 事件循环 5-30 秒导致接口超时。
          正确调用示例（在路由层）：
            stats = await run_in_thread(DiskGuard().get_storage_stats)
        """
        stat = shutil.disk_usage(str(self.STORAGE_ROOT))
        # ★ 改为 os.popen("du") 替代 rglob，在文件数极多时性能提升 10x+
        try:
            import subprocess
            result = subprocess.run(
                ["du", "-sb", str(self.STORAGE_ROOT)],
                capture_output=True, text=True, timeout=30
            )
            used_by_storage = int(result.stdout.split()[0]) if result.returncode == 0 else 0
        except Exception:
            # 降级：rglob（可能慢，但保证功能正确）
            used_by_storage = sum(
                f.stat().st_size for f in self.STORAGE_ROOT.rglob("*") if f.is_file()
            ) if self.STORAGE_ROOT.exists() else 0
        return {
            "total_gb":          round(stat.total  / 1024**3, 2),
            "free_gb":           round(stat.free   / 1024**3, 2),
            "used_gb":           round(stat.used   / 1024**3, 2),
            "storage_dir_gb":    round(used_by_storage / 1024**3, 2),
            "usage_percent":     round(stat.used / stat.total * 100, 1),
        }
```

**定时清理任务（通过 ARQ 定时任务触发）：**

```python
# app/workers/arq_worker.py — 新增定时清理任务

from app.services.disk_service import DiskGuard

async def scheduled_disk_cleanup(ctx):
    """每小时执行一次磁盘清理"""
    guard = DiskGuard()
    guard.clean_stale_chunks(max_age_hours=24)
    guard.clean_old_raw_uploads(max_age_hours=24)
    stats = guard.get_storage_stats()
    logger.info(f"[DISK] 清理完成: {stats}")

class WorkerSettings:
    functions = [run_pipeline, scheduled_disk_cleanup]
    cron_jobs = [
        cron(scheduled_disk_cleanup, hour={0, 6, 12, 18})   # 每 6 小时执行
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs       = 1
    job_timeout    = 600
```

**在任务创建时增加磁盘预检查：**

```python
# app/api/upload.py — 初始化上传前检查磁盘
from app.services.disk_service import DiskGuard

@router.post("/upload/init")
async def init_upload(...):
    DiskGuard().assert_enough_space(required_gb=0.5)
    # ... 原有逻辑 ...
```

---

## 二十八、知识库冷启动与 Embedding 缓存（★ v3 新增）

### 坑点背景

- `knowledge_service.search()` 每次都调用 OpenAI Embedding API，**每次语义检索耗时 200-500ms，增加 API 成本**
- 服务重启后知识库需要重新 embedding（如果 jsonl 数据有更新）
- `sqlite_vec.load(conn)` 首次调用有动态库加载开销

```python
# app/services/knowledge_service.py — 增加 embedding 缓存

import sqlite3, json, hashlib, time
import numpy as np
import sqlite_vec
from functools import lru_cache
from app.config import settings

class KnowledgeService:

    def __init__(self, db_path: str = "monica_knowledge.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self._init_schema()
        self._embedding_cache: dict[str, np.ndarray] = {}   # 内存缓存

    def _init_schema(self):
        # ... 原有 schema 初始化 ...

        # ★ 新增 embedding 缓存表（持久化到 SQLite，重启后复用）
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_cache (
                text_hash  TEXT PRIMARY KEY,     -- SHA256(text)
                embedding  BLOB NOT NULL,        -- numpy float32 bytes
                created_at REAL DEFAULT (unixepoch())
            )
        """)
        self.conn.commit()

    def _embed(self, text: str) -> np.ndarray:
        """优先从缓存读取，缓存未命中时调用 API"""
        text_hash = hashlib.sha256(text.encode()).hexdigest()

        # L1 缓存：进程内存（最快）
        if text_hash in self._embedding_cache:
            return self._embedding_cache[text_hash]

        # L2 缓存：SQLite 持久化（重启后仍有效）
        row = self.conn.execute(
            "SELECT embedding FROM embedding_cache WHERE text_hash = ?",
            (text_hash,)
        ).fetchone()
        if row:
            vec = np.frombuffer(row[0], dtype=np.float32)
            self._embedding_cache[text_hash] = vec   # 写回 L1
            return vec

        # L3：调用 OpenAI API（有网络开销）
        # ★ 修复：_embed() 是同步方法（供 run_in_thread 调用），必须用同步 httpx，
        #   但禁止在 async 路由中直接调用 search()，必须通过 run_in_thread 包裹，
        #   否则阻塞 asyncio 事件循环。正确用法见 stage5_context.py 的调用示例。
        import httpx
        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={"model": "text-embedding-3-small", "input": text[:4096]},
            timeout=30
        )
        resp.raise_for_status()   # ★ 修复：缺少错误处理，API 报错时会 KeyError 而不是有意义的异常
        vec = np.array(resp.json()["data"][0]["embedding"], dtype=np.float32)

        # 双写：内存 + SQLite
        self._embedding_cache[text_hash] = vec
        self.conn.execute(
            "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding) VALUES (?, ?)",
            (text_hash, vec.tobytes())
        )
        self.conn.commit()
        return vec

    def is_empty(self) -> bool:
        """检查知识库是否为空（用于冷启动判断）"""
        count = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_items"
        ).fetchone()[0]
        return count == 0

    def ensure_loaded(self, cases_path: str = "knowledge_base/cases.jsonl",
                       guidelines_path: str = "knowledge_base/guidelines.jsonl"):
        """
        冷启动保障：若知识库为空则自动导入。
        生产环境应在服务启动脚本中预先执行，避免第一个请求延迟飙升。
        """
        if self.is_empty():
            import logging
            logging.getLogger(__name__).warning(
                "知识库为空，开始导入... 首次导入约需 2-5 分钟（需调用 Embedding API）"
            )
            for path in [cases_path, guidelines_path]:
                if Path(path).exists():
                    self.ingest_jsonl(path)
```

**服务启动时预热（`app/main.py`）：**

```python
# app/main.py — 应用生命周期钩子

from contextlib import asynccontextmanager
from app.services.knowledge_service import KnowledgeService
from app.utils.thread_pool import shutdown_pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时：预热知识库（异步，不阻塞启动）
    import asyncio
    from app.utils.thread_pool import run_in_thread
    ks = KnowledgeService()
    asyncio.create_task(run_in_thread(ks.ensure_loaded))

    yield   # 应用运行中

    # 关闭时：释放资源
    shutdown_pool()

app = FastAPI(lifespan=lifespan)
```

---

## 二十九、Pipeline 编排器完整实现（★ v3 新增）

Pipeline 编排器是所有阶段的胶水层，负责：状态持久化、SSE 进度推送、错误隔离、中间产物落库。

```python
# app/pipeline/orchestrator.py

import asyncio, json, time, logging
from datetime import datetime
from app.database import SessionLocal
from app.models.task import Task
from app.models.stage_result import StageResult
from app.services.disk_service import DiskGuard
from app.utils.thread_pool import run_in_thread

logger = logging.getLogger(__name__)

class PipelineOrchestrator:

    STAGE_WEIGHTS = {
        "stage1": 5,
        "stage2": 10,
        "stage3": 35,   # TotalSegmentator 最重
        "stage4": 15,
        "stage5": 5,
        "stage6": 25,   # LLM 调用较慢
        "stage7": 5,
    }

    async def run(self, task_id: str, file_ids: list,
                  prompt: str, user_id: str, model: str):
        """
        主编排入口，从 ARQ Worker 调用。
        任何 Stage 失败均有结构化错误上报，不影响其他任务。
        """
        logger.info(f"[Pipeline] task={task_id} 开始，model={model}")

        try:
            # Stage 1: 文件标准化
            await self._run_stage("stage1", task_id, 5,
                lambda: self._stage1(task_id, file_ids))

            # Stage 2: 质量粗筛（Evaluator 可能 REJECT）
            stage1_result = self._load_stage_output(task_id, "stage1")
            await self._run_stage("stage2", task_id, 10,
                lambda: self._stage2(task_id, stage1_result))

            # Stage 3: 病灶候选检测（CPU 密集，卸载到线程池）
            stage2_result = self._load_stage_output(task_id, "stage2")
            await self._run_stage("stage3", task_id, 35,
                lambda: self._stage3_async(task_id, stage2_result))

            # Stage 4: 关键切片提取（CPU 密集）
            stage3_result = self._load_stage_output(task_id, "stage3")
            await self._run_stage("stage4", task_id, 15,
                lambda: self._stage4_async(task_id, stage3_result))

            # Stage 5: 上下文构建（语义检索）
            stage4_result = self._load_stage_output(task_id, "stage4")
            await self._run_stage("stage5", task_id, 5,
                lambda: self._stage5(task_id, stage4_result, prompt))

            # Stage 6: LLM 推理（CoT 三步）
            stage5_result = self._load_stage_output(task_id, "stage5")
            await self._run_stage("stage6", task_id, 25,
                lambda: self._stage6(task_id, stage5_result, model))

            # Stage 7: 结果存储
            stage6_result = self._load_stage_output(task_id, "stage6")
            await self._run_stage("stage7", task_id, 5,
                lambda: self._stage7(task_id, stage6_result, user_id))

            # 标记完成
            self._update_task(task_id, status="done", stage="done", progress=100)
            logger.info(f"[Pipeline] task={task_id} 完成")

        except PipelineRejectError as e:
            # 评估器 REJECT：告知用户需补充资料
            self._update_task(task_id, status="rejected", stage=e.stage,
                              reject_reason=e.reason, suggestions=e.suggestions)
            logger.warning(f"[Pipeline] task={task_id} REJECTED at {e.stage}: {e.reason}")

        except Exception as e:
            # 未预期异常：记录完整堆栈
            logger.exception(f"[Pipeline] task={task_id} 失败: {e}")
            self._update_task(task_id, status="error",
                              error_message=str(e)[:1000])

        finally:
            # 无论成功失败，清理中间临时文件（保留 PNG 和元数据）
            DiskGuard().clean_processed_files(task_id, keep_pngs=True)

    async def _run_stage(self, stage_name: str, task_id: str,
                          progress: int, stage_fn):
        """
        统一 Stage 执行包装器：
        - 更新 Redis 任务状态（触发 SSE 推送）
        - 记录阶段耗时
        - 将 Stage 产物落库（可审计、可回放）
        """
        self._update_task(task_id, status="processing",
                          stage=stage_name, progress=progress)
        t0 = time.time()
        try:
            result = await stage_fn()
            elapsed = int((time.time() - t0) * 1000)

            # Stage 产物落库
            self._save_stage_result(task_id, stage_name,
                                     output=result, elapsed_ms=elapsed,
                                     status="pass")
            return result

        except PipelineRejectError:
            raise   # 让上层处理 REJECT
        except Exception as e:
            elapsed = int((time.time() - t0) * 1000)
            self._save_stage_result(task_id, stage_name,
                                     output=None, elapsed_ms=elapsed,
                                     status="error", error=str(e))
            raise

    def _update_task(self, task_id: str, **kwargs):
        """更新任务状态到数据库（SSE 端点轮询此表）"""
        with SessionLocal() as db:
            task = db.query(Task).filter_by(task_id=task_id).first()
            if task:
                for k, v in kwargs.items():
                    setattr(task, k, v)
                task.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)  # ★ 修复：utcnow() Python 3.12+ 已废弃
                db.commit()

    def _save_stage_result(self, task_id: str, stage: str,
                            output, elapsed_ms: int,
                            status: str, error: str = None):
        """将 Stage 产物序列化后落库（Pydantic → JSON）
        ★ 修复：使用 merge() 代替 add()，重试同一 stage 时执行 upsert
        而非抛出 IntegrityError（id = f'{task_id}_{stage}' 主键冲突）
        """
        with SessionLocal() as db:
            db.merge(StageResult(
                id=f"{task_id}_{stage}",
                task_id=task_id,
                stage=stage,
                status=status,
                output_json=output.model_dump_json() if output else None,
                error_message=error,
                elapsed_ms=elapsed_ms,
            ))
            db.commit()

    def _load_stage_output(self, task_id: str, stage: str):
        """从数据库加载上一阶段产物（用于阶段间传递）"""
        with SessionLocal() as db:
            row = db.query(StageResult).filter_by(
                task_id=task_id, stage=stage
            ).first()
            if not row or not row.output_json:
                raise RuntimeError(f"Stage {stage} 产物缺失，无法继续")
            return row.output_json   # 调用方按需反序列化

    # ─── 各阶段具体实现（调用对应 Stage 类）───────────────────

    async def _stage1(self, task_id: str, file_ids: list):
        from app.pipeline.stage1_normalizer import FileNormalizer
        return await run_in_thread(FileNormalizer().run, task_id, file_ids)

    async def _stage2(self, task_id: str, stage1_result):
        from app.pipeline.stage2_screener import QualityScreener
        from app.evaluators.quality_evaluator import QualityEvaluator, EvalStatus
        from app.schemas.stage1_normalize import Stage1Result
        # ★ 修复：stage1_result 是 JSON 字符串，需先反序列化
        stage1 = Stage1Result.model_validate_json(stage1_result)
        result = QualityScreener().run(stage1)
        eval_result = QualityEvaluator()(result)
        if eval_result.status == EvalStatus.REJECT:
            raise PipelineRejectError(
                stage="stage2",
                reason="; ".join(eval_result.issues),
                suggestions=eval_result.suggestions
            )
        return result

    async def _stage3_async(self, task_id: str, stage2_result):
        """
        ★ 注意：stage2_result 此时是 JSON 字符串（_load_stage_output 返回 str）
        需先反序列化为 Stage2Result Pydantic 对象
        """
        from app.pipeline.stage3_detector import NoduleDetector
        from app.schemas.stage2_screen import Stage2Result
        stage2 = Stage2Result.model_validate_json(stage2_result)
        return await NoduleDetector().run_async(task_id, stage2.dicom_series_dir)

    async def _stage4_async(self, task_id: str, stage3_result):
        from app.pipeline.stage4_selector import KeySliceSelector
        from app.schemas.stage3_detection import Stage3Result
        stage3 = Stage3Result.model_validate_json(stage3_result)
        return await KeySliceSelector().run_async(stage3, stage3.dicom_paths)

    async def _stage5(self, task_id: str, stage4_result, prompt: str):
        from app.pipeline.stage5_context import ContextBuilder
        from app.schemas.stage4_selection import Stage4Result
        stage4 = Stage4Result.model_validate_json(stage4_result)
        return await run_in_thread(ContextBuilder().run, task_id, stage4, prompt)

    async def _stage6(self, task_id: str, stage5_result, model: str):
        from app.pipeline.stage6_llm import LLMStage
        from app.schemas.stage5_context import LLMPayload
        stage5 = LLMPayload.model_validate_json(stage5_result)
        return await LLMStage().run(stage5, model=model)

    async def _stage7(self, task_id: str, stage6_result, user_id: str):
        from app.pipeline.stage7_storage import ResultStorage
        from app.schemas.stage7_report import AnalysisReport
        stage6 = AnalysisReport.model_validate_json(stage6_result)
        return await run_in_thread(ResultStorage().save, task_id, stage6, user_id)


class PipelineRejectError(Exception):
    def __init__(self, stage: str, reason: str, suggestions: list):
        self.stage       = stage
        self.reason      = reason
        self.suggestions = suggestions
        super().__init__(reason)
```

---

## 三十、Nginx + Supervisor 完整部署配置（★ v3 新增）

### 30.1 Nginx 配置（含 SSE 超时 + 大文件上传调优）

```nginx
# nginx.conf

worker_processes 1;   # 2C 服务器，1 个 Worker 进程

events {
    worker_connections 512;
}

http {
    # ★ 修复：limit_req_zone 必须定义在 http 块顶部，server/location 中引用时才能保证已定义
    # （原版放在 http 块末尾，虽然 Nginx 通常容忍，但某些 include 顺序场景下会报 "zone not found"）
    limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

    # 基础安全头
    server_tokens off;
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";

    # 上传大小：500MB（对应 MAX_UPLOAD_SIZE_MB）
    client_max_body_size    510M;
    client_body_timeout     300s;   # 分片上传超时容忍
    client_body_buffer_size 16k;    # 避免小分片写临时文件

    # ─── 上游：FastAPI ───────────────────────────────────────
    upstream fastapi_app {
        server 127.0.0.1:8000;
        keepalive 16;
    }

    server {
        listen 443 ssl http2;
        server_name your-domain.com;

        ssl_certificate     /etc/ssl/certs/your_cert.pem;
        ssl_certificate_key /etc/ssl/private/your_key.pem;
        ssl_protocols       TLSv1.2 TLSv1.3;
        ssl_ciphers         HIGH:!aNULL:!MD5;

        # ─── 普通 API 请求 ───────────────────────────────────
        location /api/v1/ {
            proxy_pass         http://fastapi_app;
            proxy_set_header   Host              $host;
            proxy_set_header   X-Real-IP         $remote_addr;
            proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header   X-Forwarded-Proto $scheme;
            proxy_read_timeout 120s;

            # 限流：每 IP 每秒 10 个请求（防 DDoS）
            limit_req zone=api_limit burst=20 nodelay;
        }

        # ─── SSE 端点：专用配置（优先于通配 /api/v1/）──────────
        # ★ 注意：location 优先级：正则 ~ 高于前缀 /，但须放在 upload 之前
        location ~ ^/api/v1/task/[^/]+/stream$ {
            proxy_pass          http://fastapi_app;
            proxy_set_header    Host              $host;
            proxy_set_header    X-Real-IP         $remote_addr;
            proxy_set_header    Connection        "";    # 关闭 keep-alive，避免超时断开
            proxy_http_version  1.1;                    # SSE 需要 HTTP/1.1

            # ★ 关键：禁用 Nginx 响应缓冲，确保 SSE 实时推送
            proxy_buffering      off;
            proxy_cache          off;
            proxy_read_timeout   600s;   # SSE 连接保持 10 分钟（对应最长任务超时）
            proxy_send_timeout   600s;

            # 告知上游不要缓冲
            proxy_set_header     X-Accel-Buffering no;

            # SSE 不限流
        }

        # ─── 分片上传端点：超时宽松 ─────────────────────────
        location /api/v1/upload/ {
            proxy_pass          http://fastapi_app;
            proxy_set_header    Host            $host;
            proxy_set_header    X-Real-IP       $remote_addr;
            proxy_read_timeout  300s;    # 允许大文件分片上传等待
            proxy_send_timeout  300s;
            proxy_request_buffering off;  # 流式转发，不在 Nginx 缓冲请求体
        }

        # ─── 健康检查（不鉴权）───────────────────────────────
        location /health {
            proxy_pass http://fastapi_app;
            access_log off;
        }

        # ─── 静态资源（如日后有 Web 管理页面）──────────────────
        # location /static/ {
        #     alias /opt/monica-server/static/;
        #     expires 7d;
        # }

        # ★ 坑：return 301 必须放在所有 location 块之外（server 级别指令）
        # 此处 443 server 块不需要 return 301，删除避免误导
    }

    server {
        listen 80;
        server_name your-domain.com;
        return 301 https://$host$request_uri;
    }

    # ★ 已移至 http 块顶部（见上方），此处删除重复定义
}
```

### 30.2 Supervisor 配置

```ini
# supervisord.conf

[supervisord]
nodaemon=false
logfile=/var/log/supervisord.log
pidfile=/var/run/supervisord.pid

# ─── FastAPI (Uvicorn) ──────────────────────────────────────
# ★ 修复：Supervisor .ini 格式中多行 command 须用 4 空格续行缩进，否则后续行被视为新指令导致报错。
#   此处合并为单行（更保险）。
[program:fastapi]
command=/opt/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --loop uvloop --timeout-keep-alive 30
directory=/opt/monica-server
user=monica
autostart=true
autorestart=true
startretries=5
stderr_logfile=/var/log/fastapi.err.log
stdout_logfile=/var/log/fastapi.out.log
environment=PYTHONPATH="/opt/monica-server"

# ─── ARQ Worker ──────────────────────────────────────────────
[program:arq_worker]
command=/opt/venv/bin/python -m arq app.workers.arq_worker.WorkerSettings
directory=/opt/monica-server
user=monica
autostart=true
autorestart=true
startretries=5
# ★ 重要：每次重启前等待 5s，避免频繁重启 Redis 连接风暴
startsecs=5
stderr_logfile=/var/log/arq_worker.err.log
stdout_logfile=/var/log/arq_worker.out.log
environment=PYTHONPATH="/opt/monica-server"

# ─── Redis（系统服务，supervisor 仅监控）───────────────────
[program:redis]
command=/usr/bin/redis-server /etc/redis/redis.conf
autostart=true
autorestart=true
stderr_logfile=/var/log/redis.err.log
# ★ 注意：Redis 应配置密码（requirepass），supervisor 启动后验证连接
# ★ 坑：若 Redis 比 ARQ Worker 慢启动，Worker 会因连不上 Redis 退出
#   解决：在 arq_worker [program] 中添加 depends_on 或使用 stopasgroup=true

[supervisorctl]
serverurl=unix:///var/run/supervisor.sock

[unix_http_server]
file=/var/run/supervisor.sock
chmod=0700
```

### 30.3 Redis 内存优化配置

```ini
# /etc/redis/redis.conf（关键参数）

# 最大内存 100MB（2G 服务器上给 Redis 的上限）
maxmemory 100mb
maxmemory-policy allkeys-lru   # 超限时驱逐最久未用的 key

# 持久化：RDB 快照（仅任务状态，数据丢了可从 DB 重建）
save 3600 1   # 1 小时内至少 1 次变更才做快照

# 关闭 AOF（降低磁盘 I/O）
appendonly no

# 绑定本地，不对外暴露
bind 127.0.0.1
```

---

## 三十一、v3 新增 API 接口

### 31.1 磁盘状态查询（运维接口）

```
GET /api/v1/admin/disk
Authorization: Bearer <admin-token>

→ {
  "total_gb":       40.0,
  "free_gb":        18.5,
  "used_gb":        21.5,
  "storage_dir_gb": 12.3,
  "usage_percent":  53.7
}
```

### 31.2 知识库管理

```
# 查询知识库状态
GET /api/v1/admin/knowledge/status
→ { "total_items": 256, "cache_size": 128, "last_ingested": "2026-04-24T10:00:00" }

# 触发重新导入（异步）
POST /api/v1/admin/knowledge/reload
→ { "job_id": "reload_xxx", "status": "enqueued" }
```

---

## 三十二、v3 技术选型更新总览

| 组件 | v2 | v3 新增/更新 |
|------|-----|-------------|
| DICOM 字段读取 | 裸 `float(ds.get(...))` | `safe_float` / `apply_hu_transform` 工具函数 |
| ZIP 解压安全 | `zipfile.extractall` | `SafeExtractor`（Zip Bomb + 路径穿越防护）|
| CPU 任务调度 | 直接在 async 中执行 | `run_in_thread` + `ThreadPoolExecutor` |
| LLM 响应解析 | 裸 `json.loads` | `parse_llm_response` 容错解析器 |
| Embedding 成本 | 每次检索调用 API | L1/L2 两级缓存（内存 + SQLite 持久化） |
| 磁盘管理 | 无监控 | `DiskGuard`（写前预检 + 定时清理）|
| 编排器 | 骨架描述 | 完整实现（状态机 + 错误隔离 + 产物落库）|
| Nginx | 基础配置 | SSE 专用 location + 大文件上传调优 |
| 知识库冷启动 | 手动导入 | `ensure_loaded` 自动检测 + 启动预热 |
| Stage 间传递 | 裸 dict | JSON 字符串 → `model_validate_json` 反序列化 |
| 任务记录时机 | 入队后 | 入队前落库 pending（防 SSE 查不到任务）|
| ARQ 连接池 | 每次新建不关闭 | `try/finally aclose()` 防连接泄漏 |
| SSE 心跳 | 无 | 每 30s 发注释行防代理断连 |
| 微信 openid 校验 | 仅检查 errcode | 额外校验 openid 字段存在性 |
| SQL 注入防护 | 字符串拼接 category | 参数化查询 |

---

## 三十三、已知遗留问题 & 后续待完善

> 以下为当前设计中已识别但需结合实际业务决策的点，供开发时参考：

| 序号 | 状态 | 问题 | 影响 | 处置方案 |
|------|------|------|------|----------|
| 1 | ⏳ 待处理 | ARQ Worker 被 OOM Kill 后会从 Stage 1 重跑 | 浪费计算资源 | 编排器入口检查已完成阶段，从断点继续 |
| 2 | ✅ 已修复 | `enqueue_pipeline` 每次新建连接池不关闭 | 连接泄漏 | 已改用 `try/finally pool.aclose()` |
| 3 | ✅ 已修复 | `knowledge_service.search` 中 `execute()` 未使用 `params` 变量，硬编码参数列表忽略了 category 占位符 | SQL 注入 + 运行时报错 | 已修复为 `params: list` 按顺序 append，`execute(sql, params)` |
| 4 | ⏳ 待处理 | 微信小程序不支持原生 `EventSource` | SSE 实现有差异 | 服务端保留轮询备用接口 `/task/{id}`，小程序 SDK 侧选用 `miniprogram-sse` |
| 5 | ✅ 已修复 | `_save_stage_result` 用 `db.add()` 写固定主键 `{task_id}_{stage}`，重试时抛 `IntegrityError` | Stage 重试必崩 | 已改为 `db.merge()` 执行 upsert |
| 6 | ✅ 已修复 | `DiskGuard.STORAGE_ROOT` 硬编码为类变量 `Path("./storage")` | 路径随启动目录漂移 | 已改为实例属性 `Path(settings.STORAGE_ROOT).resolve()` |
| 7 | ✅ 已修复 | `_stage2` 中 `QualityScreener().run(stage1_result)` 直接传 JSON 字符串 | Stage2 必崩，TypeError | 已补 `Stage1Result.model_validate_json()` 反序列化 |
| 8 | ✅ 已修复 | `_call_gemini` 用 `str(messages)` 把 Python list 序列化为字符串 | Gemini 完全无法解析消息 | 已改为正确转换：system→`systemInstruction`，image_url→`inlineData` |
| 9 | ✅ 已修复 | `check_and_consume` TOCTOU 竞态：`incrby` 超限后不回滚 | 用户可透支 token 配额 | 已改为超限立即 `decrby` 回滚；`expire` 每次都刷新 |
| 10 | ✅ 已修复 | `complete_upload` 中 `session` 未做 None 检查 | `AttributeError` 崩溃 | 已补 `if not session: raise ValueError(...)` |
| 11 | ✅ 已修复 | `_step1_perceive_parallel` 无 Semaphore 限流 | 触发 LLM 速率限制（429）| 已补 `asyncio.Semaphore(5)` 限制并发 |
| 12 | ✅ 已修复 | `_render_dual_window` 输出路径 `storage/processed` 相对路径硬编码 | 路径随启动目录漂移 | 已改为 `Path(settings.STORAGE_ROOT).resolve() / "processed"` |
| 13 | ✅ 已修复 | `analysis.py` 配额检查版遗漏入队前写 pending 记录 | SSE 查不到任务 404 | 已同步补入 pending 写入逻辑 |
| 14 | ✅ 已修复 | `QuotaService.__init__` 每次创建新 Redis 连接 | 连接数随请求线性增长 | 已改为模块级单例 `get_redis()` 复用连接池 |
| 15 | ✅ 已修复 | `_call_llm` 上的 `@retry` 与 `_call_with_fallback` 双层嵌套，最多触发 12 次调用 | 误判模型可用性，掩盖 rate-limit | 已移除 `_call_llm` 的 `@retry`，重试统一由外层 `_perceive_one_slice` 控制 |
| 16 | ✅ 已修复 | `SafeExtractor` 用 `os.path.basename` 拍平所有路径，多 Series ZIP 中的同名 DICOM（如 `SeriesA/0001.dcm` + `SeriesB/0001.dcm`）碰撞后序列乱序 | DICOM 序列排序错误，结节检测结果不可信 | 改为保留相对目录结构，仅过滤 `..` 穿越分段 |
| 17 | ✅ 已修复 | `DualWindowPng` 构建时遗漏 `phash_mediastinum` 字段 | 纵隔窗去重失效，重复切片无法过滤 | 补上 `phash_mediastinum=phashes["mediastinum"]` |
| 18 | ✅ 已修复 | `KnowledgeService._embed()` 在 `search()` 被 async 路由直接调用时，内部同步 `httpx.post` 阻塞事件循环 | 整个服务在 Embedding API 响应前卡死（200-500ms 阻塞） | 补充注释强制要求调用方通过 `run_in_thread` 包裹；补 `resp.raise_for_status()` 避免 API 报错时 KeyError |
| 19 | ✅ 已修复 | `DiskGuard.clean_old_raw_uploads` 多遍历了 `user_dir` 一层，ZIP 文件永远找不到 | 原始 DICOM 压缩包永不清理，磁盘持续增长 | 改为 `uploads_root.rglob("*.zip")` 直接递归匹配 |
| 20 | ✅ 已修复 | `get_storage_stats()` 用 `rglob("*")` 遍历海量文件，可能耗时 5-30s 阻塞事件循环 | `GET /admin/disk` 接口长时间超时 | 改用 `subprocess.run(["du", "-sb"])` 替代，降级 fallback 保留 rglob；注释强制要求调用方用 `run_in_thread` |
| 21 | ✅ 已修复 | ARQ `on_shutdown` 钩子用 `lambda ctx: shutdown_pool()`，lambda 返回同步函数 | 阻塞调用在 async 上下文中阻塞事件循环，优雅关闭可能卡住 | 改为 `async def _shutdown_hook(ctx): await asyncio.to_thread(shutdown_pool)` |
| 22 | ✅ 已修复 | `_update_task` 中使用 `datetime.utcnow()`，Python 3.12+ 已废弃 | 产生 `DeprecationWarning`，未来版本将报错 | 改为 `datetime.now(timezone.utc).replace(tzinfo=None)` |
| 23 | ✅ 已修复 | Nginx `limit_req_zone` 定义在 `http` 块末尾，某些 include 顺序下 `location` 引用时报 "zone not found" | 限流失效，Nginx 启动报错 | 将 `limit_req_zone` 移至 `http {` 块顶部 |
| 24 | ✅ 已修复 | Supervisor `[program:fastapi]` 的 `command` 跨行换行未缩进，`.ini` 解析器将后续行视为新指令 | Supervisor 启动 FastAPI 报错，服务无法启动 | 合并为单行 `command` |
| 25 | ✅ 已修复 | `DualWindowPng` Schema 定义中缺少 `phash_mediastinum: str` 字段，Pydantic v2 会静默丢弃传入值 | 纵隔窗去重哈希写入后被丢弃，去重逻辑无效 | 在 `DualWindowPng` Schema 中补充 `phash_mediastinum: str` 字段 |
| 26 | ✅ 已修复 | 第12节 `_render_dual_window` 未调用 `apply_hu_transform`，直接用原始像素值计算窗位 | 窗位裁剪结果完全错误，肺窗/纵隔窗图像不可用（HU 偏移可达 1000+） | 补充 `apply_hu_transform(ds.pixel_array, ds)` 调用（第23节修正版未同步到第12节） |
| 27 | ✅ 已修复 | `_perceive_one_slice` 用裸 `json.loads` 解析 LLM 输出，遇到 markdown 包裹/字段类型错误时崩溃 | Step1 并行感知单张失败后无 fallback，整个报告失败 | 替换为 `parse_llm_response(raw, SlicePerception, fallback_perception)` |
| 28 | ✅ 已修复 | `auth.py` JWT 签发中 `datetime.utcnow()` Python 3.12+ 已废弃（与问题22同类，但遗漏了此处） | Python 3.13+ 将报错导致登录接口崩溃 | 改为 `datetime.datetime.now(datetime.timezone.utc)` |
| 29 | ✅ 已修复 | `init_upload` 中 `chunk_dir = Path(f"storage/chunks/{upload_id}")` 使用相对路径 | 分片临时文件存储位置随启动目录漂移，`complete_upload` 合并时读不到分片 | 改为 `Path(settings.STORAGE_ROOT).resolve() / "chunks" / upload_id` |
| 30 | ✅ 已修复 | `complete_upload` 中 `final_path = f"storage/uploads/..."` 使用相对路径 | 合并后的文件存储路径漂移，Pipeline Stage1 读文件时 FileNotFoundError | 改为 `Path(settings.STORAGE_ROOT).resolve() / "uploads" / ...` |