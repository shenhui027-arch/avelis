"""业务域示例问题的接口（ADR 0010 第 3 点）：负责人、维护者可以设置；其他成员只能读取；读取只含可见业务域。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.db


def test_set_and_read_example_questions(meta_db, factory):
    from avelis.api import catalog
    from avelis.db import session_scope
    from avelis.models import Domain, DomainMember, User

    owner, viewer, outsider = factory.user(roles=("steward",)), factory.user(), factory.user()
    with session_scope() as s:
        d = Domain(code="dq_test", name="示例域", owner_id=owner)
        s.add(d)
        s.flush()
        s.add_all([DomainMember(domain_id=d.id, user_id=owner, role="owner"),
                   DomainMember(domain_id=d.id, user_id=viewer, role="viewer")])
        domain_id = d.id
    body = catalog.ExampleQuestionsSet(questions=[{"text": "问题一", "mode": "ask"}, {"text": "问题二", "mode": "analysis"}])
    with session_scope() as s:
        with pytest.raises(Exception):
            catalog.set_example_questions(domain_id, body, s.get(User, viewer), s)  # 只有负责人、维护者能改
    with session_scope() as s:
        view = catalog.set_example_questions(domain_id, body, s.get(User, owner), s)
        assert view["example_questions"] == [{"text": "问题一", "mode": "ask"}, {"text": "问题二", "mode": "analysis"}]
    with session_scope() as s:
        asked = catalog.get_example_questions("ask", s.get(User, viewer), s)["questions"]
        assert {"text": "问题一", "domain": "示例域", "source": "configured"} in asked
        assert all(q["text"] != "问题二" for q in asked)
        mine = [q for q in catalog.get_example_questions("ask", s.get(User, outsider), s)["questions"]
                if q.get("domain") == "示例域"]
        assert mine == []  # 不可见的业务域不返回
