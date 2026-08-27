"""QA-owned cross-cutting negative contract suite (DoD §6 NEG-1~5 + p6 NEG-6).

Module unit tests live in ``tests/test_<module>_*.py`` (implementation teams);
this package holds the negative gates the DoD fixes BY FILENAME
(decision 2026-06-25-tests-ownership). Stdlib + pytest.

NEG-6 (``test_batch_no_residue.py``, p6c5 T4) — DoD §6에 정식 등재됨
(2026-08-27 PM 집행, `implementation-plan/03-definition-of-done.md` §6 NEG-6 절).
"""
