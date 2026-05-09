# Monica Server — 肺部 CT 分析 Pipeline 详细技术文档

> 本文档详细描述用户上传 DCM 压缩包后，系统每一步的处理逻辑、筛选标准、数据流向和最终输出。

---

## 一、整体流程概览

```
用户上传 ZIP(DCM) 压缩包
         │
         ▼
  [Stage 1] 文件标准化        ← 解压、类型识别、SHA256去重、DICOM脱敏
         │
         ▼
  [Stage 2] 影像质量筛查      ← 切片数量、模态、Z轴缺口检测
         │  失败 → 拒绝任务
         ▼
  [Stage 3] 结节候选检测      ← HU阈值+形态过滤，全量扫描所有切片
         │
         ▼
  [Stage 4] 切片选择 + 渲染   ← 病灶显著性评分 + 双窗位PNG渲染 + 物理标尺+候选框标注 + pHash去重
         │
         ▼
  [Stage 5] Payload 组装      ← 结节候选筛选 + 用户提示词构建
         │
         ▼
  [Stage 6] CoT 三步推理      ← Step1:切片感知 → Step2:跨片整合 → Step3:报告生成
         │
         ▼
  [Stage 7] 结果落库          ← 持久化报告 + 质量评估 + 清理中间产物
         │
         ▼
    最终报告返回用户
```

整个 Pipeline 由 ARQ Worker 异步执行，FastAPI 主进程通过 SSE 推送进度（5% → 10% → 20% → 40% → 55% → 65% → 90% → 100%）。

---

## 二、Stage 1：文件标准化

**入口文件：** `app/pipeline/stage1_normalizer.py`

### 2.1 文件接收

用户上传 ZIP 压缩包（包含若干 `.dcm` 文件）后，系统先将文件保存至：
```
storage/uploads/{user_id}/{文件名}
```

### 2.2 安全解压（SafeExtractor）

- 仅解压至指定目录 `storage/processed/{task_id}/extracted/`
- 防路径穿越攻击（过滤 `../` 等危险路径）
- 解压后对每个文件进行类型检测

### 2.3 文件类型识别

| 扩展名 | 判断方式 | 类型 |
|--------|---------|------|
| `.dcm` 或无扩展名 | 尝试 pydicom 读取头部 | `DICOM_SINGLE` |
| `.png/.jpg/.tif` 等 | 扩展名匹配 | `PLAIN_IMAGE` |
| `.zip` | 扩展名匹配 | `ARCHIVE` |

### 2.4 SHA256 文件去重

- 对每个文件计算 SHA256 哈希值
- 查询 `file_records` 表，相同哈希视为重复文件
- **重复文件依然处理**，只是不重复注册数据库记录（避免反复落库）

### 2.5 DICOM 脱敏

对每个 DICOM 文件调用 `deidentify_dicom(ds)`：
- 清除患者姓名（PatientName）
- 清除患者 ID（PatientID）
- 清除出生日期（PatientBirthDate）
- 保留所有影像数据（像素、窗位、位置信息不变）
- 以 `write_like_original=True` 回写，保留原始传输语法（避免压缩格式解码失败）

### 2.6 元数据提取

从每张 DICOM 提取：
- `series_uid`：序列 UID
- `modality`：影像模态（预期为 CT）
- `slice_thickness`：切片厚度（mm）
- `pixel_spacing`：像素间距 [行间距, 列间距]（mm/px）
- `window_center` / `window_width`：DICOM 推荐窗位
- `image_position_z`：Z 轴空间坐标（用于排序）

### 2.7 输出

```json
Stage1Result {
  "task_id": "xxx",
  "normalized_files": [...],
  "total_dicom_slices": 120,
  "file_summary": "共处理 120 个文件，DICOM 切片 120 张"
}
```

---

## 三、Stage 2：影像质量筛查

**入口文件：** `app/pipeline/stage2_screener.py`

### 3.1 DICOM 文件收集

除了 Stage1 识别的文件外，还递归扫描 `extracted/` 目录，确保不遗漏子目录中的 `.dcm` 文件。

### 3.2 Z 轴排序

调用 `sort_dicom_files_by_z()`：
- 读取每张 DICOM 的 `ImagePositionPatient[2]`（Z轴坐标）
- 按 Z 坐标从小到大排序（从脚到头，符合放射学惯例）
- 排序后保证切片在解剖位置上连续

### 3.3 质量检查

