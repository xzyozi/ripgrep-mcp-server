import logging
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from typing import List

logger = logging.getLogger(__name__)

def is_safe_path(base_dir: str | Path, target_path: str | Path) -> bool:
    """
    検証対象のパス (target_path) がベースディレクトリ (base_dir) の配下に存在するか検証する。
    """
    try:
        base = Path(base_dir).resolve()
        target = Path(target_path)
        if not target.is_absolute():
            target = base / target
        
        target = target.resolve()
        
        # targetがbaseと同じか、またはbaseの配下にあれば安全
        return base in target.parents or base == target
    except Exception as e:
        logger.error(f"Path validation error: {e}", exc_info=True)
        return False

class SearchParams(BaseModel):
    query: str = Field(..., min_length=1)
    target_dir: str = Field(...)
    context_lines: int = Field(3, ge=0, le=20)
    file_extensions: List[str] = Field(default_factory=list)
    max_matches: int = Field(20, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def query_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query cannot be empty or whitespace only")
        return v
