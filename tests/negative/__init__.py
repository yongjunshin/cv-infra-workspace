"""QA-owned cross-cutting negative contract suite (DoD §6 NEG-1~5 + p6 NEG-6).

Module unit tests live in ``tests/test_<module>_*.py`` (implementation teams);
this package holds the negative gates the DoD fixes BY FILENAME
(decision 2026-06-25-tests-ownership). Stdlib + pytest.

NEG-6 (``test_batch_no_residue.py``, p6c5 T4) is NEW and is the one gate the DoD
§6 table does not name yet — 설계 정본 `implementation-plan/p6-implementation-plan.md`
§1/§4 creates it, and the DoD 문면 개정은 PM 집행 대상이다(QA는 제안만 한다).
"""
