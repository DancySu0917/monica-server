"""
文件服务：
1. SafeExtractor  - 安全 ZIP 解压（Zip Bomb + 路径穿越防护）
2. FileService    - 分片上传状态管理（init / save_chunk / complete）
3. DiskGuard      - 磁盘空间监控与自动清理
"""
import os
import hashlib
import shutil
import time
import logging
import subprocess
from pathlib import Path
from typing import List

from app.config import settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# SafeExtractor：安全 ZIP 解压
# ══════════════════════════════════════════════════════════════════

class SafeExtractor:
    """
    安全 ZIP 解压器：
    - 路径白名单：目标文件必须在指定目录内
    - Zip Bomb 防护：校验解压前总体积和压缩比
    - 文件数量限制：防止海量小文件耗尽 inode
    - 保留相对目录结构：避免同名 DICOM flatten 后乱序
    """
    MAX_UNCOMPRESSED_SIZE = 2 * 1024 ** 3   # 2 GB
    MAX_COMPRESSION_RATIO = 100
    MAX_FILE_COUNT        = 50_000
    ALLOWED_EXTENSIONS    = {
        ".dcm", ".png", ".jpg", ".jpeg",
        ".tif", ".tiff", ".nii", ".gz", ""
    }

    def extract(self, zip_path: str, dest_dir: str) -> List[str]:
        """
        安全解压并返回解压出的文件路径列表。
        抛出 ValueError / PermissionError 阻止继续处理。
        """
        import zipfile
        dest = Path(dest_dir).resolve()
        dest.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = zf.infolist()

            # 1. 文件数量检查
            if len(infos) > self.MAX_FILE_COUNT:
                raise ValueError(
                    f"ZIP 内文件数 {len(infos)} 超过限制 {self.MAX_FILE_COUNT}"
                )

            # 2. 压缩比检查（不解压，只读元数据）
            total_compressed   = sum(i.compress_size for i in infos)
            total_uncompressed = sum(i.file_size      for i in infos)

            if total_uncompressed > self.MAX_UNCOMPRESSED_SIZE:
                raise ValueError(
                    f"解压后体积 {total_uncompressed / 1024**3:.1f}GB 超过 2GB 限制"
                )
            if total_compressed > 0 and (total_uncompressed / total_compressed) > self.MAX_COMPRESSION_RATIO:
                raise ValueError(
                    f"压缩比 {total_uncompressed / total_compressed:.0f}x 异常，疑似 Zip Bomb"
                )

            # 3. 路径穿越检查 + 逐个解压（保留目录结构）
            extracted = []
            for info in infos:
                if not info.filename or info.filename.endswith("/"):
                    continue  # 跳过目录条目

                # 清理所有 ../ 穿越分段，保留合法子目录结构
                parts = Path(info.filename).parts
                safe_parts = [p for p in parts if p not in ("..", ".")]
                if not safe_parts:
                    continue
                safe_relative = Path(*safe_parts)

                # 扩展名白名单
                ext = safe_relative.suffix.lower()
                # .nii.gz 特殊处理
                if safe_relative.name.endswith(".nii.gz"):
                    ext = ".gz"
                if ext not in self.ALLOWED_EXTENSIONS:
                    continue

                target = (dest / safe_relative).resolve()

                # 双重验证：目标必须在 dest 内
                if not str(target).startswith(str(dest) + os.sep) and target != dest:
                    raise PermissionError(
                        f"路径穿越攻击检测：{info.filename} → {target}"
                    )

                target.parent.mkdir(parents=True, exist_ok=True)

                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=64 * 1024)

                extracted.append(str(target))

        return extracted


# ══════════════════════════════════════════════════════════════════
# FileService：分片上传管理
# ══════════════════════════════════════════════════════════════════

