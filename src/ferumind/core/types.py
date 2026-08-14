"""Shared type aliases and base models for core modules."""

import sqlite3
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

# ``None`` goes last (RUF036): a union reads more clearly with the absence case
# at the end, and Ruff 0.16 enforces it.
type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]
type JsonMapping = Mapping[str, JsonValue]
type DbConnection = sqlite3.Connection
type DbRow = sqlite3.Row


class StrictModel(BaseModel):
    """Base model for boundary value objects that forbids unknown fields."""

    model_config = ConfigDict(extra="forbid")