#### 切片数量门槛
```python
passed = len(dcm_files) >= 5
```
- **< 5 张**：直接拒绝，任务状态置为 `rejected`
- 理由：少于 5 张无法构成有效 CT 序列

#### 切片缺口检测
```python
MIN_SLICE_THICKNESS_GAP_RATIO = 2.5
```
- 计算相邻切片的 Z 轴间距
- 取所有间距的中位数作为基准
- 若某段间距 > 中位数 × 2.5，视为存在缺口
- 缺口不会导致拒绝，但会记录在 `quality_issues` 中作为警告

### 3.4 输出

```json
Stage2Result {
  "dicom_series_dir": "storage/processed/xxx/extracted/",
  "series_uid": "1.2.3.xxx",
  "slice_count": 120,
  "modality": "CT",
  "quality_score": 0.85,
  "quality_issues": [],
  "passed": true
}
```

**若 `passed=false`，Pipeline 立即终止，返回错误提示给用户。**

---

## 四、Stage 3：结节候选检测

**入口文件：** `app/pipeline/stage3_detector.py`

### 4.1 检测策略（双轨降级）

**首选：TotalSegmentator**（若已安装）
- 使用 AI 肺部分割模型，在分割出的肺部 mask 内找结节
- CPU fast 模式，约 5~10 分钟

**降级：HU 阈值 + 形态过滤**（默认使用）
- 不依赖外部模型，纯数值计算，速度快
- 以下详述此方案

### 4.2 全量扫描

- **全部切片**均被扫描，不受批次限制截断
- 每批 64 张分批读取，避免 OOM

### 4.3 逐切片检测流程

#### 步骤 1：提取肺野 ROI
```
body_mask   = HU > -950                          # 排除体外空气
lung_air    = HU ∈ (-950, -300) & body_mask      # 肺气体区域
```

#### 步骤 2：双肺野验证（三级策略）

核心目的：排除腹部切片（腹部肠气也能触发 HU 阈值）。

| 总肺气像素 | 判断标准 |
|-----------|---------|
| ≥ 50,000 px | 只需 2 个面积相当的独立区域（次大/最大 ≥ 15%）|
| 35,000~50,000 px | 需要双侧分布 + 质心间距 ≥ 20%（或双侧质心且间距 ≥ 8%）|
| 30,000~35,000 px | 宽松条件：次大区域 ≥ 1000px 且比例 ≥ 10%（肺尖区域）|
| < 30,000 px | 直接跳过（非肺部切片）|

#### 步骤 3：扩展 ROI
```
lung_roi = 对 lung_air 膨胀 10px   # 包含紧邻肺边缘的结节
```

#### 步骤 4：病灶候选分割
```
GGN候选   = HU ∈ (-700, -250) & lung_roi    # 磨玻璃结节范围
实性候选  = HU ∈ (-250, +200) & lung_roi    # 亚实性/实性结节范围
combined  = GGN候选 ∪ 实性候选
```
- 去除 < 25px 的微小区域（去噪）
- 形态学闭运算填充小孔洞

#### 步骤 5：连通区域过滤

对每个候选区域逐一检查以下 6 个过滤条件，**全部通过才保留**：

| 条件 | 标准 | 过滤目的 |
|------|------|---------|
| 等效直径（像素） | 3px ~ 45px | 排除噪声和肺门大血管 |
| 真实直径（mm） | 3mm ~ 30mm | 排除肿块（>30mm）和微小噪声 |
| 圆形度 | ≥ 0.10 | 排除细长血管断面 |
| 长宽比 | ≤ 3.5 | 排除线状血管 |
| 肺内重叠率 | ≥ 5% | 排除纵隔结构（主动脉等）|
| 密度对比度 | 均值HU - 背景第10百分位 ≥ 20HU | 排除正常肺实质变异 |

#### 步骤 6：置信度计算

置信度由 4 个子评分加权融合：

```
size_score        = 高斯评分（最优直径15mm时最高）
shape_score       = min(1.0, circularity / 0.5)
lung_position_score = min(1.0, lung_overlap × 2.0)
density_score     = min(1.0, hu_contrast / 400)   # GGN
                    min(1.0, hu_contrast / 500)   # solid

elongation_penalty = max(0, 1 - (aspect_ratio - 1) / 4)

confidence = (size×0.20 + shape×0.25 + position×0.20 + density×0.20 + bonus) × penalty
```

