"""Shared type aliases and base models for core modules."""

import sqlite3
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type JsonMapping = Mapping[str, JsonValue]
type DbConnection = sqlite3.Connection
type DbRow = sqlite3.Row


class StrictModel(BaseModel):
    """Base model for boundary value objects that forbids unknown fields."""

    model_config = ConfigDict(extra="forbid")