class FileService:

    CHUNK_SIZE = settings.CHUNK_SIZE_MB * 1024 * 1024

    def init_upload(
        self,
        user_id: str,
        filename: str,
        total_size: int,
        total_chunks: int,
        file_sha256: str,
    ) -> dict:
        from app.models.file_record import FileRecord
        from app.models.upload_session import UploadSession

        # 去重：同 SHA256 已存在则跳过上传（占位符 SHA256 不做去重）
        PLACEHOLDER_SHA256 = "0" * 64
        if file_sha256 != PLACEHOLDER_SHA256:
            with SessionLocal() as db:
                existing = db.query(FileRecord).filter_by(file_hash=file_sha256).first()
                if existing:
                    return {
                        "upload_id":    None,
                        "already_exists": True,
                        "file_id":      file_sha256,   # 统一字段名，与 complete 响应一致
                    }

        upload_id = f"up_{os.urandom(8).hex()}"
        chunk_dir = Path(settings.STORAGE_ROOT).resolve() / "chunks" / upload_id
        chunk_dir.mkdir(parents=True, exist_ok=True)

        session = UploadSession(
            upload_id=upload_id,
            user_id=user_id,
            filename=filename,
            total_size=total_size,
            total_chunks=total_chunks,
            file_sha256=file_sha256,
            chunk_dir=str(chunk_dir),
        )
        with SessionLocal() as db:
            db.add(session)
            db.commit()

        return {"upload_id": upload_id, "already_exists": False}

    def save_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        data: bytes,
        user_id: str = "",
    ) -> List[int]:
        """保存单个分片，返回已接收分片列表。user_id 非空时校验归属。"""
        from app.models.upload_session import UploadSession

        with SessionLocal() as db:
            session = db.query(UploadSession).filter_by(upload_id=upload_id).first()
            if not session:
                raise ValueError(f"上传会话 {upload_id} 不存在或已过期")
            # 归属校验：防止越权写入他人分片
            if user_id and session.user_id != user_id:
                raise PermissionError("无权操作该上传会话")
            if not (0 <= chunk_index < session.total_chunks):
                raise ValueError(
                    f"chunk_index {chunk_index} 超出范围 [0, {session.total_chunks - 1}]"
                )
            chunk_dir = session.chunk_dir

        chunk_path = Path(chunk_dir) / f"chunk_{chunk_index:06d}"
        chunk_path.write_bytes(data)

        # 返回已接收的分片编号
        received = sorted(
            int(p.stem.split("_")[1])
            for p in Path(chunk_dir).glob("chunk_*")
        )
        return received

    def get_received_chunks(self, upload_id: str, user_id: str = "") -> List[int]:
        """查询已上传分片（断点续传用）。user_id 非空时校验归属。"""
        from app.models.upload_session import UploadSession

        with SessionLocal() as db:
            session = db.query(UploadSession).filter_by(upload_id=upload_id).first()
            if not session:
                raise ValueError(f"上传会话 {upload_id} 不存在")
            # 归属校验
            if user_id and session.user_id != user_id:
                raise PermissionError("无权查看该上传会话")
            chunk_dir = session.chunk_dir

        return sorted(
            int(p.stem.split("_")[1])
            for p in Path(chunk_dir).glob("chunk_*")
        )

    def complete_upload(self, upload_id: str, user_id: str = "") -> str:
        """合并所有分块，校验 SHA256，返回最终文件路径。user_id 非空时校验归属。"""
        from app.models.upload_session import UploadSession
        from app.models.file_record import FileRecord

        with SessionLocal() as db:
            session = db.query(UploadSession).filter_by(upload_id=upload_id).first()
            if not session:
                raise ValueError(f"上传会话 {upload_id} 不存在或已过期，请重新初始化上传")
            # 归属校验
            if user_id and session.user_id != user_id:
                raise PermissionError("无权操作该上传会话")
            # 快照所有字段（避免 session 关闭后访问 detached 对象）
            total_chunks = session.total_chunks
            chunk_dir    = session.chunk_dir
            user_id      = session.user_id
            file_sha256  = session.file_sha256
            filename     = session.filename

        final_path = str(
            Path(settings.STORAGE_ROOT).resolve()
            / "uploads" / user_id
            / f"{file_sha256[:8]}_{filename}"
        )
        Path(final_path).parent.mkdir(parents=True, exist_ok=True)

        sha256 = hashlib.sha256()
        with open(final_path, "wb") as out:
            for i in range(total_chunks):
                chunk_file = Path(chunk_dir) / f"chunk_{i:06d}"
                if not chunk_file.exists():
                    os.remove(final_path)
                    raise ValueError(f"分片 {i} 缺失，请重新上传")
                chunk_data = chunk_file.read_bytes()
                out.write(chunk_data)
                sha256.update(chunk_data)

        actual_hash = sha256.hexdigest()
        # 若客户端传入的 file_sha256 是真实值（64位非占位），则做完整性校验
        # 若客户端传入全零占位符，则跳过校验，以服务端实际计算值为准
        PLACEHOLDER_SHA256 = "0" * 64
        if file_sha256 != PLACEHOLDER_SHA256 and actual_hash != file_sha256:
            os.remove(final_path)
            raise ValueError("文件完整性校验失败（SHA256 不匹配），请重新上传")
        # 始终使用服务端计算的真实 SHA256 作为文件唯一标识
        file_sha256 = actual_hash

        # 清理分块临时目录
        shutil.rmtree(chunk_dir, ignore_errors=True)

        # 记录 FileRecord
        with SessionLocal() as db:
            from app.models.file_record import FileRecord
            if not db.query(FileRecord).filter_by(file_hash=file_sha256).first():
                db.add(FileRecord(
                    file_hash=file_sha256,
                    file_type="unknown",
                    storage_path=final_path,
                    size_bytes=Path(final_path).stat().st_size,
                ))
            # 标记 session 完成
            sess = db.query(UploadSession).filter_by(upload_id=upload_id).first()
            if sess:
                sess.status = "complete"
            db.commit()

        return final_path, file_sha256