- GGN：大GGN（≥10mm 且 圆形度≥0.15）额外 +0.10
- **最低门槛 < 0.15 的候选直接丢弃**

### 4.4 候选后处理

#### 血管去重（连续切片检测）
- 如果同一坐标位置（精度 10px）在 ≥ 3 个连续切片出现候选 → 判定为血管
- 将这些候选的置信度降至 40%（不完全删除，可能是血管旁真结节）

#### 分段采样（防止假阳性堆积）
- 将所有切片分为 10 个区段
- 每区段最多保留 5 个切片，每切片最多贡献 2 个候选
- 最终返回 **≤ 50 个候选**，按置信度 + 直径排序

### 4.5 输出

```json
Stage3Result {
  "has_nodule_candidates": true,
  "candidates": [
    {
      "candidate_id": "a1b2c3d4",
      "slice_index": 67,
      "bbox_x": 0.32,       // 归一化坐标（相对图像宽度）
      "bbox_y": 0.45,
      "bbox_w": 0.06,
      "bbox_h": 0.05,
      "estimated_diameter_mm": 16.2,
      "confidence": 0.73,
      "density_type": "solid"   // 注意：此处仅供内部参考，不传给LLM
    }
  ],
  "dicom_paths": ["path/to/slice_067.dcm", ...],   // 按Z轴排序的全部DICOM路径
  "total_slices_scanned": 120
}
```

---

## 五、Stage 4：切片选择 + 双窗位渲染

**入口文件：** `app/pipeline/stage4_selector.py`

### 5.1 设计目标

从 120+ 张切片中，选出 **TOP_K（默认10）张**最有价值的切片渲染为 PNG 发给大模型。

核心挑战：Stage3 可能漏检真实病灶，不能只依赖 Stage3 的候选结果。

> **配置说明：** `TOP_K_SLICES` 默认值为 10（本地开发环境也已从 5 调整为 10）。张数越多覆盖率越高，但 Token 消耗等比增加；10 张是精度与成本的均衡点。

### 5.2 三层评分策略

#### 第一层：病灶显著性评分（最重要，独立于 Stage3）

**新增函数 `_compute_abnormality_score(dcm_path)` 对每张切片直接分析图像特征：**

| 维度 | 检测方法 | 权重 |
|------|---------|------|
| 结节/局灶病灶 | 肺野内 3~35mm、圆形度≥0.08、密度增高≥15HU 的区域，按大小累加评分 | **45%** |
| 磨玻璃影(GGO) | HU(-750,-350) 在肺野内的面积占比，超过27%满分 | 20% |
| 实变/肺炎 | HU(-100,+100) 大面积分布，超过18%肺野满分 | 20% |
| 胸腔积液 | 图像下部 35% 区域内 HU(0,80) 的像素数量 | 10% |
| 肺气肿/大疱 | HU < -900 占肺野比例，超过25%满分 | 5% |

```
abnormality_score = Σ(各维度得分 × 权重)  ∈ [0.0, 1.0]

切片最终评分 += abnormality_score × 3.5   // 最多贡献 3.5 分
```

**并行计算**：使用 `ThreadPoolExecutor(max_workers=4)` 对采样池中所有切片并行评分。

#### 第二层：Stage3 结节候选额外加分

Stage3 候选作为补充确认（不再是主要分数来源）：

| Stage3 候选直径 | 额外基础分 |
|---------------|---------|
| ≥ 15mm | +2.0 ~ +2.5 |
| 10~15mm | +1.5 ~ +1.75 |
| < 10mm | +1.0 |

- 腹部假阳性（无双肺野验证）：**-2.0 分**

#### 第三层：全局均匀性奖励

- 将切片按 TOP_K 个区段划分
- 若当前切片所在区段尚未被选中：+0.5 分
- 已有切片的区段：+0.1 分
- 与已选切片的最小距离越远，额外 +0 ~ 0.3 分（鼓励覆盖不同区域）

### 5.3 双轨选片

```
采样池 = 均匀采样(top_k × 6 个位置) ∪ 所有有效Stage3候选

轨道A（高分候选） = 评分前 60% × top_k 个
轨道B（均匀补充） = top_k 个均匀采样（填补轨道A未覆盖的区域）

合并去重 → 按分数排序 → 取前 top_k × 2 个作为候选池（供pHash去重消耗）
```

### 5.4 pHash 去重

- 对每张候选切片计算感知哈希（pHash）
- 两张图的**汉明距离 < 8** 视为视觉重复，丢弃后者
- 确保最终选出的 10 张切片视觉上各不相同

