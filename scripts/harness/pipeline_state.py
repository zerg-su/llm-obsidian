"""Additive graph progress and content-addressed receipts for pipelines."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .contracts import ContractError, to_dict
from .pipelines import (
    CompiledPipeline,
    PipelineOperationBinding,
    _identifier,
    _schema_id,
    _sha256,
    _version,
)
from .store import OperationStore


class PipelineLedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineControllerState:
    owner_id: str
    pipeline_run_id: str
    definition_sha256: str
    step_order: tuple[str, ...]
    completed_steps: tuple[str, ...] = ()
    status: str = "running"
    revision: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.revision < 0:
            raise ContractError("invalid pipeline controller metadata")
        _identifier(self.owner_id, "pipeline owner_id")
        _identifier(self.pipeline_run_id, "pipeline_run_id")
        _sha256(self.definition_sha256, "pipeline definition")
        if (
            not self.step_order
            or len(set(self.step_order)) != len(self.step_order)
            or any(
                _identifier(step_id, "pipeline step_id") != step_id
                for step_id in self.step_order
            )
        ):
            raise ContractError("pipeline step order must be non-empty and unique")
        if self.completed_steps != self.step_order[: len(self.completed_steps)]:
            raise ContractError("completed pipeline steps must form an ordered prefix")
        expected_status = (
            "reap-ready"
            if self.completed_steps == self.step_order
            else "running"
        )
        if self.status != expected_status:
            raise ContractError("pipeline controller status disagrees with progress")


@dataclass(frozen=True)
class StepReceipt:
    definition_sha256: str
    step_id: str
    primitive_id: str
    primitive_version: str
    operation_id: str
    replay_key: str
    input_sha256: str
    output_sha256: str
    output_schema: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported StepReceipt schema")
        _sha256(self.definition_sha256, "receipt definition")
        _identifier(self.step_id, "receipt step_id")
        _identifier(self.primitive_id, "receipt primitive_id")
        _version(self.primitive_version, "receipt primitive_version")
        _identifier(self.operation_id, "receipt operation_id")
        _sha256(self.replay_key, "receipt replay_key")
        _sha256(self.input_sha256, "receipt input")
        _sha256(self.output_sha256, "receipt output")
        _schema_id(self.output_schema, "receipt output_schema")


def _state_from_dict(value: Mapping[str, object]) -> PipelineControllerState:
    return PipelineControllerState(
        owner_id=str(value.get("owner_id") or ""),
        pipeline_run_id=str(value.get("pipeline_run_id") or ""),
        definition_sha256=str(value.get("definition_sha256") or ""),
        step_order=tuple(value.get("step_order") or ()),
        completed_steps=tuple(value.get("completed_steps") or ()),
        status=str(value.get("status") or ""),
        revision=int(value.get("revision", -1)),
        schema_version=int(value.get("schema_version", 0)),
    )


def _receipt_from_dict(value: Mapping[str, object]) -> StepReceipt:
    return StepReceipt(
        definition_sha256=str(value.get("definition_sha256") or ""),
        step_id=str(value.get("step_id") or ""),
        primitive_id=str(value.get("primitive_id") or ""),
        primitive_version=str(value.get("primitive_version") or ""),
        operation_id=str(value.get("operation_id") or ""),
        replay_key=str(value.get("replay_key") or ""),
        input_sha256=str(value.get("input_sha256") or ""),
        output_sha256=str(value.get("output_sha256") or ""),
        output_schema=str(value.get("output_schema") or ""),
        schema_version=int(value.get("schema_version", 0)),
    )


class PipelineLedger:
    """Use the operation store's owner lock and atomic writer for graph state."""

    def __init__(self, operation_store: OperationStore):
        self.operation_store = operation_store

    def _run_dir(self, owner_id: str, pipeline_run_id: str) -> Path:
        _identifier(owner_id, "pipeline owner_id")
        _identifier(pipeline_run_id, "pipeline_run_id")
        return (
            self.operation_store.root
            / "owners"
            / owner_id
            / "pipelines"
            / pipeline_run_id
        )

    def _state_path(self, owner_id: str, pipeline_run_id: str) -> Path:
        return self._run_dir(owner_id, pipeline_run_id) / "state.json"

    def _receipt_path(
        self, owner_id: str, pipeline_run_id: str, replay_key: str
    ) -> Path:
        _sha256(replay_key, "receipt replay_key")
        return (
            self._run_dir(owner_id, pipeline_run_id)
            / "receipts"
            / f"{replay_key}.json"
        )

    @staticmethod
    def _read(path: Path, parser):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("record must be an object")
            return parser(value)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PipelineLedgerError(f"invalid pipeline record {path.name}") from exc

    def read(
        self, owner_id: str, pipeline_run_id: str
    ) -> PipelineControllerState:
        path = self._state_path(owner_id, pipeline_run_id)
        if not path.is_file():
            raise PipelineLedgerError(f"unknown pipeline run: {pipeline_run_id}")
        return self._read(path, _state_from_dict)

    def start(
        self,
        owner_id: str,
        pipeline_run_id: str,
        compiled: CompiledPipeline,
    ) -> PipelineControllerState:
        state = PipelineControllerState(
            owner_id=owner_id,
            pipeline_run_id=pipeline_run_id,
            definition_sha256=compiled.definition_sha256,
            step_order=tuple(
                step.step_id
                for step in compiled.definition.steps
                if step.primitive_id not in {"human_gate", "bounded_loop"}
            ),
        )
        with self.operation_store.locked(owner_id):
            path = self._state_path(owner_id, pipeline_run_id)
            if path.is_file():
                existing = self._read(path, _state_from_dict)
                if (
                    existing.owner_id,
                    existing.pipeline_run_id,
                    existing.definition_sha256,
                    existing.step_order,
                ) != (
                    state.owner_id,
                    state.pipeline_run_id,
                    state.definition_sha256,
                    state.step_order,
                ):
                    raise PipelineLedgerError(
                        "pipeline run identity already belongs to another definition"
                    )
                return existing
            path.parent.mkdir(parents=True, exist_ok=True)
            self.operation_store._write(path, to_dict(state))
            return state

    def lookup(
        self,
        owner_id: str,
        pipeline_run_id: str,
        replay_key: str,
    ) -> StepReceipt | None:
        path = self._receipt_path(owner_id, pipeline_run_id, replay_key)
        return self._read(path, _receipt_from_dict) if path.is_file() else None

    def accept(
        self,
        owner_id: str,
        pipeline_run_id: str,
        binding: PipelineOperationBinding,
        *,
        output_sha256: str,
    ) -> tuple[PipelineControllerState, StepReceipt]:
        receipt = StepReceipt(
            definition_sha256=binding.definition_sha256,
            step_id=binding.step_id,
            primitive_id=binding.primitive_id,
            primitive_version=binding.primitive_version,
            operation_id=binding.spec.operation_id,
            replay_key=binding.replay_key,
            input_sha256=binding.input_sha256,
            output_sha256=output_sha256,
            output_schema=binding.output_schema,
        )
        with self.operation_store.locked(owner_id):
            state = self.read(owner_id, pipeline_run_id)
            if (
                state.owner_id != owner_id
                or binding.spec.owner_id != owner_id
                or state.definition_sha256 != binding.definition_sha256
            ):
                raise PipelineLedgerError(
                    "receipt does not belong to the exact pipeline run"
                )
            receipt_path = self._receipt_path(
                owner_id, pipeline_run_id, binding.replay_key
            )
            existing = (
                self._read(receipt_path, _receipt_from_dict)
                if receipt_path.is_file()
                else None
            )
            if existing is not None and existing != receipt:
                raise PipelineLedgerError(
                    "replay key already has a conflicting output receipt"
                )
            if binding.step_id in state.completed_steps:
                if existing == receipt:
                    return state, receipt
                raise PipelineLedgerError("completed step cannot change its receipt")
            next_index = len(state.completed_steps)
            if (
                next_index >= len(state.step_order)
                or state.step_order[next_index] != binding.step_id
            ):
                raise PipelineLedgerError("pipeline receipt is out of order")
            if existing is None:
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                self.operation_store._write(receipt_path, to_dict(receipt))
            completed = state.completed_steps + (binding.step_id,)
            updated = replace(
                state,
                completed_steps=completed,
                status=(
                    "reap-ready"
                    if completed == state.step_order
                    else "running"
                ),
                revision=state.revision + 1,
            )
            self.operation_store._write(
                self._state_path(owner_id, pipeline_run_id),
                to_dict(updated),
            )
            return updated, receipt