# ══════════════════════════════════════════════════════════════════
# DiskGuard：磁盘监控 + 自动清理
# ══════════════════════════════════════════════════════════════════

class DiskGuard:
    """磁盘空间守卫：写前检查 + 定时清理过期文件"""

    WARN_THRESHOLD_GB  = 5.0
    BLOCK_THRESHOLD_GB = 1.0

    def __init__(self):
        self.STORAGE_ROOT = Path(settings.STORAGE_ROOT).resolve()

    def check_free_space(self) -> float:
        stat = shutil.disk_usage(str(self.STORAGE_ROOT))
        return stat.free / 1024 ** 3

    def assert_enough_space(self, required_gb: float = 0.5):
        free_gb = self.check_free_space()
        if free_gb < self.BLOCK_THRESHOLD_GB:
            raise RuntimeError(
                f"磁盘空间严重不足（剩余 {free_gb:.1f}GB），拒绝新任务"
            )
        if free_gb < required_gb + self.WARN_THRESHOLD_GB:
            logger.warning(f"[DISK] 磁盘剩余 {free_gb:.1f}GB，接近告警阈值")

    def clean_stale_chunks(self, max_age_hours: int = 24):
        """清理超过 N 小时未完成的分片临时目录"""
        chunk_root = self.STORAGE_ROOT / "chunks"
        if not chunk_root.exists():
            return
        now = time.time()
        cleaned_bytes = 0
        for upload_dir in chunk_root.iterdir():
            if not upload_dir.is_dir():
                continue
            age_hours = (now - upload_dir.stat().st_mtime) / 3600
            if age_hours > max_age_hours:
                size = sum(f.stat().st_size for f in upload_dir.rglob("*") if f.is_file())
                shutil.rmtree(upload_dir, ignore_errors=True)
                cleaned_bytes += size
        if cleaned_bytes:
            logger.info(f"[DISK] 清理过期分片 {cleaned_bytes / 1024**2:.1f}MB")

    def clean_processed_files(self, task_id: str, keep_pngs: bool = True):
        """任务完成后清理中间产物（原始 DICOM + TotalSegmentator 输出）"""
        dicom_dir = self.STORAGE_ROOT / "uploads" / task_id / "extracted"
        # TotalSegmentator 输出目录与 stage3_detector 保持一致（不再使用 /tmp）
        seg_dir   = self.STORAGE_ROOT / "processed" / task_id / "totalseg_tmp"

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
        for f in uploads_root.rglob("*.zip"):
            try:
                age_hours = (now - f.stat().st_mtime) / 3600
                if age_hours > max_age_hours:
                    size = f.stat().st_size
                    f.unlink(missing_ok=True)
                    logger.info(f"[DISK] 删除过期上传 {f.name}: {size / 1024**2:.1f}MB")
            except FileNotFoundError:
                pass

    def get_storage_stats(self) -> dict:
        """
        返回存储使用统计。
        注意：调用方须通过 run_in_thread 包裹，避免 rglob 阻塞事件循环。
        """
        stat = shutil.disk_usage(str(self.STORAGE_ROOT))
        try:
            result = subprocess.run(
                ["du", "-sb", str(self.STORAGE_ROOT)],
                capture_output=True, text=True, timeout=30
            )
            used_by_storage = int(result.stdout.split()[0]) if result.returncode == 0 else 0
        except Exception:
            used_by_storage = sum(
                f.stat().st_size for f in self.STORAGE_ROOT.rglob("*") if f.is_file()
            ) if self.STORAGE_ROOT.exists() else 0
        return {
            "total_gb":       round(stat.total  / 1024**3, 2),
            "free_gb":        round(stat.free   / 1024**3, 2),
            "used_gb":        round(stat.used   / 1024**3, 2),
            "storage_dir_gb": round(used_by_storage / 1024**3, 2),
            "usage_percent":  round(stat.used / stat.total * 100, 1),
        }