### 5.5 双窗位渲染

每张选中的切片渲染 **3 张 PNG**：

| 图像 | 窗位参数 | 用途 |
|------|---------|------|
| 肺窗 | WC=-600, WW=1200 | 标准肺实质显示，主要参考图 |
| 纵隔窗 | WC=+40, WW=400 | 纵隔结构、心脏、大血管 |
| 窄窗位 | WC=-500, WW=600 | 提高低密度异常（GGO、小病灶）对比度 |

**渲染细节：**
- 线性映射到 [0, 255]：`pixel = (HU - low) / (high - low) × 255`
- 先生成灰度图，再 **转换为 RGB 三通道**（提高视觉 LLM 识别精度）
- 统一缩放至 **512×512 PNG**，使用 LANCZOS 高质量插值

**图像叠加标注（帮助 LLM 精确测量结节大小）：**

| 标注类型 | 出现位置 | 说明 |
|---------|---------|------|
| 黄色物理标尺 | 三张图均有，底部 | 每格 = 10mm，LLM 可直接对比结节与刻度读出毫米数 |
| 彩色候选框 | 仅肺窗图 | 红/橙/绿/蓝矩形框标出算法候选位置，框右上角标签"候选N ~Xmm"提示算法估计直径（最多标注4个）|

**标尺物理精度说明：**

```
来源：DICOM 元数据中的 PixelSpacing 和 Rows 字段（由扫描仪写入，物理精确）

计算公式：
  原始视野 FOV = PixelSpacing × Rows         （真实物理尺寸，单位 mm）
  渲染后每mm像素数 = 512 / FOV
  标尺1格（10mm）的像素长度 = 10 × 512 / FOV

示例（标准胸部 CT）：
  PixelSpacing=0.703mm，Rows=512 → FOV=360mm
  标尺1格 = 10 × 512 / 360 ≈ 14px → 实际代表 10mm（误差 < 0.05mm）

注意：FOV 使用原始图像行数（Rows）而非硬编码 512，
      可正确处理 256×256、768×768 等非标准尺寸 CT。
```

**元数据一并记录：**
```json
{
  "pixel_spacing": [0.703, 0.703],
  "fov_mm": 360.0,                   // = pixel_spacing × orig_rows（精确计算，非硬编码）
  "window_center": -600,
  "window_width": 1200
}
```

### 5.6 输出

```json
Stage4Result {
  "selected_slices": [
    {
      "slice_index": 67,
      "rank": 0,
      "score": 4.23,
      "dual_window": {
        "lung_window_path": "storage/processed/xxx/slices/slice_0067_lung.png",
        "mediastinum_window_path": "...",
        "ggn_window_path": "..."
      },
      "slice_location_mm": -45.2,
      "slice_thickness_mm": 1.25,
      "nodule_candidates": [...],   // Stage3在此切片检测到的候选
      "dicom_metadata": {
        "pixel_spacing": [0.703, 0.703],
        "fov_mm": 360.0
      },
      "selection_reason": "结节候选切片（优先选取）"
    }
  ],
  "nodule_coverage_rate": 0.85
}
```

---

## 六、Stage 5：Payload 组装

**入口文件：** `app/pipeline/stage5_context.py`

### 6.1 结节候选筛选（三层优先级）

从 Stage3 的全部候选中，按以下优先级选出 **≤ 25 个**传给 LLM：

| 优先级 | 规则 | 上限 |
|-------|------|------|
| 第一优先 | 按直径降序，最大的 10 个 | 10 个 |
| 第二优先 | 按置信度降序，最高的 5 个（去重后补充）| 累计 20 个 |
| 第三优先 | Stage4 选中切片上的候选（确保图文一致性）| 累计 25 个 |

**关键原则：传给 LLM 的候选只提供位置坐标和大小，不传递密度类型（不告诉 LLM 是 solid 还是 GGN）。**

### 6.2 用户提示词构建

```
请对以下肺部 CT 影像进行专业分析，基于图像视觉内容判断结节的密度类型和大小。

算法初步检测到 结节候选（切片67，直径约16.2mm，置信度0.73）

请按 JSON 格式输出完整分析报告...
```

注意：提示词中**不包含** "实性结节" / "磨玻璃结节" 等密度描述，防止算法的 HU 判断污染 LLM 的视觉判断。

### 6.3 知识库检索（辅助，非必须）

