from __future__ import annotations

from types import SimpleNamespace

from fpa.domains.master_data import masterdata_write
from fpa.domains.master_data.masterdata_write import MaterialWriteService
from fpa.kernel.scope import Scope


class _MaterialTx:
    def __init__(self) -> None:
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql
        return 1

    def last_insert_id(self):
        return 31


class _MaterialService(MaterialWriteService):
    @classmethod
    def load_material(cls, tx, *, scope, record_id):
        return {
            "id": record_id,
            "organization_id": 1,
            "farm_id": 2,
            "area_id": 3,
            "code": "M-NEW",
            "name": "新物料",
            "status": "draft",
            "row_version": 1,
        }

    @classmethod
    def _decorate(cls, row, scope, permissions):
        return dict(row)


def test_material_creation_starts_as_draft(monkeypatch) -> None:
    monkeypatch.setattr(
        masterdata_write,
        "tenant_keys_for_create",
        lambda tx, scope: {"organization_id": 1, "farm_id": 2, "area_id": 3},
    )
    tx = _MaterialTx()
    ctx = SimpleNamespace(
        actor=SimpleNamespace(user_id=9, permissions=frozenset({"material.create"})),
        require=lambda _permission: None,
    )

    result = _MaterialService().create_material(
        tx,
        ctx,
        Scope.all_data(user_id=9),
        "M-NEW",
        "新物料",
        "feed",
        "20kg",
        "kg",
        10,
        2,
        30,
        "",
    )

    assert "'draft'" in tx.sql
    assert result.data["record"]["status"] == "draft"


def test_material_submit_and_verify_are_registered() -> None:
    from fpa.bootstrap import load_all
    from fpa.kernel.capability import Confirmation, REGISTRY

    load_all()
    submit = REGISTRY.find("material.submit")
    verify = REGISTRY.find("material.verify")

    assert submit is not None
    assert verify is not None
    assert verify.effective_confirmation is Confirmation.ALWAYS


def test_farm_create_is_registered() -> None:
    from fpa.bootstrap import load_all
    from fpa.kernel.capability import REGISTRY

    load_all()
    capability = REGISTRY.find("farm.create")

    assert capability is not None
    assert capability.path == "/api/v1/farms"
    assert capability.required_permission == "farm.create"
