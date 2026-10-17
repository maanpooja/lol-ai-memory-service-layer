"""Pydantic models for every LLM output. Used as the Gemini response schema and to validate.

Only types are enforced here. Ranges, categories and ids are checked in code
(governance/policy.py and service.py), never trusted from the model.
"""

from typing import Literal

from pydantic import BaseModel


class ExtractedFact(BaseModel):
    fact: str
    category: str
    confidence: float
    sensitivity: str  # "none" or a blocked category
    authority_claim: bool


class ExtractResult(BaseModel):
    facts: list[ExtractedFact]


class Decision(BaseModel):
    fact_index: int
    op: Literal["ADD", "UPDATE", "NONE"]
    target: str  # an existing memory's ref (e.g. "m3") for UPDATE, otherwise ""
    text: str  # final fact text to store


class DecideResult(BaseModel):
    decisions: list[Decision]


class RelevanceResult(BaseModel):
    refs: list[str]  # most useful first


class ClassifyResult(BaseModel):
    sensitivity: str
    authority_claim: bool