- 用结节描述文本在 sqlite-vec 向量数据库中检索相似案例和指南条目
- 失败时静默降级，不影响主流程

### 6.4 LLM Payload 结构

```json
LLMPayload {
  "user_prompt": "请对以下肺部CT影像...",
  "selected_slices": [
    {
      "rank": 0,
      "dual_window": {"lung_window_path": "...", "ggn_window_path": "..."},
      "nodule_candidates": [{"bbox_x": 0.32, "estimated_diameter_mm": 16.2, ...}],
      "dicom_metadata": {"pixel_spacing": [0.703, 0.703], "fov_mm": 360.0},
      "slice_thickness_mm": 1.25
    }
  ],
  "nodule_description": {
    "nodules": [
      {"estimated_diameter_mm": 16.2, "confidence": 0.73, "slice_index": 67, ...}
    ]
  }
}
```

---

## 七、Stage 6：CoT 三步推理（核心）

**入口文件：** `app/pipeline/stage6_llm.py`

这是整个 Pipeline 最核心的阶段，采用三步 Chain-of-Thought 推理，每步均调用大模型。

### 7.1 Step 1：并行全肺感知

**并发**对每张选中切片（最多 TOP_K 张）发起独立的 LLM 请求。

#### 发给 LLM 的内容（每张切片）

**图像：**
1. 肺窗 PNG（512×512 RGB）— base64 编码
2. 窄窗位 PNG（512×512 RGB）— base64 编码

**文字提示（System）：**
```
你是一名资深胸部影像科医生，专长肺部CT全面分析。

【CT影像学阅片方向——必须记住】
- 图像左侧 = 患者右肺，图像右侧 = 患者左肺（放射学惯例）

【需要检查的9大类异常】
1. 结节/肿块：大小、密度（实性/混合/纯磨玻璃）、边界、形态
   - 实性结节：高密度白色团块，遮蔽血管
   - 混合磨玻璃(mGGN)：磨玻璃背景中有实性成分
   - 纯磨玻璃(pGGN)：淡薄云雾状，不遮蔽血管
2. 肺炎/感染：片状/斑片状实变、支气管充气征
3. 磨玻璃影(GGO)：弥漫性/局灶性，面积和分布
4. 肺气肿/慢阻肺：低密度区、肺大疱
5. 肺间质病变：网格影、蜂窝影、纤维化
6. 胸膜/积液：胸腔积液量、胸膜增厚、气胸
7. 淋巴结/纵隔：淋巴结肿大、纵隔增宽
8. 气道异常：支气管扩张、气道狭窄
9. 其他：钙化、空洞、肺不张、血管异常

【结节大小估算——必须利用尺寸参考信息】
- 提示词中会提供像素间距（mm/像素）和视野大小，请利用这些信息估算结节实际直径
- 误差控制在 ±2mm 以内

【密度判断铁律（针对结节）】
- 必须基于图像视觉：遮蔽血管 → 至少mGGN或solid；血管可见穿行 → pGGN
- 第二张窄窗位图像提高了低密度区的对比度，实性结节在此图中仍为高密度白色

【忠实性原则】
- 只描述实际可见的异常，不臆造
```

**文字提示（User，每张切片独立）：**
```
切片 #2：第一张为标准肺窗（WW=1200），第二张为窄窗位（WW=600）。
⚠️ 方向提示：图像左侧 = 患者右肺，图像右侧 = 患者左肺。

【尺寸参考——请用于估算结节大小】
  像素间距: 0.703mm/像素（512×512图像对应实际视野约360mm）
  切片厚度: 1.25mm
  估算公式: 结节占图像宽度的比例 × 360mm = 实际直径
  示例: 结节占图像宽度约3% → 直径约11mm
        结节占图像宽度约6% → 直径约22mm

【图像标注说明——关键！请充分利用】
① 肺窗图像底部有黄色物理标尺（每格=10mm），可直接对比结节与刻度来测量大小，无需估算像素比例
② 若肺窗图像中有彩色矩形框（红/橙/绿/蓝），框内即为算法检测到的疑似结节区域
   - 框右上角标签"候选N ~Xmm"中的Xmm是算法估计直径（供参考，你需要基于标尺独立测量）
   - 若候选框内的区域视觉上不像结节，请如实描述所见并说明理由
③ 测量方法：将结节的最大横径与底部标尺对比，读出毫米数

请对这张切片进行全面系统扫描，检查所有类型肺部异常（不只看结节）：
① 扫描整个肺野是否有肺炎、磨玻璃影、气肿、间质改变等
② 检查胸膜腔是否有积液、气胸
③ 观察纵隔和淋巴结是否异常
④ 重点检查以下算法标记的疑似结节位置（仅位置参考，密度类型请自行视觉判断）：
  候选1: 患者右肺（图像左侧） 下叶区域，算法估计直径约16.2mm，置信度0.73
  （图像坐标 x≈0.32, y≈0.75，密度类型请自行基于图像判断）

结节大小请优先通过对比底部黄色标尺来测量，精确到1mm。
```

