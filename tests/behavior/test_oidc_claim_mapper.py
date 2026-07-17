"""OIDC/JWT 클레임 → ACL 태그."""
from __future__ import annotations

from harag.api.auth_jwt import _default_claim_mapper


def test_claim_mapper_dept_and_roles():
    uid, tags = _default_claim_mapper({
        "sub": "alice",
        "departments": ["benefits"],
        "roles": ["counter"],
        "acl_mode": "dept",
    })
    assert uid == "alice"
    assert "dept:benefits" in tags
    assert "role:counter" in tags
    assert "acl:dept" in tags


def test_claim_mapper_singular_department():
    uid, tags = _default_claim_mapper({
        "sub": "bob",
        "department": "claims",
    })
    assert uid == "bob"
    assert "dept:claims" in tags
