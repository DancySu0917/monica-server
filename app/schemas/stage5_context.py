from pydantic import BaseModel
from typing import List, Dict, Any, Optional


class NoduleDescription(BaseModel):
    nodules: List[Dict[str, Any]] = []


class MedicalContext(BaseModel):
    similar_cases:  List[Dict[str, Any]] = []
    guidelines:     List[Dict[str, Any]] = []


class LLMPayload(BaseModel):
    task_id:              str
    user_prompt:          str
    selected_slices:      List[Any]           # SelectedSlice 列表
    nodule_description:   NoduleDescription
    medical_context:      MedicalContext
    historical_results:   List[Dict[str, Any]] = []
    stage:                str = "stage5_context"
    elapsed_ms:           int