#### LLM 返回（JSON）

```json
{
  "slice_rank": 2,
  "visual_description": "双肺野清晰，右肺下叶见一枚高密度结节，形态类圆形，边界清晰，遮蔽周围血管...",
  "abnormal_regions": [
    {
      "location": "右肺下叶",
      "size_mm": "约16x13mm",
      "density_type": "solid",
      "density_basis": "结节呈高密度白色，遮蔽周围血管，在窄窗图中仍为高密度",
      "description": "类圆形高密度结节，边界清晰，无毛刺"
    }
  ],
  "ggn_detected": false,
  "other_findings": [],
  "quality_note": null
}
```

#### 关键设计点
- `temperature=0.1`（低随机性，保证一致性）
- `response_format="json_object"`（强制 JSON 输出）
- 图像以 `detail: "high"` 传入（高分辨率分析）
- 任意一张切片失败不影响整体，使用 fallback 占位

---

### 7.2 Step 2：跨切片整合

**输入：** Step1 的全部切片感知结果（10 张切片的 JSON 描述）

**目的：** 将 10 张切片的感知结果合并，消除重复，整合出统一的结节清单和其他异常清单。

#### 发给 LLM 的内容（纯文本，无图片）

**System：**
```
你是一名资深影像科医生，擅长多切片CT图像综合分析。
请基于多张切片的视觉感知结果，整合出两个清单：
1. 结节清单（nodules）：跨切片可见的肺结节/肿块
2. 其他异常清单（other_findings）：肺炎/积液/气肿/间质病变等

【结节整合规则】
1. 只整合有视觉描述支持的结节，禁止凭算法候选臆造
2. 同一结节在多切片均可见时，只保留一个（合并位置相近+相邻切片的）
3. density_type 必须来自视觉感知，取多切片多数票，solid/mGGN 优先于 pGGN
4. 结节大小取各切片描述的最大测量值

【其他异常整合规则】
1. 合并多切片中描述的相同位置/类型异常
2. 若所有切片均无非结节异常，other_findings 返回空列表
```

**User：**
```json
各切片视觉感知结果（共10张切片）：
[{"slice_rank": 0, "visual_description": "...", ...}, ...]

备注：算法共检测到 8 个候选区域，最大直径约 16.2mm（仅供数量参考，密度类型以视觉感知为准）。
```

#### LLM 返回

```json
{
  "nodules": [
    {
      "integrated_nodule_id": "N1",
      "best_slice_rank": 2,
      "cross_slice_consistency": "高",
      "estimated_3d_size": "约16x13mm",
      "location_description": "右肺下叶（患者解剖方向）",
      "density_type": "solid",
      "algo_density_type": null
    }
  ],
  "other_findings": [
    {
      "finding_id": "F1",
      "category": "肺气肿",
      "location": "双肺",
      "description": "双肺弥漫性低密度改变，肺纹理稀疏，符合肺气肿表现",
      "severity": "轻度",
      "supporting_slices": [0, 1, 2, 3]
    }
  ]
}
```

---

### 7.3 Step 3：生成最终全面报告

**输入：** Step2 整合结节清单 + 其他异常清单 + Step1 前5张切片的文字描述

**目的：** 生成完整的肺部 CT 分析报告，包含 Lung-RADS 分级。

#### 发给 LLM 的内容（纯文本，无图片）

