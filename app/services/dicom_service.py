"""
DICOM 字段安全读取工具函数。

DICOM 标准允许很多字段以 MultiValue 形式存在，直接 float(ds.get(...))
在字段为列表时会抛 TypeError。本模块提供统一的安全访问接口。
"""
import pydicom
import numpy as np
from pathlib import Path
from typing import Optional, List, Tuple

try:
    from pydicom.multival import MultiValue
    from pydicom.sequence import Sequence as DicomSequence
except ImportError:
    MultiValue = list
    DicomSequence = list


# ── 安全类型转换 ───────────────────────────────────────────────────

def safe_float(value, default: float = 0.0) -> float:
    """安全转换 DICOM 字段为 float，MultiValue 取第一个元素"""
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return float(value[0]) if len(value) > 0 else default
    try:
        # MultiValue 继承自 list，上面已覆盖；这里处理 pydicom DS 类型
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_list(value, default: Optional[List[float]] = None) -> List[float]:
    """安全转换 DICOM 字段为 float 列表"""
    if value is None:
        return default or []
    if isinstance(value, (list, tuple)):
        try:
            return [float(v) for v in value]
        except (ValueError, TypeError):
            return default or []
    try:
        return [float(value)]
    except (ValueError, TypeError):
        return default or []


# ── HU 转换 ────────────────────────────────────────────────────────

def apply_hu_transform(pixel_array: np.ndarray, ds: pydicom.Dataset) -> np.ndarray:
    """
    应用 RescaleSlope / RescaleIntercept 将像素值转为真实 HU 值。
    若字段缺失则假设 slope=1, intercept=0（原样返回）。

    ⚠️ 不做此转换，阈值检测的 HU 范围会完全错乱！
    """
    slope     = safe_float(ds.get("RescaleSlope"),     default=1.0)
    intercept = safe_float(ds.get("RescaleIntercept"), default=0.0)
    return pixel_array.astype(np.float32) * slope + intercept


def get_window_params(
    ds: pydicom.Dataset,
    preferred_wc: float = -600.0,
    preferred_ww: float = 1500.0,
):
    """
    读取 DICOM 内嵌窗位参数，多窗位序列取第一个（最通用的显示窗位）。
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
    return safe_float(ds.get("SliceLocation"), default=0.0)


# ── DICOM PII 脱敏 ────────────────────────────────────────────────

import hashlib

_PII_TAGS_TO_BLANK = [
    "PatientName", "PatientID", "PatientBirthDate",
    "PatientAddress", "PatientTelephoneNumbers",
    "InstitutionName", "InstitutionAddress",
    "ReferringPhysicianName", "OperatorsName",
]

_PII_TAGS_TO_HASH = [
    "PatientID",
]


def deidentify_dicom(ds: pydicom.Dataset) -> pydicom.Dataset:
    """
    原地脱敏 DICOM 数据集（在落盘前调用）：
    - PatientID：SHA256 单向 Hash（保留关联性，去除原始值）
    - 其余 PII 字段：置为空字符串
    """
    pid = str(ds.get("PatientID", ""))
    for tag in _PII_TAGS_TO_BLANK:
        if hasattr(ds, tag):
            try:
                setattr(ds, tag, "")
            except Exception:
                pass
    # PatientID 用 hash 替换（方便同患者关联，但不可逆推）
    if pid:
        hashed = hashlib.sha256(pid.encode()).hexdigest()[:16]
        try:
            ds.PatientID = hashed
        except Exception:
            pass
    return ds


# ── 肺野提取（Stage3 / Stage4 共用）─────────────────────────────

def extract_lung_mask(
    hu: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    从 HU 数组中提取体内掩膜和肺野空气掩膜（Stage3/Stage4 共用逻辑）。

    预处理流程：
      1. body_mask：排除体外空气（HU > -950），去除小对象、闭运算、填洞
      2. lung_air：在 body_mask 内找低密度肺野（-950 < HU < -300），去小对象、闭运算

    返回：
      (body_mask, lung_air) — 均为 bool 数组，形状与 hu 相同

    注意：此函数纯 CPU 密集，应在线程池中调用（run_in_thread）以避免阻塞事件循环。
    """
    from skimage import morphology
    from scipy import ndimage as ndi

    # Step 1: 体内掩膜（排除体外空气）
    body_mask: np.ndarray = hu > -950
    body_mask = morphology.remove_small_objects(body_mask, min_size=1000)
    body_mask = morphology.binary_closing(body_mask, morphology.disk(5))
    body_mask = ndi.binary_fill_holes(body_mask)

    # Step 2: 肺野空气区域（-950 < HU < -300，在体内）
    lung_air: np.ndarray = (hu > -950) & (hu < -300) & body_mask
    lung_air = morphology.remove_small_objects(lung_air, min_size=500)
    lung_air = morphology.binary_closing(lung_air, morphology.disk(3))

    return body_mask, lung_air


# ── 序列排序 + 文件读取 ───────────────────────────────────────────

def sort_dicom_files_by_z(paths: List[str]) -> List[str]:
    """按 Z 轴位置（切片位置）升序排列 DICOM 文件"""
    def z_key(p: str) -> float:
        try:
            ds = pydicom.dcmread(p, stop_before_pixels=True)
            return get_image_position_z(ds)
        except Exception:
            return 0.0
    return sorted(paths, key=z_key)


def read_dicom_meta(dcm_path: str) -> dict:
    """读取 DICOM 元数据（不读像素，节省内存）"""
    ds = pydicom.dcmread(dcm_path, stop_before_pixels=True)
    return {
        "window_center":     safe_float(ds.get("WindowCenter"),    -600.0),
        "window_width":      safe_float(ds.get("WindowWidth"),     1500.0),
        "slice_location":    safe_float(ds.get("SliceLocation"),      0.0),
        "slice_thickness":   safe_float(ds.get("SliceThickness"),     1.0),
        "pixel_spacing":     safe_list(ds.get("PixelSpacing"),   [1.0, 1.0]),
        "rescale_slope":     safe_float(ds.get("RescaleSlope"),       1.0),
        "rescale_intercept": safe_float(ds.get("RescaleIntercept"),   0.0),
        "modality":          str(ds.get("Modality", "CT")),
        "kvp":               str(ds.get("KVP", "")),
        "series_uid":        str(ds.get("SeriesInstanceUID", "")),
        "image_position_z":  get_image_position_z(ds),
    }
