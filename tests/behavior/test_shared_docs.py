"""A2 — 부서 공유 문서함 scope·ACL."""
from __future__ import annotations

from harag.api.acl_helpers import (
    can_manage_shared, personal_acl_tags, shared_acl_tags,
)
from harag.contracts.boundaries import AuthContext
from harag.storage.metadata_store import MetadataStore


def test_A2_can_manage_shared_roles():
    admin = AuthContext(
        user_id="a", acl_tags=frozenset({"owner:a", "role:doc_admin", "dept:claims"}))
    staff = AuthContext(
        user_id="b", acl_tags=frozenset({"owner:b", "dept:claims"}))
    assert can_manage_shared(admin) is True
    assert can_manage_shared(staff) is False


def test_A2_personal_strips_dept_tags():
    auth = AuthContext(
        user_id="a",
        acl_tags=frozenset({"owner:a", "dept:claims", "role:staff"}),
    )
    assert personal_acl_tags(auth) == ["owner:a"]
    tags = shared_acl_tags(auth)
    assert "dept:claims" in tags
    assert "scope:shared" in tags


def test_A2_list_for_acl_shared_only():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    meta.register_for_owner(
        "d1", "mine.pdf", "alice", department="", scope="personal")
    meta.register_for_owner(
        "d2", "shared.pdf", "alice", department="claims", scope="shared")
    meta.register_for_owner(
        "d3", "carol.pdf", "carol", department="", scope="personal")

    bob = meta.list_for_acl("bob", ["dept:claims"])
    ids = {r.document_id for r in bob}
    assert "d2" in ids
    assert "d1" not in ids
    assert "d3" not in ids

    alice = meta.list_for_acl("alice", ["dept:claims"])
    a_ids = {r.document_id for r in alice}
    assert "d1" in a_ids and "d2" in a_ids


def test_A2_find_accessible_shared():
    meta = MetadataStore(dsn="sqlite:///:memory:")
    meta.register_for_owner(
        "sx", "policy.pdf", "alice", department="claims", scope="shared")
    assert meta.find_accessible("sx", "bob", ["dept:claims"]) is not None
    assert meta.find_accessible("sx", "bob", ["dept:other"]) is None