**System（Lung-RADS 严格规则）：**
```
【Lung-RADS 2022 分级规则——必须严格遵守】

▌实性结节（solid）：
  直径 < 6mm                  → Lung-RADS 2
  直径 6mm ~ < 8mm            → Lung-RADS 3（6个月CT随访）
  直径 8mm ~ < 15mm           → Lung-RADS 4A（3个月CT随访或PET-CT）
  直径 ≥ 15mm 或有毛刺/分叶   → Lung-RADS 4B（活检或手术）

▌纯磨玻璃结节（pGGN）：
  直径 < 6mm                  → Lung-RADS 2
  直径 6mm ~ 20mm             → Lung-RADS 3（6个月CT随访）
  直径 > 20mm                 → Lung-RADS 4A

▌混合磨玻璃结节（mGGN）：
  直径 < 6mm                  → Lung-RADS 2
  直径 ≥ 6mm，实性成分 < 6mm  → Lung-RADS 4A
  实性成分 ≥ 6mm              → Lung-RADS 4B

▌微小结节（< 6mm）           → Lung-RADS 2（无论密度类型）
```

**User（包含 Step2 整合结果）：**
```
【Step2 整合结节清单（密度类型来自视觉感知，请严格使用）】：
[{"integrated_nodule_id": "N1", "density_type": "solid", "estimated_3d_size": "约16x13mm", ...}]

【Step2 整合其他异常清单】：
[{"category": "肺气肿", "description": "..."}]

【Step1 视觉感知摘要（前5张切片）】：
["双肺野清晰，右肺下叶见一枚高密度结节...", ...]

【密度类型映射】：pGGN=纯磨玻璃结节，mGGN=混合磨玻璃结节，solid=实性结节
```

#### LLM 返回（最终报告 JSON）

```json
{
  "findings": [
    "右肺下叶一枚约16×13mm实性结节，边界清晰，类圆形",
    "双肺弥漫性轻度肺气肿改变"
  ],
  "impression": "右肺下叶实性结节，大小约16×13mm，Lung-RADS 4B，建议活检或手术。双肺轻度肺气肿。",
  "nodule_assessment": [
    {
      "nodule_id": "N1",
      "location": "右肺下叶",
      "size_mm": "16x13mm",
      "lung_rads_grade": "4B",
      "morphology": "类圆形，边界清晰",
      "density_type": "实性结节(solid)",
      "malignancy_risk": "高",
      "follow_up": "建议活检或手术切除"
    }
  ],
  "pulmonary_findings": [
    {
      "finding_id": "F1",
      "category": "肺气肿",
      "location": "双肺",
      "description": "双肺弥漫性低密度改变，肺纹理稀疏",
      "severity": "轻度",
      "clinical_significance": "建议肺功能检查",
      "follow_up": "定期复查"
    }
  ],
  "overall_lung_rads": "4B",
  "recommendations": ["建议尽快行肺结节活检或手术切除", "建议行肺功能检查评估肺气肿程度"],
  "confidence": 0.88,
  "limitations": ["AI分析结果存在局限性，需结合临床信息综合判断"],
  "disclaimer": "本报告由AI辅助生成，仅供医学专业人员参考，不构成临床诊断依据。"
}
```

---

## 八、Stage 7：结果落库

**入口文件：** `app/pipeline/stage7_storage.py`

### 8.1 报告质量评估

调用 `ReportEvaluator.evaluate(report)`：
- 检查 `findings` 是否非空
- 检查 `nodule_assessment` 结节是否有合法的 Lung-RADS 等级
- 检查 `confidence` 是否 > 0
- 输出评估状态（pass/warn/fail）和综合评分（0~1）

### 8.2 数据库持久化

写入 `analysis_results` 表，包含以下字段：

| 字段 | 内容 |
|------|------|
| `id` | UUID，结果唯一标识 |
| `task_id` | 关联的任务 ID |
| `findings` | 影像发现列表（JSON） |
| `impression` | 总体印象（文本） |
| `nodule_assessment` | 结节详细评估（JSON，含 Lung-RADS 等级）|
| `pulmonary_findings` | 其他肺部异常（JSON）|
| `overall_lung_rads` | 整体最高 Lung-RADS 等级（如 "4B"）|
| `recommendations` | 随访建议（JSON）|
| `confidence` | AI 置信度（0~1）|
| `cot_snapshot` | CoT 三步推理中间过程完整快照（JSON，含每步 token 用量）|
| `raw_response` | LLM 原始返回文本 |
| `llm_model` | 实际使用的模型名称 |
| `tokens_step1/2/3` | 各步骤 token 消耗量 |
| `eval_scores` | 报告质量评估结果 |

### 8.3 清理中间产物

Stage7 落库完成后，异步等待 10 秒后清理：
- `storage/processed/{task_id}/extracted/` — 解压后的 DICOM 原始文件
- TotalSegmentator 临时输出目录（若存在）
- **保留**：渲染好的 PNG 切片（`slices/` 目录），供前端展示

