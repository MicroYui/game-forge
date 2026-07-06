def test_solvers_available():
    import clingo, z3  # noqa: F401

    assert clingo.__version__
    assert z3.get_version_string()


def test_review_schema_version_present():
    from gameforge.contracts import versions as v

    assert v.REVIEW_SCHEMA_VERSION == "review@1"


def test_new_spine_packages_importable():
    import gameforge.spine.dsl  # noqa: F401
    import gameforge.spine.sim  # noqa: F401
