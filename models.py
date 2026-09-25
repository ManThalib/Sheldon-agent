"""Dataclass models for Sheldon LP score results."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PoolScore:
    """Result of scoring a single pool LP opportunity."""

    pool: Optional[str]
    pool_address: Optional[str]
    dex: Optional[str]
    pair_class: str
    pair: List[Optional[str]]
    score: float
    components: Dict[str, float]
    verdict: str
    reason: str
    data_quality: Dict[str, Any]
    _pool: Optional[dict] = field(repr=False)

    def to_dict(self, include_internal: bool = False) -> Dict[str, Any]:
        d = asdict(self)
        if not include_internal:
            d.pop("_pool", None)
        return d


@dataclass
class PositionScore:
    """Result of scoring a single open LP position."""

    position_id: Optional[str]
    pool: Optional[str]
    pool_address: Optional[str]
    pair_class: str
    pair: List[Optional[str]]
    lower_bound: Any
    upper_bound: Any
    fees_usd: Optional[float]
    score: float
    components: Dict[str, float]
    verdict: str
    collect_fees: bool
    reason: str
    data_quality: Dict[str, Any]
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