---

## 九、发给大模型的完整内容总结

### 每次 LLM 调用数量

| 步骤 | 调用次数 | 图像 | 文本 |
|------|---------|------|------|
| Step 1 | top_k 次（默认10次，并发） | 每次2张带标注PNG | 系统提示 + 图像标注说明 + 尺寸参考 + 候选位置 |
| Step 2 | 1次 | 无图像 | 10张切片的感知结果JSON |
| Step 3 | 1次 | 无图像 | 整合结节+其他异常+Lung-RADS规则 |

**每次分析总计 12 次 LLM 调用，Step1 并发执行。**

### Step 1 每次请求发送的图像规格

- 格式：PNG，RGB三通道
- 尺寸：512×512 像素
- 编码：base64，内嵌在 JSON 消息体
- detail 级别：`high`（最高分辨率分析）
- 每次请求附带 2 张图（肺窗 + 窄窗位）
- 图像标注：肺窗含底部黄色物理标尺 + 彩色候选框；纵隔/窄窗仅含标尺

---

## 十、关键设计原则

### 10.1 算法不主导，LLM 自主视觉判断

- Stage3 的 HU 阈值仅用于定位候选位置坐标
- **密度类型（实性/GGN/mGGN）完全由 LLM 基于图像判断，算法结果不传递给 LLM**
- 这解决了 HU 阈值误判导致的"实性结节被错判为 GGN"问题

### 10.2 双重病灶筛查保障

- Stage3（算法候选）+ Stage4 病灶显著性评分（独立图像分析）双轨并行
- 即使 Stage3 完全漏检，Stage4 的图像特征评分仍能找到有病灶的切片
- 两层机制互补，大幅降低"有病灶却拍不到"的概率

### 10.3 图像标注 + 尺寸参考注入

- 渲染 PNG 时在图像上直接叠加**黄色物理标尺**（底部，每格=10mm），LLM 无需估算像素占比，直接对比刻度即可读出毫米数
- 肺窗图像额外叠加**彩色候选框**（Stage3 检测到的候选位置），框标签"候选N ~Xmm"辅助 LLM 快速定位并聚焦测量
- 标尺精度来自 DICOM 的 `PixelSpacing × Rows`，物理误差 < 0.05mm
- 提示词中同步注入像素间距、视野大小、切片厚度作为备用参考
- 目标：结节大小估算误差从 ±5~8mm 压缩到 ±1~2mm

### 10.4 放射学方向校正

- 明确告知 LLM：CT 轴位图像中，图像**左侧 = 患者右肺**，图像**右侧 = 患者左肺**
- 所有位置描述统一使用"患者解剖方向"（患者左肺/右肺），而非图像方向

### 10.5 Lung-RADS 严格规则驱动

- Step3 系统提示中硬编码完整的 Lung-RADS 2022 分级规则表
- 防止大模型凭直觉随意给分，确保临床合规性

---

## 十一、数据流示意

```
ZIP 压缩包
    │
    ├─ Stage1 ──► 解压 DCM × 120 → SHA256 去重 → DICOM 脱敏
    │
    ├─ Stage2 ──► Z轴排序 → 切片数检查(≥5) → 缺口检测 → passed/rejected
    │
    ├─ Stage3 ──► 120张全量扫描 → HU阈值过滤 → 形态过滤 → 50个候选(含坐标+直径)
    │
    ├─ Stage4 ──► 病灶显著性评分(60张并行) → 双轨选片 → pHash去重
    │              → 10张切片 × 3张PNG(肺窗+纵隔+窄窗) → 512×512 RGB
    │              → 叠加黄色物理标尺(底部,每格10mm) + 肺窗候选框(含算法估计直径标签)
    │
    ├─ Stage5 ──► 候选筛选(≤25个,无密度) → 用户提示词 → LLMPayload
    │
    ├─ Stage6 ──► Step1: 10×(2张带标注图+文字) → 10个感知JSON  [并发，共20张图]
    │             Step2: 10个感知JSON → 整合JSON               [1次调用，无图]
    │             Step3: 整合JSON+Lung-RADS规则 → 最终报告JSON  [1次调用，无图]
    │
    └─ Stage7 ──► 质量评估 → 落库 → 清理中间产物 → 返回 result_id
```

---

*最后更新：2026-05-09 | 版本：v2.1（图像物理标尺 + 候选框标注 + TOP_K=10）*
